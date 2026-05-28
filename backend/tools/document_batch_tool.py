"""多文档批量入库 + 进度查询工具。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Type

from langchain_core.callbacks.manager import AsyncCallbackManagerForToolRun, CallbackManagerForToolRun
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from service.batch_ingestor import get_batch_status as query_batch_status
from service.batch_ingestor import start_batch


class DocumentBatchIngestInput(BaseModel):
    source_paths: list[str] = Field(..., description="合同/报告文件绝对路径列表，支持 PDF/Word/Markdown/TXT")
    session_id: str | None = Field(default=None, description="关联会话 ID")
    project_id: str | None = Field(default=None, description="关联项目 ID")


class DocumentBatchIngestTool(BaseTool):
    name: str = "batch_ingest_documents"
    description: str = (
        "批量上传多个合同/报告文件入库。传入文件路径列表，系统自动去重并后台处理。"
        "返回 batch_id 可用于查询处理进度。"
    )
    args_schema: Type[BaseModel] = DocumentBatchIngestInput
    model_config = ConfigDict(arbitrary_types_allowed=True)
    _root_dir: Path = PrivateAttr()

    def __init__(self, root_dir: Path, **kwargs) -> None:
        super().__init__(**kwargs)
        self._root_dir = root_dir.resolve()

    def _run(
        self,
        source_paths: list[str],
        session_id: str | None = None,
        project_id: str | None = None,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        result = start_batch(
            source_paths=source_paths,
            session_id=session_id,
            project_id=project_id,
        )
        return json.dumps(result, ensure_ascii=False, indent=2)

    async def _arun(
        self,
        source_paths: list[str],
        session_id: str | None = None,
        project_id: str | None = None,
        run_manager: AsyncCallbackManagerForToolRun | None = None,
    ) -> str:
        return await asyncio.to_thread(self._run, source_paths, session_id, project_id, None)


class DocumentBatchStatusInput(BaseModel):
    batch_id: str = Field(..., description="批次 ID，如 BATCH-1717000000-a1b2")


class DocumentBatchStatusTool(BaseTool):
    name: str = "batch_ingest_status"
    description: str = "查询批量文档入库的处理进度。传入 batch_id，返回每个文件的处理状态。"
    args_schema: Type[BaseModel] = DocumentBatchStatusInput
    model_config = ConfigDict(arbitrary_types_allowed=True)
    _root_dir: Path = PrivateAttr()

    def __init__(self, root_dir: Path, **kwargs) -> None:
        super().__init__(**kwargs)
        self._root_dir = root_dir.resolve()

    def _run(
        self,
        batch_id: str,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        result = query_batch_status(batch_id)
        return json.dumps(result, ensure_ascii=False, indent=2)

    async def _arun(
        self,
        batch_id: str,
        run_manager: AsyncCallbackManagerForToolRun | None = None,
    ) -> str:
        return await asyncio.to_thread(self._run, batch_id, None)
