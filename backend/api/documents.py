"""批量文档入库 API：上传、进度查询、SSE 实时推送。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, UploadFile, File
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from service.batch_ingestor import start_batch, get_batch_status as query_batch_status
from service.redis_client import compute_batch_progress, get_cached_doc_index, is_batch_terminal

logger = logging.getLogger(__name__)

router = APIRouter()
UPLOAD_DIR = Path(__file__).resolve().parent.parent / "data" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".doc", ".md", ".txt"}


# ── 1. POST /api/documents/batch-ingest ──────────────

@router.post("/documents/batch-ingest")
async def batch_ingest(
    files: list[UploadFile] = File(...),
    session_id: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """接收多文件上传，保存到 data/uploads，创建批量入库任务。"""
    if not files:
        raise HTTPException(status_code=400, detail="至少需要上传一个文件")

    source_paths: list[str] = []
    display_names: dict[str, str] = {}
    items: list[dict[str, Any]] = []
    precomputed_hashes: dict[str, str] = {}
    scope_key = project_id or session_id or "default"

    for f in files:
        if not f.filename:
            items.append({"filename": "(unknown)", "status": "failed", "error": "文件名为空"})
            continue

        ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
        ext = f".{ext}"
        if ext not in ALLOWED_EXTENSIONS:
            items.append({"filename": f.filename, "status": "failed", "error": f"不支持的文件格式: {ext}"})
            continue

        try:
            contents = await f.read()
        except Exception as exc:
            items.append({"filename": f.filename, "status": "failed", "error": f"读取文件失败: {exc}"})
            continue

        # 内存算 hash → 先查 Redis 缓存，重复的跳过不存盘
        file_hash = hashlib.sha256(contents).hexdigest()
        cached = get_cached_doc_index(scope_key, file_hash)
        if cached and cached.get("status") == "indexed":
            items.append({
                "filename": f.filename, "doc_name": f.filename,
                "file_hash": file_hash, "doc_id": cached.get("doc_id"),
                "status": "cached", "cached": True,
            })
            continue

        # 不重复 → 存盘
        try:
            unique_name = f"{uuid.uuid4().hex[:12]}_{f.filename}"
            save_path = UPLOAD_DIR / unique_name
            save_path.write_bytes(contents)
            abs_path = str(save_path.resolve())
            source_paths.append(abs_path)
            display_names[abs_path] = f.filename
            precomputed_hashes[abs_path] = file_hash
        except Exception as exc:
            items.append({"filename": f.filename, "status": "failed", "error": f"保存文件失败: {exc}"})

    if not source_paths:
        return {"ok": True, "batch_id": "", "total": len(items), "items": items}

    # 调用批量调度器（传预计算 hash，避免 Worker 重复读盘）
    result = start_batch(
        source_paths=source_paths,
        display_names=display_names,
        session_id=session_id,
        project_id=project_id,
        precomputed_hashes=precomputed_hashes,
    )
    # 合并预校验失败项
    result["items"] = items + result.get("items", [])
    result["total"] = len(result["items"])
    return result


# ── 2. GET /api/documents/batch-status/{batch_id} ─────

@router.get("/documents/batch-status/{batch_id}")
async def batch_status(batch_id: str) -> dict[str, Any]:
    """快照查询（SSE 兜底）。"""
    status = query_batch_status(batch_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"batch {batch_id} 不存在或已过期")
    status["progress"] = compute_batch_progress(status)
    return {"ok": True, "batch_id": batch_id, **status}


# ── 3. GET /api/documents/batch-events/{batch_id} ─────

@router.get("/documents/batch-events/{batch_id}")
async def batch_events(batch_id: str):
    """SSE 实时推送 batch 进度。"""

    async def event_generator():
        last_items_json = ""
        heartbeat_interval = 10
        last_heartbeat = time.time()

        for _ in range(1200):  # max 1200s = 20min
            await asyncio.sleep(1)

            status = query_batch_status(batch_id)
            if status is None:
                yield f"event: error\ndata: {{\"error\": \"batch 不存在\"}}\n\n"
                break

            items = status.pop("items", [])
            items_json = json.dumps(items, sort_keys=True, ensure_ascii=False)
            progress = compute_batch_progress(status)

            # 状态有变化 → 推送
            if items_json != last_items_json:
                last_items_json = items_json
                payload = json.dumps({
                    "batch_id": batch_id,
                    "progress": progress,
                    "total_count": int(status.get("total_count", 0)),
                    "success_count": int(status.get("success_count", 0)),
                    "cached_count": int(status.get("cached_count", 0)),
                    "failed_count": int(status.get("failed_count", 0)),
                    "processing_count": int(status.get("processing_count", 0)),
                    "queued_count": int(status.get("queued_count", 0)),
                    "items": items,
                }, ensure_ascii=False)
                yield f"event: batch_progress\ndata: {payload}\n\n"

            # 终态 → 关闭
            if is_batch_terminal(status):
                yield f"event: batch_done\ndata: {{\"batch_id\": \"{batch_id}\", \"progress\": 100}}\n\n"
                break

            # heartbeat
            now = time.time()
            if now - last_heartbeat > heartbeat_interval:
                last_heartbeat = now
                yield f"event: heartbeat\ndata: {{\"ts\": \"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\"}}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
