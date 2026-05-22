"""飞书机器人 WebSocket 客户端 —— 长连接接收飞书事件。

用法:
    python -m feishu.bot
"""

from __future__ import annotations

import asyncio
import logging

import lark_oapi as lark

from feishu.config import get_feishu_config
from feishu.handler import handle_message, start_agent_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# ── Agent 初始化 ──────────────────────────────────


async def _init_agent() -> None:
    """启动时初始化 checkpointer + Agent + 长期记忆。"""
    from graph.checkpointer import init_checkpointer_async
    from graph.agent import agent_manager
    from service.memory_indexer import memory_indexer
    from tools.skills_scanner import refresh_snapshot
    from config import get_settings

    settings = get_settings()
    base_dir = settings.backend_dir

    await init_checkpointer_async()
    refresh_snapshot(base_dir)
    agent_manager.initialize(base_dir)
    memory_indexer.configure(base_dir)
    memory_indexer.rebuild_index()
    logger.info("Agent 初始化完成")


# ── 事件处理 ──────────────────────────────────────


def on_message_received(event: lark.im.v1.P2ImMessageReceiveV1) -> None:
    """当飞书用户给机器人发消息时触发。"""
    handle_message(event)


# ── 主入口 ────────────────────────────────────────


def main() -> None:
    config = get_feishu_config()
    if not config.enabled:
        logger.info("飞书机器人未启用 (FEISHU_BOT_ENABLED=false)")
        return

    asyncio.run(_init_agent())
    start_agent_loop()  # 独立线程的事件循环，避免和 lark SDK 冲突

    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message_received)
        .build()
    )

    client = lark.ws.Client(
        app_id=config.app_id,
        app_secret=config.app_secret,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO,
    )

    logger.info("正在连接飞书 WebSocket ...")
    client.start()


if __name__ == "__main__":
    main()
