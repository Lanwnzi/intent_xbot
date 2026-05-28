"""MinerU PDF/Word 解析共享工具 —— 带 SHA256 缓存，跳过重复解析。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

MINERU_API_URL = os.getenv("MINERU_API_URL", "")
MINERU_TIMEOUT = int(os.getenv("MINERU_TIMEOUT", "600"))
CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "parsed"

MIME_MAP = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def _compute_sha256(file_path: Path) -> str:
    """计算文件的 SHA256 哈希。"""
    sha = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(8192):
            sha.update(chunk)
    return sha.hexdigest()


def _get_cache_path(file_hash: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{file_hash}.md"


def _read_manifest() -> dict[str, str]:
    path = CACHE_DIR / "manifest.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_manifest(manifest: dict[str, str]) -> None:
    (CACHE_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def parse_with_mineru(file_path: Path, file_hash: str | None = None) -> str:
    """调用本地 MinerU API 将 PDF/Word 解析为 Markdown。

    先检查 SHA256 缓存（data/parsed/{hash}.md），命中则跳过解析。
    如果调用方已计算好 file_hash，传入可避免重复 SHA256 计算。
    """
    t_total = time.perf_counter()

    # 计算 hash（外部传入或自动计算），检查缓存
    t_hash = time.perf_counter()
    if file_hash is None:
        file_hash = _compute_sha256(file_path)
    hash_time = time.perf_counter() - t_hash
    cache_path = _get_cache_path(file_hash)

    if cache_path.exists():
        t_read = time.perf_counter()
        cached = cache_path.read_text(encoding="utf-8").strip()
        read_time = time.perf_counter() - t_read
        if cached:
            total = time.perf_counter() - t_total
            logger.info("[mineru] CACHE HIT hash=%s file=%s hash_time=%.2fs read_time=%.2fs total=%.2fs",
                        file_hash[:12], file_path.name, hash_time, read_time, total)
            return cached

    # 缓存未命中，调 MinerU
    logger.info("[mineru] CACHE MISS hash=%s file=%s hash_time=%.2fs", file_hash[:12], file_path.name, hash_time)
    if not MINERU_API_URL:
        raise RuntimeError("MINERU_API_URL 未配置，请在 .env 中设置 MinerU 服务地址")

    ext = file_path.suffix.lower()
    mime = MIME_MAP.get(ext, "application/pdf")

    t_api = time.perf_counter()
    with open(file_path, "rb") as f:
        files = [("files", (file_path.name, f, mime))]
        data = {
            "backend": "pipeline",
            "parse_method": "auto",
            "lang_list": "ch",
            "return_md": "true",
            "return_content_list": "true",
            "start_page_id": "0",
            "end_page_id": "99999",
        }
        vllm_url = os.getenv("MINERU_VLLM_SERVER_URL", "")
        if vllm_url:
            data["server_url"] = vllm_url

        try:
            logger.info("[mineru] request: url=%s file=%s hash=%s", MINERU_API_URL, file_path.name, file_hash[:12])
            response = requests.post(MINERU_API_URL, files=files, data=data, timeout=MINERU_TIMEOUT)
            api_time = time.perf_counter() - t_api
            logger.info("[mineru] response: status=%s size=%d api_time=%.2fs", response.status_code, len(response.content), api_time)
            response.raise_for_status()
        except requests.Timeout:
            raise RuntimeError(f"MinerU 解析超时（{MINERU_TIMEOUT}s），文件可能过大")
        except requests.RequestException as e:
            raise RuntimeError(f"MinerU API 请求失败: {e}")

    result = response.json()
    logger.info("MinerU response keys: %s", list(result.keys()) if isinstance(result, dict) else type(result))

    content_blocks: list[str] = []

    results = result.get("results") if isinstance(result, dict) else None
    if isinstance(results, dict):
        for _fname, fdata in results.items():
            if isinstance(fdata, dict):
                raw_md = fdata.get("md_content", "")
                if raw_md:
                    content_blocks.append(str(raw_md))
                cl = fdata.get("content_list", "")
                if cl:
                    if isinstance(cl, str):
                        try:
                            cl = json.loads(cl)
                        except json.JSONDecodeError:
                            cl = []
                    if isinstance(cl, list):
                        for item in cl:
                            if isinstance(item, dict):
                                text = item.get("text", "")
                                if text:
                                    content_blocks.append(str(text))

    if not content_blocks:
        fallback = result.get("md") or result.get("markdown") or ""
        if fallback:
            content_blocks.append(str(fallback))
        cl = result.get("content_list", [])
        if isinstance(cl, list):
            for item in cl:
                if isinstance(item, dict):
                    t = item.get("text", "") or item.get("md", "")
                    if t:
                        content_blocks.append(str(t))
                elif isinstance(item, str):
                    content_blocks.append(item)

    md_text = "\n\n".join(content_blocks)

    if not md_text.strip():
        logger.error("MinerU returned empty markdown. Full response: %s",
                     json.dumps(result, ensure_ascii=False)[:2000])
        raise RuntimeError("MinerU 解析结果为空，请检查文件内容或 MinerU 服务状态")

    # 写入缓存
    cache_path.write_text(md_text, encoding="utf-8")
    manifest = _read_manifest()
    manifest[file_hash] = file_path.name
    _write_manifest(manifest)
    total = time.perf_counter() - t_total
    logger.info("[mineru] cached: hash=%s file=%s chars=%d hash_time=%.2fs api_time=%.2fs total=%.2fs",
                file_hash[:12], file_path.name, len(md_text), hash_time, api_time, total)

    return md_text.strip()


def compute_file_hash(file_path: Path) -> str:
    """公开方法：计算文件 SHA256，供调用方做去重判断。"""
    return _compute_sha256(file_path)
