"""Agent 调用桥接层 —— 独立线程 + 独立事件循环，把 async Agent 包装成同步接口。

为什么需要独立线程的事件循环：
  lark SDK 的 WebSocket 在主线程里跑了自己的事件循环。
  agent_manager.astream() 是 async 异步生成器，必须在事件循环里运行。
  主线程已被占用 → 另起线程 + 新事件循环 → 协程投递过去跑。

用法:
  agent_bridge.start()                         # 启动时调一次
  reply = agent_bridge.process(chat_id, text)   # 收到消息时调，阻塞等返回
"""

from __future__ import annotations

import asyncio
import logging
import threading

from graph.agent import agent_manager
from graph.context import RequestContext

logger = logging.getLogger(__name__)

_agent_loop: asyncio.AbstractEventLoop | None = None
_loop_ready = threading.Event()


# ── 公开 API ──────────────────────────────────────


def start() -> None:
    """启动后台线程：初始化 Agent + 跑事件循环。bot.py 启动时调一次。"""
    global _agent_loop

    def _run() -> None:
        global _agent_loop
        _agent_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_agent_loop)

        # 在事件循环里完成 Agent 初始化（checkpointer / tools / skills / memory）
        _agent_loop.run_until_complete(_init_agent())

        _loop_ready.set()
        _agent_loop.run_forever()

    t = threading.Thread(target=_run, daemon=True, name="agent-bridge")
    t.start()
    _loop_ready.wait(timeout=30)
    logger.info("Agent bridge started")


def process(chat_id: str, user_text: str, *, timeout: float = 120) -> str:
    """同步入口：传入 chat_id 和用户文本，返回 Agent 的完整回复。

    内部把 _call_agent 协程投递到独立事件循环，阻塞等待结果。
    """
    _loop_ready.wait(timeout=5)
    assert _agent_loop is not None, "agent_bridge.start() must be called first"

    future = asyncio.run_coroutine_threadsafe(
        _call_agent(chat_id=chat_id, user_text=user_text),
        _agent_loop,
    )
    return future.result(timeout=timeout)


# ── 内部 ───────────────────────────────────────────


async def _init_agent() -> None:
    """初始化 checkpointer + tools + skills + memory（和 app.py lifespan 一致）。"""
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
    logger.info("Agent initialized")


async def _call_agent(*, chat_id: str, user_text: str) -> str:
    """异步核心：调 agent.astream()，收集所有 token + done 兜底。"""
    tokens: list[str] = []
    done_text = ""
    ctx = RequestContext(thread_id=chat_id)

    try:
        async for evt in agent_manager.astream(
            message=user_text,
            history=[],
            context=ctx,
        ):
            if evt["type"] == "token":
                tokens.append(str(evt.get("content", "")))
            elif evt["type"] == "done":
                done_text = str(evt.get("content", ""))
    except Exception as exc:
        logger.exception("Agent stream failed")
        return f"Agent error: {exc}"

    return "".join(tokens).strip() or done_text.strip() or "[no response]"
