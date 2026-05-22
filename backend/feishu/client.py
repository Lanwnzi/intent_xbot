"""飞书 API 客户端 —— 获取 token、发送消息。"""

from __future__ import annotations

import json
import logging

import lark_oapi as lark
from lark_oapi.api.auth.v3 import InternalTenantAccessTokenRequest

from feishu.config import get_feishu_config

logger = logging.getLogger(__name__)


class FeishuClient:
    """封装飞书 HTTP API：获取访问令牌、回复消息。"""

    def __init__(self) -> None:
        self._config = get_feishu_config()
        self._client = lark.Client.builder() \
            .app_id(self._config.app_id) \
            .app_secret(self._config.app_secret) \
            .log_level(lark.LogLevel.WARNING) \
            .build()

    def _get_access_token(self) -> str:
        """获取 tenant_access_token。SDK 内部有缓存，首次请求后自动续期。"""
        req = InternalTenantAccessTokenRequest.builder() \
            .request_body(lark.auth.v3.InternalTenantAccessTokenRequestBody \
                .builder().app_id(self._config.app_id).app_secret(self._config.app_secret).build()) \
            .build()
        resp = self._client.auth.v3.tenant_access_token.internal(req)
        if not resp.success():
            raise RuntimeError(f"获取飞书 access_token 失败: {resp.code} {resp.msg}")
        return resp.data.tenant_access_token

    def reply_text(self, message_id: str, text: str) -> bool:
        """回复一条文本消息。返回 True/False 表示是否发送成功。"""
        content = lark.im.v1.ReplyMessageRequestBody \
            .builder() \
            .msg_type("text") \
            .content(json.dumps({"text": text})) \
            .build()
        req = lark.im.v1.ReplyMessageRequest \
            .builder() \
            .message_id(message_id) \
            .request_body(content) \
            .build()

        resp = self._client.im.v1.message.reply(req)
        if not resp.success():
            logger.error("飞书回复失败: code=%s msg=%s", resp.code, resp.msg)
            return False
        logger.info("回复成功 → message_id=%s", resp.data.message_id or "?")
        return True


# 模块级单例
feishu_client = FeishuClient()
