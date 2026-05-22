"""飞书机器人配置 —— 从 .env 读取。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv


def _load_dotenv() -> None:
    config_dir = Path(__file__).resolve().parent.parent / "config"
    load_dotenv(config_dir / ".env")


@dataclass(frozen=True)
class FeishuConfig:
    app_id: str
    app_secret: str
    enabled: bool = True


@lru_cache(maxsize=1)
def get_feishu_config() -> FeishuConfig:
    _load_dotenv()

    enabled = (os.getenv("FEISHU_BOT_ENABLED") or "true").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    app_id = (os.getenv("FEISHU_APP_ID") or "").strip()
    app_secret = (os.getenv("FEISHU_APP_SECRET") or "").strip()

    if not app_id or not app_secret:
        raise RuntimeError("FEISHU_APP_ID 和 FEISHU_APP_SECRET 必须在 .env 中配置。")

    return FeishuConfig(
        app_id=app_id,
        app_secret=app_secret,
        enabled=enabled,
    )
