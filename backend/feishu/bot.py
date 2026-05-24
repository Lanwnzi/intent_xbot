"""飞书机器人 WebSocket 客户端 —— 长连接接收飞书事件。

用法:
    python -m feishu.bot
"""

from __future__ import annotations

import logging

import lark_oapi as lark

from feishu import agent_bridge
from feishu.config import get_feishu_config
from feishu.handler import handle_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def on_message_received(event: lark.im.v1.P2ImMessageReceiveV1) -> None:
    """当飞书用户给机器人发消息时触发。"""
    handle_message(event)


def main() -> None:
    config = get_feishu_config()
    if not config.enabled:
        logger.info("飞书机器人未启用 (FEISHU_BOT_ENABLED=false)")
        return

    agent_bridge.start()  # 后台线程：初始化 Agent + 事件循环

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
