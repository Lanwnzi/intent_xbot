"""消息事件处理 —— 解析飞书事件 + 调 Agent 回复。"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from dataclasses import dataclass

import lark_oapi as lark

from feishu.client import feishu_client

logger = logging.getLogger(__name__)

# 独立线程 + 独立事件循环跑 Agent（避免和 lark SDK 内部事件循环冲突）
_agent_loop: asyncio.AbstractEventLoop | None = None
_loop_ready = threading.Event()


@dataclass(frozen=True)
class MessageEvent:
    sender_id: str
    chat_id: str
    message_id: str
    message_type: str
    content_raw: str
    thread_id: str


# ── 启动（bot.py 调一次）─────────────────────────


def start_agent_loop() -> None:
    """在独立线程中启动 Agent 事件循环。"""
    global _agent_loop

    def _run() -> None:
        global _agent_loop
        _agent_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_agent_loop)
        _loop_ready.set()
        _agent_loop.run_forever()

    t = threading.Thread(target=_run, daemon=True, name="feishu-agent")
    t.start()
    _loop_ready.wait(timeout=5)
    logger.info("Agent 事件循环线程已启动")


# ── 公开入口 ──────────────────────────────────────


def handle_message(event: lark.im.v1.P2ImMessageReceiveV1) -> MessageEvent | None:
    parsed = _parse_event(event)
    if parsed is None:
        return None

    text = _extract_text(parsed.content_raw, parsed.message_type)
    if not text:
        return None

    logger.info("Calling agent for: %s", text[:40])
    try:
        reply = _call_agent_sync(chat_id=parsed.chat_id, user_text=text)
        ok = feishu_client.reply_text(parsed.message_id, reply)
        logger.info("Agent reply: %s (sent=%s)", reply[:80], ok)
    except Exception as exc:
        logger.exception("Agent call failed")
        feishu_client.reply_text(parsed.message_id, f"error: {exc}")

    return parsed


# ── Agent 调用 ─────────────────────────────────────


def _call_agent_sync(*, chat_id: str, user_text: str) -> str:
    """把 Agent 协程投递到独立事件循环，阻塞等待结果。"""
    _loop_ready.wait(timeout=5)
    assert _agent_loop is not None
    future = asyncio.run_coroutine_threadsafe(
        _agent_reply(chat_id=chat_id, user_text=user_text),
        _agent_loop,
    )
    return future.result(timeout=120)


async def _agent_reply(*, chat_id: str, user_text: str) -> str:
    """调用 AgentManager.astream()，收集完整回复。"""
    from graph.agent import agent_manager
    from graph.context import RequestContext

    parts: list[str] = []
    done_content = ""
    ctx = RequestContext(thread_id=chat_id)

    try:
        async for evt in agent_manager.astream(
            message=user_text,
            history=[],
            context=ctx,
        ):
            if evt["type"] == "token":
                parts.append(str(evt.get("content", "")))
            elif evt["type"] == "done":
                done_content = str(evt.get("content", ""))
    except Exception as exc:
        logger.exception("Agent stream failed")
        return f"Agent error: {exc}"

    return "".join(parts).strip() or done_content.strip() or "[no response]"


# ── 内部 ───────────────────────────────────────────


def _parse_event(event: lark.im.v1.P2ImMessageReceiveV1) -> MessageEvent | None:
    evt = event.event
    if evt is None or evt.message is None:
        return None

    msg = evt.message
    sender = evt.sender

    sender_id = ""
    if sender and sender.sender_id:
        sender_id = sender.sender_id.open_id or ""

    return MessageEvent(
        sender_id=sender_id,
        chat_id=msg.chat_id or "",
        message_id=msg.message_id or "",
        message_type=msg.message_type or "",
        content_raw=msg.content or "",
        thread_id=msg.thread_id or "",
    )


def _extract_text(content_raw: str, message_type: str) -> str:
    if message_type != "text":
        return ""
    try:
        return json.loads(content_raw).get("text", "")
    except (json.JSONDecodeError, TypeError):
        return content_raw
