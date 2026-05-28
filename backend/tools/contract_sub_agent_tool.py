"""合同处理子 Agent 工具。

将合同处理专家子 Agent 包装成主 Agent 可调用的工具。
主 Agent 在用户提到合同相关问题时调用此工具，
子 Agent 内部自行决定调用 contract_review / document_retrieval / list_contracts 等。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Type

from langchain_core.callbacks.manager import AsyncCallbackManagerForToolRun
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from graph.agent_factory import build_contract_sub_agent

logger = logging.getLogger(__name__)


class ContractSubAgentInput(BaseModel):
    task: str = Field(
        ...,
        description="合同处理任务描述，包含用户的原始需求和必要的上下文（文件路径、doc_id 等）",
    )


class ContractSubAgentTool(BaseTool):
    """合同处理专家子 Agent — 包装为工具供主 Agent 调用。"""

    name: str = "contract_expert"
    description: str = (
        "合同处理专家，负责所有合同相关任务："
        "1 审核合同并生成审核报告（用户说'审核合同'时使用）；"
        "2 检索合同文档内容（用户问'合同里关于X的条款'时使用）；"
        "3 列出可用的合同文件。"
        "传入完整的用户需求作为 task 参数，子 Agent 会自动选择合适工具执行。"
    )
    args_schema: Type[BaseModel] = ContractSubAgentInput
    model_config = ConfigDict(arbitrary_types_allowed=True)
    _root_dir: Path = PrivateAttr()
    _sub_agent: Any = PrivateAttr(default=None)

    def __init__(self, root_dir: Path, **kwargs) -> None:
        super().__init__(**kwargs)
        self._root_dir = root_dir.resolve()

    def _get_sub_agent(self) -> Any:
        if self._sub_agent is None:
            self._sub_agent = build_contract_sub_agent(self._root_dir)
        return self._sub_agent

    def _run(self, task: str, run_manager=None) -> str:
        raise NotImplementedError("ContractSubAgentTool only supports async invocation")

    async def _arun(
        self,
        task: str,
        run_manager: AsyncCallbackManagerForToolRun | None = None,
    ) -> str:
        sub_agent = self._get_sub_agent()

        try:
            result = await sub_agent.ainvoke(
                {"messages": [HumanMessage(content=task)]},
                config={"recursion_limit": 25},
            )
        except Exception as exc:
            logger.exception("Contract sub-agent invocation failed")
            return f"合同处理专家执行失败: {exc}"

        # 提取最终回复
        messages = result.get("messages", [])
        for msg in reversed(messages):
            content = getattr(msg, "content", "") or ""
            if content and not getattr(msg, "tool_calls", None):
                return str(content)

        return "合同处理专家已完成任务，但未生成文字回复。"
