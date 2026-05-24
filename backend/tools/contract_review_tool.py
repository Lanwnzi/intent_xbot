"""合同审核工具 —— 自包含黑盒：MinerU 解析 → LLM 七维度审核 → 生成报告 → 保存。

子 Agent 将此工具作为黑盒调用，不参与审核逻辑。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Type

from langchain_core.callbacks.manager import AsyncCallbackManagerForToolRun, CallbackManagerForToolRun
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from tools.contract_review_prompt import CONTRACT_REVIEW_SYSTEM_PROMPT, CONTRACT_REVIEW_USER_TEMPLATE

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "reports"
SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc", ".md", ".txt"}


class ContractReviewInput(BaseModel):
    file_path: str = Field(..., description="合同文件的绝对路径，支持 PDF / Word / Markdown / 纯文本")
    contract_name: str = Field(default="", description="合同名称，为空时自动从文件名提取")


class ContractReviewTool(BaseTool):
    """合同审核工具（自包含黑盒）。

    完整流程：文件 → MinerU 解析 → LLM 七维度审核 → 生成报告 → 保存到 data/reports/。
    调用者（子 Agent）只需要传入文件路径，拿到报告路径和风险摘要。
    """

    name: str = "contract_review"
    description: str = (
        "审核合同文件并生成审核报告。内部自动完成文件解析、风险分析、报告保存。"
        "返回 report_id、report_path、summary 和 risk_count。"
        "支持 PDF、Word、Markdown 和纯文本格式。"
        "注意：文件解析可能需要数分钟（取决于文件大小和 MinerU 服务速度）。"
    )
    args_schema: Type[BaseModel] = ContractReviewInput
    model_config = ConfigDict(arbitrary_types_allowed=True)
    _root_dir: Path = PrivateAttr()

    def __init__(self, root_dir: Path, **kwargs) -> None:
        super().__init__(**kwargs)
        self._root_dir = root_dir.resolve()
        DATA_DIR.mkdir(parents=True, exist_ok=True)

    # ── 路径安全校验 ──────────────────────────────────────

    def _resolve_path(self, path_str: str) -> Path:
        candidate = Path(path_str).resolve()
        if self._root_dir not in candidate.parents and candidate != self._root_dir:
            raise ValueError(f"路径安全校验失败：文件必须在项目目录内。got: {path_str}")
        return candidate

    # ── 格式判断 ──────────────────────────────────────────

    def _file_ext(self, path: Path) -> str:
        return path.suffix.lower()

    def _needs_mineru(self, ext: str) -> bool:
        return ext in {".pdf", ".docx", ".doc"}

    def _parse_with_mineru(self, file_path: Path) -> str:
        from service.mineru_parser import parse_with_mineru
        return parse_with_mineru(file_path)

    # ── LLM 审核 ─────────────────────────────────────────

    def _build_review_llm(self):
        from config import get_settings
        from graph.llm import build_llm_config_from_settings, get_llm

        settings = get_settings()
        llm_config = build_llm_config_from_settings(settings, temperature=0.0, streaming=False)
        return get_llm(llm_config)

    def _call_review_llm(self, contract_name: str, contract_md: str) -> dict:
        llm = self._build_review_llm()
        user_prompt = CONTRACT_REVIEW_USER_TEMPLATE.format(
            contract_name=contract_name,
            contract_markdown=contract_md[:30000],
        )

        # 第一次尝试
        try:
            response = llm.invoke(
                [
                    {"role": "system", "content": CONTRACT_REVIEW_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ]
            )
            return self._parse_llm_response(str(response.content))
        except (json.JSONDecodeError, ValueError):
            pass

        # 重试：加强格式约束
        try:
            retry_prompt = user_prompt + "\n\n⚠️ 你上次输出的不是合法 JSON。请这次严格只输出 JSON，不要有任何解释。"
            response = llm.invoke(
                [
                    {"role": "system", "content": CONTRACT_REVIEW_SYSTEM_PROMPT},
                    {"role": "user", "content": retry_prompt},
                ]
            )
            return self._parse_llm_response(str(response.content))
        except (json.JSONDecodeError, ValueError):
            pass

        # 降级：返回原始输出
        raw_text = str(response.content) if response else "LLM 审核失败"
        return {
            "risks": [],
            "overall_assessment": raw_text[:2000],
            "degraded": True,
        }

    def _parse_llm_response(self, raw: str) -> dict:
        cleaned = raw.strip()
        json_match = re.search(r"\{[\s\S]*\}", cleaned)
        if json_match:
            cleaned = json_match.group(0)
        return json.loads(cleaned)

    # ── 报告生成 ─────────────────────────────────────────

    def _generate_report(
        self,
        contract_name: str,
        review_result: dict,
        report_id: str,
    ) -> str:
        risks = review_result.get("risks", [])
        assessment = review_result.get("overall_assessment", "")
        degraded = review_result.get("degraded", False)

        level_label = {"high": "🔴 高风险", "medium": "🟡 中风险", "low": "🟢 低风险"}
        risk_count = {"high": 0, "medium": 0, "low": 0}
        for r in risks:
            level = r.get("risk_level", "low")
            if level in risk_count:
                risk_count[level] += 1

        lines = [
            f"# {contract_name} 审核报告",
            "",
            f"**报告编号**: {report_id}",
            f"**审核时间**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
            f"**审核维度**: 合同主体、付款条款、交付验收、违约责任、解除条款、保密条款、争议解决",
            "",
        ]

        if degraded:
            lines.append("> ⚠️ 结构化审核失败，以下为 LLM 原始输出，仅供参考。")
            lines.append("")
            lines.append(assessment)
            return "\n".join(lines)

        lines.append(f"## 风险点清单（共 {len(risks)} 项）")
        lines.append("")
        lines.append("| # | 风险类型 | 风险等级 | 原文证据 | 问题说明 | 修改建议 |")
        lines.append("|---|---|---|---|---|---|")
        for idx, risk in enumerate(risks, start=1):
            rtype = risk.get("risk_type", "")
            rlevel = level_label.get(risk.get("risk_level", "low"), risk.get("risk_level", ""))
            evid = risk.get("evidence", "").replace("|", "\\|").replace("\n", " ")
            issue = risk.get("issue", "").replace("|", "\\|").replace("\n", " ")
            sugg = risk.get("suggestion", "").replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {idx} | {rtype} | {rlevel} | {evid} | {issue} | {sugg} |")

        lines.append("")
        lines.append("## 风险统计")
        total_desc_parts = []
        if risk_count["high"]:
            total_desc_parts.append(f"高风险 {risk_count['high']} 项")
        if risk_count["medium"]:
            total_desc_parts.append(f"中风险 {risk_count['medium']} 项")
        if risk_count["low"]:
            total_desc_parts.append(f"低风险 {risk_count['low']} 项")
        total_desc = "、".join(total_desc_parts) if total_desc_parts else "未发现明显风险"
        lines.append(f"- 共发现 {len(risks)} 个风险点：{total_desc}")
        lines.append("")
        lines.append("## 整体评估")
        lines.append("")
        lines.append(assessment)
        lines.append("")

        return "\n".join(lines)

    def _generate_report_id(self) -> str:
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        existing = sorted(DATA_DIR.glob(f"CR-{today}-*.md"))
        seq = len(existing) + 1
        return f"CR-{today}-{seq:03d}"

    # ── 工具主入口 ───────────────────────────────────────

    def _run(
        self,
        file_path: str,
        contract_name: str = "",
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        # Step 0: 参数校验
        try:
            path = self._resolve_path(file_path)
        except ValueError as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

        ext = self._file_ext(path)
        if ext not in SUPPORTED_EXTENSIONS:
            return json.dumps(
                {"error": f"不支持的文件格式：{ext}，支持的格式：{', '.join(sorted(SUPPORTED_EXTENSIONS))}"},
                ensure_ascii=False,
            )
        if not path.exists():
            return json.dumps({"error": f"文件不存在：{file_path}"}, ensure_ascii=False)
        if not contract_name:
            contract_name = path.stem

        # Step 1: 文件 → 文本
        try:
            if self._needs_mineru(ext):
                contract_md = self._parse_with_mineru(path)
            else:
                contract_md = path.read_text(encoding="utf-8").strip()
                if not contract_md:
                    return json.dumps({"error": "文件内容为空"}, ensure_ascii=False)
        except RuntimeError as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

        # Step 2: LLM 审核
        try:
            review_result = self._call_review_llm(contract_name, contract_md)
        except Exception as exc:
            return json.dumps({"error": f"LLM 审核调用失败: {exc}"}, ensure_ascii=False)

        # Step 3: 生成报告并保存
        report_id = self._generate_report_id()
        report_content = self._generate_report(contract_name, review_result, report_id)
        safe_name = re.sub(r"[\\/:*?\"<>|]", "_", contract_name)[:50]
        report_filename = f"{report_id}_{safe_name}_审核报告.md"
        report_path = DATA_DIR / report_filename
        report_path.write_text(report_content, encoding="utf-8")

        # Step 4: 统计 + 返回
        risks = review_result.get("risks", [])
        risk_count = {"high": 0, "medium": 0, "low": 0}
        for r in risks:
            level = r.get("risk_level", "low")
            if level in risk_count:
                risk_count[level] += 1

        total = len(risks)
        summary_parts = [f"共发现 {total} 个风险点"]
        if risk_count["high"]:
            summary_parts.append(f"高风险 {risk_count['high']} 项")
        if risk_count["medium"]:
            summary_parts.append(f"中风险 {risk_count['medium']} 项")
        if risk_count["low"]:
            summary_parts.append(f"低风险 {risk_count['low']} 项")
        summary = "，".join(summary_parts) + "。"

        return json.dumps(
            {
                "report_id": report_id,
                "report_path": str(report_path.relative_to(self._root_dir)).replace("\\", "/"),
                "summary": summary,
                "risk_count": risk_count,
            },
            ensure_ascii=False,
        )

    async def _arun(
        self,
        file_path: str,
        contract_name: str = "",
        run_manager: AsyncCallbackManagerForToolRun | None = None,
    ) -> str:
        return await asyncio.to_thread(self._run, file_path, contract_name, None)
