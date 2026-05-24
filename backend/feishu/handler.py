"""消息事件处理 —— 解析飞书消息 → 调 Agent → 回复。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import lark_oapi as lark

from feishu import agent_bridge
from feishu.client import feishu_client

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MessageEvent:
    sender_id: str
    chat_id: str
    message_id: str
    message_type: str
    content_raw: str
    thread_id: str


# ── 入口 ───────────────────────────────────────────


def handle_message(event: lark.im.v1.P2ImMessageReceiveV1) -> MessageEvent | None:
    parsed = _parse_event(event)
    if parsed is None:
        return None

    text = _extract_text(parsed.content_raw, parsed.message_type)
    if not text:
        return None

    reply = agent_bridge.process(chat_id=parsed.chat_id, user_text=text)
    feishu_client.reply_text(parsed.message_id, reply)
    logger.info("Agent reply: %s", reply[:80])
    return parsed


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
