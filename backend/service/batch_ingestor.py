"""多文档批量入库调度器。

文档级并发：ThreadPoolExecutor 并发处理多个文档。
每个 Worker 串行调用 document_indexer.ingest()，不拆分单文档内部步骤。
"""

from __future__ import annotations

import logging
import secrets
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from typing import Any

from service import redis_client as redis
from service.document_indexer import document_indexer
from service.mineru_parser import compute_file_hash

logger = logging.getLogger(__name__)

MAX_WORKERS = 3
_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

# Chroma 写保护锁（langchain_chroma 客户端非线程安全）
_chroma_write_lock = Lock()

SUPPORTED_EXTENSIONS = {".md", ".txt", ".pdf", ".docx", ".doc"}


def _generate_id(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000)}-{secrets.token_hex(3)}"


def _make_scope_key(project_id: str | None, session_id: str | None) -> str:
    return project_id or session_id or "default"


# ── 主入口 ────────────────────────────────────────────

def start_batch(
    source_paths: list[str],
    *,
    display_names: dict[str, str] | None = None,
    session_id: str | None = None,
    project_id: str | None = None,
    company_id: str | None = None,
    precomputed_hashes: dict[str, str] | None = None,
) -> dict[str, Any]:
    """接收文件路径列表 → 去重 → 后台批量入库 → 返回 batch_id + 每文件状态。"""
    batch_id = _generate_id("BATCH")
    scope_key = _make_scope_key(project_id, session_id)
    items: list[dict[str, Any]] = []
    job_ids: list[str] = []
    display_names = display_names or {}
    precomputed_hashes = precomputed_hashes or {}

    # 预校验 + 去重
    for src_str in source_paths:
        src = Path(src_str)
        doc_name = display_names.get(src_str) or src.name

        # 文件不存在 / 格式不支持 → 直接 failed
        if not src.exists():
            items.append({"source_path": src_str, "doc_name": doc_name, "status": "failed", "error": "文件不存在"})
            continue
        ext = src.suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            items.append({"source_path": src_str, "doc_name": doc_name, "status": "failed", "error": f"不支持的格式: {ext}"})
            continue

        # 计算 file_hash（优先用 precomputed_hashes，避免重复读盘）
        try:
            file_hash = precomputed_hashes.get(src_str) or compute_file_hash(src)
        except Exception as exc:
            items.append({"source_path": src_str, "doc_name": doc_name, "status": "failed", "error": f"hash 计算失败: {exc}"})
            continue

        # ── 去重判断 ──
        result = _resolve_or_create(
            source_path=src_str, file_hash=file_hash, scope_key=scope_key,
            doc_name=doc_name, session_id=session_id, project_id=project_id,
            company_id=company_id, batch_id=batch_id,
        )
        items.append(result)
        if result.get("job_id"):
            job_ids.append(result["job_id"])

    # 初始化 Redis batch 状态
    redis.init_batch(batch_id, len(items))
    for item in items:
        redis.append_batch_item(batch_id, item)
        if item.get("status") == "queued":
            redis.set_job_status(item["job_id"], {
                "job_id": item.get("job_id", ""),
                "status": "queued",
                "doc_name": item.get("doc_name", ""),
                "file_hash": item.get("file_hash", ""),
            })

    return {"ok": True, "batch_id": batch_id, "total": len(items), "items": items}


def _resolve_or_create(**kwargs) -> dict[str, Any]:
    """去重判断 → 分派到 Worker 或直接返回已缓存。"""
    source_path = kwargs["source_path"]
    file_hash = kwargs["file_hash"]
    scope_key = kwargs["scope_key"]
    doc_name = kwargs["doc_name"]

    # ① Redis 缓存
    cached = redis.get_cached_doc_index(scope_key, file_hash)
    if cached and cached.get("status") == "indexed":
        return {
            "source_path": source_path, "doc_name": doc_name,
            "file_hash": file_hash, "doc_id": cached["doc_id"],
            "status": "cached", "cached": True,
        }

    # ② SQLite 回查
    existing = _query_sqlite_indexed(file_hash, kwargs.get("project_id"), kwargs.get("session_id"))
    if existing:
        # 回填 Redis
        redis.cache_doc_index(scope_key, file_hash, {
            "doc_id": existing["doc_id"], "status": "indexed",
            "doc_name": existing.get("doc_name", ""),
        })
        return {
            "source_path": source_path, "doc_name": doc_name,
            "file_hash": file_hash, "doc_id": existing["doc_id"],
            "status": "cached", "cached": True,
        }

    # ③ 创建新任务
    job_id = _generate_id("JOB")
    acquired = redis.acquire_ingest_lock(scope_key, file_hash, job_id)
    if not acquired:
        # 锁被占用 → 同一文件正在被另一个 Worker 处理，不新建任务
        return {
            "source_path": source_path, "doc_name": doc_name,
            "file_hash": file_hash, "status": "processing",
        }

    # 抢到锁 → 提交线程池
    _executor.submit(_worker, job_id=job_id, **kwargs)
    return {
        "source_path": source_path, "doc_name": doc_name,
        "file_hash": file_hash, "status": "queued",
        "job_id": job_id,
    }


def _query_sqlite_indexed(
    file_hash: str, project_id: str | None, session_id: str | None
) -> dict | None:
    """SQLite 查询：同一 file_hash + scope 下是否有 indexed 文档。"""
    try:
        conn = document_indexer._get_conn()
        row = conn.execute(
            """SELECT doc_id, doc_name, status FROM documents
               WHERE file_hash = ? AND status = 'indexed'
                 AND (? IS NULL OR project_id = ?)
               LIMIT 1""",
            (file_hash, project_id, project_id),
        ).fetchone()
        conn.close()
        return {"doc_id": row["doc_id"], "doc_name": row["doc_name"]} if row else None
    except Exception:
        return None


# ── Worker ────────────────────────────────────────────

def _worker(
    job_id: str = "",
    source_path: str = "",
    file_hash: str = "",
    scope_key: str = "",
    doc_name: str = "",
    session_id: str | None = None,
    project_id: str | None = None,
    company_id: str | None = None,
    batch_id: str = "",
    **__,
) -> None:
    """后台线程：调 document_indexer.ingest() → 更新状态。"""
    try:
        # parsing
        redis.set_job_status(job_id, {"status": "parsing", "job_id": job_id, "doc_name": doc_name})
        redis.update_batch_item_status(batch_id, job_id, "parsing")

        # indexing（ingest 内部串行执行 chunk → SQLite → FTS5 → Chroma）
        redis.set_job_status(job_id, {"status": "indexing", "job_id": job_id, "doc_name": doc_name})
        redis.update_batch_item_status(batch_id, job_id, "indexing")

        # 加 Chroma 写保护（多线程下 langchain_chroma 可能不安全）
        with _chroma_write_lock:
            result = document_indexer.ingest(
                source_path=source_path,
                doc_name=doc_name,
                session_id=session_id,
                project_id=project_id,
                company_id=company_id,
            )

        if result.get("ok") and result.get("status") == "indexed":
            doc_id = result.get("doc_id", "")
            redis.cache_doc_index(scope_key, file_hash, {
                "doc_id": doc_id, "status": "indexed", "doc_name": doc_name,
            })
            redis.set_job_status(job_id, {"status": "indexed", "doc_id": doc_id, "doc_name": doc_name})
            redis.update_batch_item_status(batch_id, job_id, "indexed", doc_id=doc_id)
            redis.update_batch_progress(batch_id, "indexed", was_cached=False)
            logger.info("[batch] job=%s indexed doc_id=%s", job_id, doc_id)
        elif result.get("cached"):
            redis.set_job_status(job_id, {"status": "cached", "doc_id": result["doc_id"]})
            redis.update_batch_item_status(batch_id, job_id, "cached", doc_id=result.get("doc_id"))
            redis.update_batch_progress(batch_id, "indexed", was_cached=True)
        else:
            _mark_job_failed(job_id, batch_id, result.get("error", "ingest 返回失败"))

    except Exception as exc:
        _mark_job_failed(job_id, batch_id, str(exc))
    finally:
        redis.release_ingest_lock(scope_key, file_hash)


def _mark_job_failed(job_id: str, batch_id: str, error: str) -> None:
    logger.error("[batch] job=%s FAILED: %s", job_id, error)
    redis.set_job_status(job_id, {"status": "failed", "error_message": error})
    redis.update_batch_item_status(batch_id, job_id, "failed", error_message=error)
    redis.update_batch_progress(batch_id, "failed", was_cached=False)


# ── 进度查询 ──────────────────────────────────────────

def get_batch_status(batch_id: str) -> dict:
    result = redis.get_batch_status(batch_id)
    if result is None:
        return {"ok": False, "error": f"batch {batch_id} 不存在或已过期"}
    return {"ok": True, "batch_id": batch_id, **result}
