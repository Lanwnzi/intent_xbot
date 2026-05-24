"""通过工厂按配置创建 Agent（checkpointer、tools、prompt、middleware）。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware
from langchain.agents.middleware.tool_call_limit import ToolCallLimitMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool

from graph.guardian import build_guardian_middleware
from graph.checkpointer import get_checkpointer
from service.prompt_builder import build_system_prompt
from graph.llm import build_llm_config_from_settings, get_llm

CONTRACT_SUB_AGENT_PROMPT = """你是一名合同任务路由器。根据用户请求判断任务类型，路由到正确的工具。

你的核心职责是**路由判断**，不是亲自执行复杂业务。

## 工具清单

- **contract_review**: 审核合同文件，生成审核报告（自包含黑盒：内部完成解析→审核→报告→保存）。返回 report_id、report_path、summary 和 risk_count。
- **document_retrieval**: 在已入库的合同文档中检索具体条款内容。必须带 doc_id 范围过滤。
- **list_documents**: 列出已入库的合同文档及状态。
- **list_contracts**: 列出已上传但未入库的合同文件。

## 路由规则

### 1. 审核请求 → 调 contract_review
关键字：审核、审查、检查合同、帮我看合同有什么问题、合同风险

你只需要做：确认文件路径 → 调 contract_review(file_path, contract_name) → 拿到结果后汇报。
**你自己不参与审核分析**，contract_review 是独立黑盒。

### 2. 细节查询 → 编排 document_retrieval
关键字：违约责任是什么、保密条款怎么写的、付款条款、合同里关于X的规定

你需要做：
- 先调 list_documents 找到 doc_id
- 再调 document_retrieval(query=..., doc_id=...) 检索原文
- 基于检索到的原文片段回答用户问题，引用出处和章节

### 3. 文件列表 → 调 list_contracts 或 list_documents
关键字：有哪些合同、上传了什么文件、库里有什么

## 重要规则
- 审核任务不参与分析，只做路由。文档检索任务可以编排多步工具调用。
- document_retrieval 必须带 doc_id 范围过滤，不能全库搜索。
- 如果工具返回错误，解释原因并给出建议。
"""


def build_contract_sub_agent(base_dir: Path) -> Any:
    """创建合同处理子 Agent，返回 CompiledStateGraph。

    子 Agent 拥有 contract_review、document_retrieval、list_documents、list_contracts 工具，
    在独立上下文中处理合同相关任务。
    """
    from config import get_settings
    from tools.contract_review_tool import ContractReviewTool
    from tools.document_retrieval_tool import DocumentRetrievalTool, DocumentListTool
    from tools.list_contracts_tool import ListContractsTool

    settings = get_settings()
    llm = get_llm(build_llm_config_from_settings(settings, temperature=0.0, streaming=False))

    sub_tools: list[BaseTool] = [
        ContractReviewTool(root_dir=base_dir),
        DocumentRetrievalTool(root_dir=base_dir),
        DocumentListTool(root_dir=base_dir),
        ListContractsTool(root_dir=base_dir),
    ]

    return create_agent(
        model=llm,
        tools=sub_tools,
        system_prompt=CONTRACT_SUB_AGENT_PROMPT,
        middleware=[ToolCallLimitMiddleware(run_limit=10, exit_behavior="end")],
    )

# 兼容 LangGraph 的 CompiledStateGraph 类型
AgentGraph = Any

# 默认：消息数达到 50 触发压缩，保留最近 20 条
DEFAULT_SUMMARIZATION_TRIGGER_MESSAGES = 50
DEFAULT_SUMMARIZATION_KEEP_MESSAGES = 20


def _summarization_trigger_messages() -> int:
    v = os.getenv("SUMMARIZATION_TRIGGER_MESSAGES", "").strip()
    if not v:
        return DEFAULT_SUMMARIZATION_TRIGGER_MESSAGES
    try:
        return max(1, int(v))
    except ValueError:
        return DEFAULT_SUMMARIZATION_TRIGGER_MESSAGES


def _summarization_keep_messages() -> int:
    v = os.getenv("SUMMARIZATION_KEEP_MESSAGES", "").strip()
    if not v:
        return DEFAULT_SUMMARIZATION_KEEP_MESSAGES
    try:
        return max(1, int(v))
    except ValueError:
        return DEFAULT_SUMMARIZATION_KEEP_MESSAGES


@dataclass
class AgentConfig:
    """Agent 构建所需配置。"""

    llm: BaseChatModel
    tools: list[BaseTool]
    system_prompt: str
    checkpointer: Any | None = None
    guardian_enabled: bool = True
    use_summarization: bool = False
    summarization_trigger_messages: int = DEFAULT_SUMMARIZATION_TRIGGER_MESSAGES
    summarization_keep_messages: int = DEFAULT_SUMMARIZATION_KEEP_MESSAGES


def build_agent_config(
    base_dir: Path,
    tools: list[BaseTool],
    *,
    use_checkpointer: bool = True,
    use_summarization: bool | None = None,
) -> AgentConfig:
    """从当前运行配置与 base_dir、tools 构建 AgentConfig。"""
    from config import get_settings

    settings = get_settings()
    prompt = build_system_prompt(base_dir) if base_dir else ""
    llm = get_llm(build_llm_config_from_settings(settings, temperature=0.0, streaming=True))
    checkpointer = get_checkpointer() if use_checkpointer else None
    if use_summarization is None:
        use_summarization = os.getenv("SUMMARIZATION_ENABLED", "false").strip().lower() in ("true", "1", "yes")
    return AgentConfig(
        llm=llm,
        tools=tools,
        system_prompt=prompt,
        checkpointer=checkpointer,
        guardian_enabled=settings.guardian_enabled,
        use_summarization=use_summarization,
        summarization_trigger_messages=_summarization_trigger_messages(),
        summarization_keep_messages=_summarization_keep_messages(),
    )


def create_agent_from_config(config: AgentConfig) -> AgentGraph:
    """根据 AgentConfig 创建带 checkpointer、可选 Guardian / Summarization 的 agent graph。"""
    middleware: list[Any] = []
    if config.guardian_enabled:
        middleware.append(build_guardian_middleware())
    middleware.append(ToolCallLimitMiddleware(run_limit=10, exit_behavior="end"))
    if config.use_summarization:
        middleware.append(
            SummarizationMiddleware(
                model=config.llm,
                trigger=("messages", config.summarization_trigger_messages),
                keep=("messages", config.summarization_keep_messages),
            )
        )
    return create_agent(
        model=config.llm,
        tools=config.tools,
        system_prompt=config.system_prompt,
        checkpointer=config.checkpointer,
        middleware=middleware if middleware else (),
    )

