"""合同审查快速通道 API —— 前端 [合同审查] 按钮直接调用，绕过 Agent 链路。"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from service.mineru_parser import parse_with_mineru
from tools.contract_review_prompt import CONTRACT_REVIEW_SYSTEM_PROMPT, CONTRACT_REVIEW_USER_TEMPLATE

logger = logging.getLogger(__name__)

router = APIRouter()
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
REPORTS_DIR = DATA_DIR / "reports"

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc", ".md", ".txt"}


class ReviewRequest(BaseModel):
    file_path: str = Field(..., min_length=1, description="合同文件绝对路径")
    contract_name: str = Field(default="", description="合同名称，为空时从文件名提取")


class Timings:
    """耗时统计容器。"""
    def __init__(self) -> None:
        self.data: dict[str, float] = {}
        self._start: dict[str, float] = {}

    def start(self, name: str) -> None:
        self._start[name] = time.perf_counter()

    def stop(self, name: str) -> float:
        elapsed = time.perf_counter() - self._start.pop(name, 0)
        self.data[name] = elapsed
        return elapsed

    def summary(self, total: float) -> str:
        parts = [f"{k}={v:.2f}s" for k, v in self.data.items()]
        parts.append(f"total={total:.2f}s")
        return " | ".join(parts)


# ── LLM 审核（与 contract_review_tool 共用逻辑）────────────────

def _parse_llm_response(raw: str) -> dict:
    cleaned = raw.strip()
    json_match = re.search(r"\{[\s\S]*\}", cleaned)
    if json_match:
        cleaned = json_match.group(0)
    return json.loads(cleaned)


def _call_review_llm(contract_name: str, contract_md: str, timings: Timings) -> dict:
    timings.start("llm_build")
    from config import get_settings
    from graph.llm import build_llm_config_from_settings, get_llm
    settings = get_settings()
    llm_config = build_llm_config_from_settings(settings, temperature=0.0, streaming=False)
    llm = get_llm(llm_config)
    timings.stop("llm_build")

    user_prompt = CONTRACT_REVIEW_USER_TEMPLATE.format(
        contract_name=contract_name,
        contract_markdown=contract_md[:30000],
    )
    timings.start("llm_invoke")
    try:
        response = llm.invoke([
            {"role": "system", "content": CONTRACT_REVIEW_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ])
        timings.stop("llm_invoke")
        return _parse_llm_response(str(response.content))
    except (json.JSONDecodeError, ValueError):
        pass

    try:
        retry_prompt = user_prompt + "\n\n⚠️ 你上次输出的不是合法 JSON。请这次严格只输出 JSON。"
        response = llm.invoke([
            {"role": "system", "content": CONTRACT_REVIEW_SYSTEM_PROMPT},
            {"role": "user", "content": retry_prompt},
        ])
        timings.stop("llm_invoke")
        return _parse_llm_response(str(response.content))
    except (json.JSONDecodeError, ValueError):
        pass

    timings.stop("llm_invoke")
    raw_text = str(response.content) if response else "LLM 审核失败"
    return {"risks": [], "overall_assessment": raw_text[:2000], "degraded": True}


def _generate_report(contract_name: str, review_result: dict, report_id: str) -> str:
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
    parts = []
    if risk_count["high"]: parts.append(f"高风险 {risk_count['high']} 项")
    if risk_count["medium"]: parts.append(f"中风险 {risk_count['medium']} 项")
    if risk_count["low"]: parts.append(f"低风险 {risk_count['low']} 项")
    desc = "、".join(parts) if parts else "未发现明显风险"
    lines.append(f"- 共发现 {len(risks)} 个风险点：{desc}")
    lines.append("")
    lines.append("## 整体评估")
    lines.append("")
    lines.append(assessment)
    lines.append("")
    return "\n".join(lines)


def _generate_report_id() -> str:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    existing = sorted(REPORTS_DIR.glob(f"CR-{today}-*.md"))
    return f"CR-{today}-{len(existing) + 1:03d}"


# ── API ────────────────────────────────────────────────
@router.post("/contracts/review")
async def review_contract(payload: ReviewRequest) -> dict[str, Any]:
    """快速通道：解析 → LLM 审核 → 报告 → 异步入库。绕过 Agent 链路。"""
    T = Timings()
    T.start("total")

    path = Path(payload.file_path).resolve()
    if not path.exists():
        raise HTTPException(status_code=400, detail=f"文件不存在: {payload.file_path}")

    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"不支持的文件格式：{ext}")

    contract_name = payload.contract_name or path.stem

    # Step 1: 文件 → 文本
    T.start("parse_or_read")
    try:
        if ext in {".pdf", ".docx", ".doc"}:
            markdown = parse_with_mineru(path)
        else:
            markdown = path.read_text(encoding="utf-8").strip()
            if not markdown:
                raise HTTPException(status_code=400, detail="文件内容为空")
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    parse_time = T.stop("parse_or_read")
    logger.info("[fast-track] parse_or_read=%.2fs chars=%d", parse_time, len(markdown))

    # Step 2: LLM 审核（仅一次调用，含 build + invoke）
    T.start("llm_total")
    try:
        review_result = _call_review_llm(contract_name, markdown, T)
    except Exception as exc:
        logger.exception("[fast-track] LLM review failed")
        raise HTTPException(status_code=500, detail=f"审核失败: {exc}")
    llm_total = T.stop("llm_total")
    logger.info("[fast-track] llm_total=%.2fs (build=%.2fs invoke=%.2fs)",
                llm_total, T.data.get("llm_build", 0), T.data.get("llm_invoke", 0))

    # Step 3: 代码模板渲染报告（不调 LLM）
    T.start("report_render")
    report_id = _generate_report_id()
    report_content = _generate_report(contract_name, review_result, report_id)
    safe_name = re.sub(r"[\\/:*?\"<>|]", "_", contract_name)[:50]
    render_time = T.stop("report_render")
    logger.info("[fast-track] report_render=%.2fs", render_time)

    # Step 4: 保存报告
    T.start("report_save")
    report_path = REPORTS_DIR / f"{report_id}_{safe_name}_审核报告.md"
    report_path.write_text(report_content, encoding="utf-8")
    save_time = T.stop("report_save")
    logger.info("[fast-track] report_save=%.2fs", save_time)

    # Step 5: 异步入库（fire-and-forget，不等）
    try:
        import threading
        def _ingest():
            try:
                from service.document_indexer import document_indexer
                document_indexer.ingest(str(path.resolve()), doc_name=contract_name)
            except Exception as exc:
                logger.warning("[fast-track] Background ingest failed: %s", exc)
        threading.Thread(target=_ingest, daemon=True).start()
    except Exception as exc:
        logger.warning("[fast-track] Failed to start background ingest: %s", exc)

    # Step 6: 统计（纯代码，不计时）
    risks = review_result.get("risks", [])
    risk_count = {"high": 0, "medium": 0, "low": 0}
    for r in risks:
        level = r.get("risk_level", "low")
        if level in risk_count:
            risk_count[level] += 1

    total = len(risks)
    summary_parts = [f"共发现 {total} 个风险点"]
    if risk_count["high"]: summary_parts.append(f"高风险 {risk_count['high']} 项")
    if risk_count["medium"]: summary_parts.append(f"中风险 {risk_count['medium']} 项")
    if risk_count["low"]: summary_parts.append(f"低风险 {risk_count['low']} 项")
    summary = "，".join(summary_parts) + "。"

    total_time = T.stop("total")
    logger.info("[fast-track] ⏱️ TIMING: %s", T.summary(total_time))

    return {
        "report_id": report_id,
        "report_path": str(report_path),
        "summary": summary,
        "risk_count": risk_count,
        "contract_name": contract_name,
    }
