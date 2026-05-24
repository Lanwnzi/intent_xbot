from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, UploadFile, File
from pydantic import BaseModel, Field

from graph.agent import agent_manager
from service.memory_indexer import memory_indexer
from tools.skills_scanner import refresh_snapshot, scan_skills

router = APIRouter()

UPLOAD_DIR = Path(__file__).resolve().parent.parent / "data" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_UPLOAD_MIME = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
    "text/plain",
    "text/markdown",
    "text/x-markdown",
}

ALLOWED_PREFIXES = (
    "workspace/",
    "memory/",
    "memory_module_v1/long_term_memory/",
    "skills/",
    "knowledge/",
)
ALLOWED_ROOT_FILES = {
    "skills/SKILLS_SNAPSHOT.md",
    "memory_module_v1/long_term_memory/MEMORY.md",
}


class SaveFileRequest(BaseModel):
    path: str = Field(..., min_length=1)
    content: str


_DEFAULT_TEMPLATES: dict[str, str] = {
    "workspace/AGENTS.md": "# Agents Guide\n\n请在此描述工作区中各个 Agent 的职责、风格与使用约定。\n",
    "workspace/IDENTITY.md": "# Workspace Identity\n\n在这里定义此工作区的项目愿景、价值观与整体风格。\n",
    "workspace/SOUL.md": "# Soul\n\n用于描述此 AI 的“灵魂”、长期目标与行为准则。\n",
    "workspace/USER.md": "# User Profile\n\n记录典型用户画像、需求与偏好。\n",
    "memory_module_v1/long_term_memory/MEMORY.md": "# Long-term Memory\n\n在这里追加重要的长期记忆片段。\n",
}


def _resolve_path(relative_path: str) -> Path:
    if agent_manager.base_dir is None:
        raise HTTPException(status_code=503, detail="Agent manager is not initialized")

    normalized = relative_path.replace("\\", "/").strip("/")

    # 兼容旧前端请求：直接请求 "SKILLS_SNAPSHOT.md" 或 "MEMORY.md"
    if normalized == "SKILLS_SNAPSHOT.md":
        normalized = "skills/SKILLS_SNAPSHOT.md"
    elif normalized == "MEMORY.md":
        normalized = "memory_module_v1/long_term_memory/MEMORY.md"

    if normalized not in ALLOWED_ROOT_FILES and not normalized.startswith(ALLOWED_PREFIXES):
        raise HTTPException(status_code=400, detail="Path is not in the editable whitelist")

    candidate = (agent_manager.base_dir / normalized).resolve()
    base_dir = agent_manager.base_dir.resolve()
    if base_dir not in candidate.parents and candidate != base_dir:
        raise HTTPException(status_code=400, detail="Path traversal detected")
    return candidate


@router.get("/files")
async def read_file(path: str = Query(..., min_length=1)) -> dict[str, str]:
    file_path = _resolve_path(path)
    if not file_path.exists():
        # 对关键系统文件提供默认模板，避免前端反复收到 404
        normalized = path.replace("\\", "/").strip("/")
        default_content = _DEFAULT_TEMPLATES.get(normalized)
        if default_content is not None:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(default_content, encoding="utf-8")
        else:
            raise HTTPException(status_code=404, detail="File not found")
    return {
        "path": path.replace("\\", "/"),
        "content": file_path.read_text(encoding="utf-8"),
    }


@router.post("/files")
async def save_file(payload: SaveFileRequest) -> dict[str, Any]:
    file_path = _resolve_path(payload.path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(payload.content, encoding="utf-8")

    normalized = payload.path.replace("\\", "/")
    if normalized in {
        # "memory/MEMORY.md",
        "memory_module_v1/long_term_memory/MEMORY.md",
    }:
        memory_indexer.rebuild_index()
    if normalized.startswith("skills/"):
        refresh_snapshot(agent_manager.base_dir)

    return {"ok": True, "path": normalized}


@router.post("/upload")
async def upload_file(file: UploadFile = File(...)) -> dict[str, Any]:
    """上传合同文件到 data/uploads/，返回文件信息。"""
    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名不能为空")

    mime = file.content_type or "application/octet-stream"
    if mime not in ALLOWED_UPLOAD_MIME:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型：{mime}。支持的类型：PDF、Word、Markdown、纯文本",
        )

    # 同名去重：删除之前上传的同名文件
    for existing in UPLOAD_DIR.iterdir():
        if existing.is_file() and existing.name.endswith(f"_{file.filename}"):
            existing.unlink()

    unique_name = f"{uuid.uuid4().hex[:12]}_{file.filename}"
    save_path = UPLOAD_DIR / unique_name

    with open(save_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    return {
        "ok": True,
        "filename": file.filename,
        "saved_path": str(save_path.resolve()),
        "content_type": mime,
    }


@router.get("/contracts")
async def list_contracts() -> dict[str, list[dict[str, Any]]]:
    """列出 data/uploads/ 中所有已上传的合同文件。"""
    if not UPLOAD_DIR.exists():
        return {"files": []}

    files: list[dict[str, Any]] = []
    for fpath in sorted(UPLOAD_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not fpath.is_file():
            continue
        stat = fpath.stat()
        files.append({
            "filename": fpath.name,
            "path": str(fpath.resolve()),
            "size": stat.st_size,
            "uploaded_at": stat.st_mtime,
        })
    return {"files": files}


@router.delete("/contracts")
async def delete_contract(filename: str = Query(..., min_length=1)) -> dict[str, Any]:
    """删除指定合同文件。"""
    target = UPLOAD_DIR / filename
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"文件不存在: {filename}")
    if not target.is_relative_to(UPLOAD_DIR):
        raise HTTPException(status_code=400, detail="路径越权")
    target.unlink()
    return {"ok": True, "filename": filename}


class IngestRequest(BaseModel):
    source_path: str = Field(..., min_length=1)
    doc_name: str = Field(default="")
    session_id: str | None = None
    batch_id: str | None = None
    project_id: str | None = None
    company_id: str | None = None


@router.post("/documents/ingest")
async def ingest_document(payload: IngestRequest) -> dict[str, Any]:
    """将文档入库：MinerU 解析（PDF/Word）→ 切分 → SQLite + Chroma + FTS5。"""
    from service.document_indexer import document_indexer

    result = document_indexer.ingest(
        source_path=payload.source_path,
        doc_name=payload.doc_name,
        session_id=payload.session_id,
        batch_id=payload.batch_id,
        project_id=payload.project_id,
        company_id=payload.company_id,
    )
    return result


@router.get("/documents")
async def list_documents(
    session_id: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
) -> dict[str, list[dict[str, Any]]]:
    """列出已入库的文档。"""
    from service.document_indexer import document_indexer
    docs = document_indexer.list_documents(session_id=session_id, project_id=project_id)
    return {"documents": docs}


@router.get("/skills")
async def list_skills() -> list[dict[str, str]]:
    if agent_manager.base_dir is None:
        raise HTTPException(status_code=503, detail="Agent manager is not initialized")
    return [skill.__dict__ for skill in scan_skills(agent_manager.base_dir)]
