from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Type

from langchain_core.callbacks.manager import AsyncCallbackManagerForToolRun, CallbackManagerForToolRun
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from service.document_indexer import document_indexer


class DocumentRetrievalInput(BaseModel):
    query: str = Field(..., description="检索查询，例如：违约责任条款、保密义务")
    doc_id: str | None = Field(default=None, description="指定文档 ID，例如 DOC-20260524-001")
    session_id: str | None = Field(default=None, description="按会话过滤")
    project_id: str | None = Field(default=None, description="按项目过滤")
    company_id: str | None = Field(default=None, description="按公司过滤")
    top_k: int = Field(default=5, ge=1, le=10, description="返回结果数量")


class DocumentRetrievalTool(BaseTool):
    name: str = "document_retrieval"
    description: str = (
        "在已入库的合同/报告文档中执行混合检索（向量 + 全文）。"
        "必须指定检索范围（doc_id / session_id / project_id / company_id 至少一个），不能全库检索。"
        "返回带来源 metadata 的文档片段。"
    )
    args_schema: Type[BaseModel] = DocumentRetrievalInput
    model_config = ConfigDict(arbitrary_types_allowed=True)
    _root_dir: Path = PrivateAttr()

    def __init__(self, root_dir: Path, **kwargs) -> None:
        super().__init__(**kwargs)
        self._root_dir = root_dir.resolve()

    def _run(
        self,
        query: str,
        doc_id: str | None = None,
        session_id: str | None = None,
        project_id: str | None = None,
        company_id: str | None = None,
        top_k: int = 10,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        results = document_indexer.retrieve(
            query=query,
            doc_id=doc_id,
            session_id=session_id,
            project_id=project_id,
            company_id=company_id,
            top_k=top_k,
        )

        if not results:
            return "未找到相关文档内容，请确认检索范围和查询词。"

        if isinstance(results[0], dict) and "error" in results[0]:
            return f"检索失败: {results[0]['error']}"

        lines = [f"检索 '{query}' 共命中 {len(results)} 个片段：", ""]
        for idx, item in enumerate(results, start=1):
            lines.append(f"### [{idx}] {item.get('doc_name', '')}")
            title = item.get("section_title", "")
            if title:
                lines.append(f"**章节**: {title}")
            lines.append(f"**来源**: {item.get('source_path', '')} (chunk #{item.get('chunk_index', 0)})")
            lines.append(f"**相关度**: {item.get('fused_score', 0):.4f}")
            lines.append(f"**chunk_id**: {item.get('chunk_id', '')}")
            lines.append("")
            lines.append(item.get("content", "")[:1500])
            lines.append("")

        return "\n".join(lines)[:6000]

    async def _arun(
        self,
        query: str,
        doc_id: str | None = None,
        session_id: str | None = None,
        project_id: str | None = None,
        company_id: str | None = None,
        top_k: int = 5,
        run_manager: AsyncCallbackManagerForToolRun | None = None,
    ) -> str:
        return await asyncio.to_thread(
            self._run, query, doc_id, session_id, project_id, company_id, top_k, None
        )


class DocumentListInput(BaseModel):
    session_id: str | None = Field(default=None, description="按会话过滤")


class DocumentListTool(BaseTool):
    name: str = "list_documents"
    description: str = (
        "列出所有已入库的合同/报告文档及状态（indexed/failed/ingesting/indexing）。"
        "用于在检索前了解可用文档。"
        "返回结果按最近入库时间倒序排列；当用户没有明确指定文档时，可优先展示前 3 个候选让用户确认。"
    )
    args_schema: Type[BaseModel] = DocumentListInput
    model_config = ConfigDict(arbitrary_types_allowed=True)
    _root_dir: Path = PrivateAttr()

    def __init__(self, root_dir: Path, **kwargs) -> None:
        super().__init__(**kwargs)
        self._root_dir = root_dir.resolve()

    def _run(
        self,
        session_id: str | None = None,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        docs = document_indexer.list_documents(session_id=session_id, status=None)
        if not docs:
            return "当前库中没有任何文档，请先将合同/报告文件入库。"

        status_map = {
            "indexed": "就绪",
            "failed": "失败",
            "indexing": "索引中",
            "ingesting": "处理中",
        }

        lines = [
            "已入库文档列表：",
            "",
            "| 序号 | 文件名 | 文档ID | 状态 |",
            "|---|---|---|---|",
        ]
        for idx, doc in enumerate(docs, start=1):
            status_label = status_map.get(str(doc.get("status", "")), str(doc.get("status", "")))
            lines.append(f"| {idx} | {doc['doc_name']} | {doc['doc_id']} | {status_label} |")

        return "\n".join(lines)

    async def _arun(
        self,
        session_id: str | None = None,
        run_manager: AsyncCallbackManagerForToolRun | None = None,
    ) -> str:
        return await asyncio.to_thread(self._run, session_id, None)
