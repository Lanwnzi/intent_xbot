"""Redis 客户端：文档索引缓存、任务状态、分布式锁。

Redis 不可用时降级为 no-op，不阻断业务流程。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")

_redis = None


def _get_redis():
    global _redis
    if _redis is not None:
        return _redis
    try:
        from redis import Redis
        _redis = Redis.from_url(REDIS_URL, decode_responses=True)
        _redis.ping()
        logger.info("Redis connected: %s", REDIS_URL)
    except Exception as exc:
        logger.warning("Redis unavailable, caching disabled: %s", exc)
        _redis = False  # sentinel
    return _redis


# ── 文档索引缓存 ──────────────────────────────────────

def cache_doc_index(scope_key: str, file_hash: str, data: dict, ttl: int = 3600) -> None:
    r = _get_redis()
    if not r:
        return
    try:
        key = f"doc:index:{scope_key}:{file_hash}"
        r.hset(key, mapping=data)
        r.expire(key, ttl)
    except Exception as exc:
        logger.warning("Redis cache_doc_index failed: %s", exc)


def get_cached_doc_index(scope_key: str, file_hash: str) -> dict | None:
    r = _get_redis()
    if not r:
        return None
    try:
        key = f"doc:index:{scope_key}:{file_hash}"
        raw = r.hgetall(key)
        return raw if raw else None
    except Exception:
        return None


# ── 分布式锁 ──────────────────────────────────────────

def acquire_ingest_lock(scope_key: str, file_hash: str, job_id: str, ttl: int = 300) -> bool:
    r = _get_redis()
    if not r:
        return True  # 降级：无 Redis 时直接放行
    try:
        key = f"lock:doc_ingest:{scope_key}:{file_hash}"
        return bool(r.set(key, job_id, nx=True, ex=ttl))
    except Exception:
        return True


def release_ingest_lock(scope_key: str, file_hash: str) -> None:
    r = _get_redis()
    if not r:
        return
    try:
        key = f"lock:doc_ingest:{scope_key}:{file_hash}"
        r.delete(key)
    except Exception as exc:
        logger.warning("Redis release lock failed: %s", exc)


# ── Job 状态 ──────────────────────────────────────────

def set_job_status(job_id: str, data: dict, ttl: int = 86400) -> None:
    r = _get_redis()
    if not r:
        return
    try:
        key = f"doc:job:{job_id}"
        r.hset(key, mapping=data)
        r.expire(key, ttl)
    except Exception as exc:
        logger.warning("Redis set_job_status failed: %s", exc)


def get_job_status(job_id: str) -> dict | None:
    r = _get_redis()
    if not r:
        return None
    try:
        key = f"doc:job:{job_id}"
        raw = r.hgetall(key)
        return raw if raw else None
    except Exception:
        return None


# ── Batch 状态 ────────────────────────────────────────

def init_batch(batch_id: str, total: int, ttl: int = 86400) -> None:
    """初始化 batch 计数器。"""
    r = _get_redis()
    if not r:
        return
    try:
        key = f"doc:batch:{batch_id}"
        r.hset(key, mapping={
            "total_count": str(total),
            "queued_count": str(total),
            "processing_count": "0",
            "success_count": "0",
            "cached_count": "0",
            "failed_count": "0",
        })
        r.expire(key, ttl)
    except Exception as exc:
        logger.warning("Redis init_batch failed: %s", exc)


def update_batch_progress(batch_id: str, job_status: str, was_cached: bool) -> None:
    """每完成一个 job，更新 batch 计数。"""
    r = _get_redis()
    if not r:
        return
    try:
        key = f"doc:batch:{batch_id}"
        r.hincrby(key, "queued_count", -1)
        if was_cached:
            r.hincrby(key, "cached_count", 1)
        elif job_status == "indexed":
            r.hincrby(key, "success_count", 1)
        elif job_status == "failed":
            r.hincrby(key, "failed_count", 1)
        elif job_status in ("parsing", "chunking", "indexing"):
            r.hincrby(key, "processing_count", 1)
    except Exception as exc:
        logger.warning("Redis update_batch_progress failed: %s", exc)


def append_batch_item(batch_id: str, item: dict) -> None:
    r = _get_redis()
    if not r:
        return
    try:
        key = f"doc:batch:{batch_id}:items"
        job_id = item.get("job_id", "")
        # 用 Hash 存储，field=job_id，方便后续更新单条状态
        r.hset(key, job_id, json.dumps(item, ensure_ascii=False))
        r.expire(key, 86400)
    except Exception as exc:
        logger.warning("Redis append_batch_item failed: %s", exc)


def update_batch_item_status(batch_id: str, job_id: str, status: str,
                              doc_id: str | None = None, error_message: str | None = None) -> None:
    """更新 batch 中单个 job 的状态字段。"""
    r = _get_redis()
    if not r:
        return
    try:
        key = f"doc:batch:{batch_id}:items"
        raw = r.hget(key, job_id)
        if raw:
            item = json.loads(raw)
            item["status"] = status
            if doc_id:
                item["doc_id"] = doc_id
            if error_message:
                item["error_message"] = error_message
            r.hset(key, job_id, json.dumps(item, ensure_ascii=False))
    except Exception as exc:
        logger.warning("Redis update_batch_item_status failed: %s", exc)


def compute_batch_progress(batch: dict) -> int:
    try:
        total = int(batch.get("total_count", 1))
        done = int(batch.get("success_count", 0)) + int(batch.get("cached_count", 0)) + int(batch.get("failed_count", 0))
        return min(100, max(0, int(done / max(total, 1) * 100)))
    except (ValueError, ZeroDivisionError):
        return 0


def is_batch_terminal(batch: dict) -> bool:
    return (int(batch.get("queued_count", 0)) + int(batch.get("processing_count", 0))) == 0


def get_batch_status(batch_id: str) -> dict | None:
    r = _get_redis()
    if not r:
        return None
    try:
        key = f"doc:batch:{batch_id}"
        raw = r.hgetall(key)
        if not raw:
            return None
        # 从 Hash 读取 item 列表
        items_key = f"{key}:items"
        items_raw = r.hgetall(items_key)
        raw["items"] = [json.loads(v) for v in items_raw.values()] if items_raw else []
        return raw
    except Exception:
        return None
