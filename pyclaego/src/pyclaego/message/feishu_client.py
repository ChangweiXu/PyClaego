"""飞书消息发送客户端 - 封装飞书 REST API 调用与 token 自动刷新"""

import asyncio
import json
import time
from typing import Any

import aiohttp

from ..logging import get_running_log

_rlog = get_running_log()

# 飞书开放平台 API 根地址
_BASE_URL = "https://open.feishu.cn/open-apis"


class FeishuClient:
    """飞书 REST API 客户端

    功能：
    - tenant_access_token 自动获取与刷新（有效期 2 小时，提前 5 分钟刷新）
    - 发送文本消息（单聊 / 群聊）
    - 发送富文本消息（post 类型）
    - 发送卡片消息（interactive 类型）
    - 批量发送消息（多用户 / 多部门）
    - 回复消息（在线程中回复）
    """

    def __init__(self, app_id: str, app_secret: str) -> None:
        """初始化飞书客户端

        Args:
            app_id: 飞书应用 App ID
            app_secret: 飞书应用 App Secret
        """
        self._app_id = app_id
        self._app_secret = app_secret
        self._token: str | None = None
        self._token_expire_at: float = 0.0
        self._token_lock = asyncio.Lock()
        self._session: aiohttp.ClientSession | None = None

    # ------------------------------------------------------------------
    # 内部 HTTP 工具方法
    # ------------------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        """获取复用的 aiohttp Session"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Content-Type": "application/json; charset=utf-8"}
            )
        return self._session

    async def _get_token(self) -> str:
        """获取 tenant_access_token，过期前 5 分钟自动刷新

        Returns:
            有效的 tenant_access_token 字符串
        """
        async with self._token_lock:
            now = time.time()
            # 有效期还剩 5 分钟以上则直接返回
            if self._token and now < self._token_expire_at - 300:
                return self._token

            url = f"{_BASE_URL}/auth/v3/tenant_access_token/internal"
            payload = {"app_id": self._app_id, "app_secret": self._app_secret}
            session = await self._get_session()
            try:
                async with session.post(url, json=payload) as resp:
                    data = await resp.json()
                    if data.get("code") != 0:
                        raise RuntimeError(
                            f"[FeishuClient] 获取 token 失败: code={data.get('code')}, msg={data.get('msg')}"
                        )
                    self._token = data["tenant_access_token"]
                    self._token_expire_at = now + data.get("expire", 7200)
                    _rlog.info("feishu", "[FeishuClient] tenant_access_token 已刷新")
                    return self._token
            except Exception as e:
                _rlog.error("feishu", f"[FeishuClient] 刷新 token 异常: {e}")
                raise

    async def _post(self, path: str, payload: dict[str, Any], *, params: dict | None = None) -> dict:
        """通用 POST 请求（自动带 Bearer token）

        Args:
            path: API 路径（不含 BASE_URL）
            payload: 请求体 dict
            params: URL 查询参数

        Returns:
            响应 JSON dict
        """
        token = await self._get_token()
        url = f"{_BASE_URL}{path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        session = await self._get_session()
        try:
            async with session.post(url, json=payload, headers=headers, params=params) as resp:
                data = await resp.json(content_type=None)
                _rlog.info(
                    "feishu",
                    f"[FeishuClient] POST {path} -> code={data.get('code')}, msg={data.get('msg', 'ok')}",
                )
                return data
        except Exception as e:
            _rlog.error("feishu", f"[FeishuClient] POST {path} 异常: {e}")
            raise

    async def _patch(self, path: str, payload: dict[str, Any]) -> dict:
        """通用 PATCH 请求（自动带 Bearer token）

        Args:
            path: API 路径（不含 BASE_URL）
            payload: 请求体 dict

        Returns:
            响应 JSON dict
        """
        token = await self._get_token()
        url = f"{_BASE_URL}{path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        session = await self._get_session()
        try:
            async with session.patch(url, json=payload, headers=headers) as resp:
                data = await resp.json(content_type=None)
                _rlog.info(
                    "feishu",
                    f"[FeishuClient] PATCH {path} -> code={data.get('code')}, msg={data.get('msg', 'ok')}",
                )
                return data
        except Exception as e:
            _rlog.error("feishu", f"[FeishuClient] PATCH {path} 异常: {e}")
            raise

    # ------------------------------------------------------------------
    # 发送消息 - 单聊 / 群聊文本
    # ------------------------------------------------------------------

    async def send_text(
        self,
        receive_id: str,
        receive_id_type: str,
        text: str,
    ) -> dict:
        """发送文本消息

        Args:
            receive_id: 接收方 ID（open_id / chat_id / user_id / email / union_id）
            receive_id_type: ID 类型，取值 "open_id" | "chat_id" | "user_id" | "email" | "union_id"
            text: 消息文本内容

        Returns:
            飞书 API 响应 dict

        Example:
            # 单聊
            await client.send_text("ou_xxx", "open_id", "你好，这是一条测试消息")
            # 群聊
            await client.send_text("oc_xxx", "chat_id", "群内广播消息")
        """
        payload = {
            "receive_id": receive_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        return await self._post(
            "/im/v1/messages",
            payload,
            params={"receive_id_type": receive_id_type},
        )

    # ------------------------------------------------------------------
    # 发送消息 - 富文本（post 类型）
    # ------------------------------------------------------------------

    async def send_rich_text(
        self,
        receive_id: str,
        receive_id_type: str,
        title: str,
        content_rows: list[list[dict]],
        lang: str = "zh_cn",
    ) -> dict:
        """发送富文本消息（飞书 post 类型）

        Args:
            receive_id: 接收方 ID
            receive_id_type: ID 类型
            title: 消息标题
            content_rows: 消息正文，列表的列表，每个内层列表代表一行，
                每行由若干内联元素 dict 组成。
                支持的 element 类型：
                  - 文本: {"tag": "text", "text": "内容", "un_escape": False, "style": ["bold"]}
                  - 超链接: {"tag": "a", "href": "https://...", "text": "链接文字"}
                  - @用户: {"tag": "at", "user_id": "ou_xxx", "user_name": "名字（可选）"}
                  - 图片: {"tag": "img", "image_key": "img_xxx", "width": 300, "height": 200}
                  - 代码块: {"tag": "code_block", "language": "python", "text": "代码内容"}
            lang: 语言代码，默认 "zh_cn"，也可使用 "en_us"

        Returns:
            飞书 API 响应 dict

        Example:
            rows = [
                [{"tag": "text", "text": "你好，这是第一行"},
                 {"tag": "a", "href": "https://feishu.cn", "text": "飞书官网"}],
                [{"tag": "text", "text": "第二行，@张三："},
                 {"tag": "at", "user_id": "ou_xxx"}],
            ]
            await client.send_rich_text("ou_yyy", "open_id", "通知标题", rows)
        """
        post_content = {
            lang: {
                "title": title,
                "content": content_rows,
            }
        }
        payload = {
            "receive_id": receive_id,
            "msg_type": "post",
            "content": json.dumps({"post": post_content}, ensure_ascii=False),
        }
        return await self._post(
            "/im/v1/messages",
            payload,
            params={"receive_id_type": receive_id_type},
        )

    # ------------------------------------------------------------------
    # 发送消息 - 交互卡片（interactive）
    # ------------------------------------------------------------------

    async def send_card(
        self,
        receive_id: str,
        receive_id_type: str,
        card: dict,
    ) -> dict:
        """发送飞书交互卡片消息

        Args:
            receive_id: 接收方 ID
            receive_id_type: ID 类型
            card: 卡片 JSON dict，直接传入（不需要转义），支持两种形式：
                1. 卡片 JSON：{"config": {...}, "elements": [...], "header": {...}}
                2. 卡片模板：{"type": "template", "data": {"template_id": "ctp_xxx",
                                                             "template_variable": {...}}}

        Returns:
            飞书 API 响应 dict

        Example:
            card = {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {"tag": "plain_text", "content": "消息标题"},
                    "template": "blue",
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {"tag": "lark_md", "content": "**正文内容**\\n支持 Markdown"},
                    }
                ],
            }
            await client.send_card("ou_xxx", "open_id", card)
        """
        payload = {
            "receive_id": receive_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        }
        return await self._post(
            "/im/v1/messages",
            payload,
            params={"receive_id_type": receive_id_type},
        )

    async def update_card(self, message_id: str, card: dict) -> dict:
        """更新已发送的交互卡片消息（PATCH 原地更新）

        通过 PATCH /im/v1/messages/{message_id} 更新卡片内容，
        用户侧看到的是同一条消息内容被刷新，而非新消息。

        Args:
            message_id: 待更新的消息 ID（send_card 返回的 message_id）
            card: 新的卡片 JSON dict

        Returns:
            飞书 API 响应 dict
        """
        payload = {
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        }
        return await self._patch(f"/im/v1/messages/{message_id}", payload)

    # ------------------------------------------------------------------
    # 批量发送消息
    # ------------------------------------------------------------------

    async def batch_send(
        self,
        msg_type: str,
        content: dict | None = None,
        card: dict | None = None,
        open_ids: list[str] | None = None,
        user_ids: list[str] | None = None,
        department_ids: list[str] | None = None,
        union_ids: list[str] | None = None,
    ) -> dict:
        """批量发送消息（向多用户或多部门）

        使用飞书批量发送接口（异步接口，有一定延迟）。
        仅支持向用户发送，不能直接发群组。
        每天上限 50 万条，每个 ID 列表长度 ≤ 200。

        Args:
            msg_type: 消息类型，"text" | "post" | "image" | "share_chat" | "interactive"
            content: 消息内容 dict（当 msg_type 为 text/post/image/share_chat 时使用）
            card: 卡片内容 dict（当 msg_type 为 interactive 时使用）
            open_ids: 接收用户的 open_id 列表
            user_ids: 接收用户的 user_id 列表
            department_ids: 接收部门 ID 列表（消息发给部门下所有成员）
            union_ids: 接收用户的 union_id 列表

        Returns:
            飞书 API 响应 dict，含 message_id（以 bm- 开头）及各类无效 ID 列表

        Example:
            # 批量文本发送
            result = await client.batch_send(
                msg_type="text",
                content={"text": "系统通知：服务维护中"},
                open_ids=["ou_aaa", "ou_bbb"],
                department_ids=["od_ccc"],
            )
            print(result["data"]["message_id"])  # bm-xxx

            # 批量富文本发送
            result = await client.batch_send(
                msg_type="post",
                content={"post": {"zh_cn": {"title": "公告", "content": [[...]]}}},
                open_ids=["ou_aaa"],
            )
        """
        if not any([open_ids, user_ids, department_ids, union_ids]):
            raise ValueError(
                "[FeishuClient] batch_send 需要至少提供一个目标 ID 列表"
                "（open_ids / user_ids / department_ids / union_ids）"
            )
        if msg_type == "interactive" and card is None:
            raise ValueError("[FeishuClient] interactive 类型需要提供 card 参数")
        if msg_type != "interactive" and content is None:
            raise ValueError(f"[FeishuClient] {msg_type} 类型需要提供 content 参数")

        payload: dict[str, Any] = {"msg_type": msg_type}

        if content is not None:
            payload["content"] = content
        if card is not None:
            payload["card"] = card
        if open_ids:
            payload["open_ids"] = open_ids
        if user_ids:
            payload["user_ids"] = user_ids
        if department_ids:
            payload["department_ids"] = department_ids
        if union_ids:
            payload["union_ids"] = union_ids

        return await self._post("/message/v4/batch_send/", payload)

    # ------------------------------------------------------------------
    # 回复消息（在同一会话线程中回复）
    # ------------------------------------------------------------------

    async def reply_text(
        self,
        message_id: str,
        text: str,
        reply_in_thread: bool = False,
    ) -> dict:
        """回复消息（文本）

        Args:
            message_id: 被回复消息的 ID（om_ 开头）
            text: 回复文本内容
            reply_in_thread: 是否以话题形式回复（仅群聊支持）

        Returns:
            飞书 API 响应 dict
        """
        payload: dict[str, Any] = {
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        if reply_in_thread:
            payload["reply_in_thread"] = True
        return await self._post(f"/im/v1/messages/{message_id}/reply", payload)

    async def reply_rich_text(
        self,
        message_id: str,
        title: str,
        content_rows: list[list[dict]],
        lang: str = "zh_cn",
    ) -> dict:
        """回复消息（富文本）

        Args:
            message_id: 被回复消息的 ID
            title: 富文本标题
            content_rows: 富文本行列表（格式同 send_rich_text）
            lang: 语言代码

        Returns:
            飞书 API 响应 dict
        """
        post_content = {lang: {"title": title, "content": content_rows}}
        payload = {
            "msg_type": "post",
            "content": json.dumps({"post": post_content}, ensure_ascii=False),
        }
        return await self._post(f"/im/v1/messages/{message_id}/reply", payload)

    # ------------------------------------------------------------------
    # 消息表情回应
    # ------------------------------------------------------------------

    async def add_reaction(self, message_id: str, emoji_type: str = "DONE") -> dict:
        """为消息添加表情回应（Reaction）

        调用飞书「添加消息表情回应」接口：
        POST /im/v1/messages/{message_id}/reactions

        Args:
            message_id: 飞书消息 ID（open_message_id）
            emoji_type: 表情类型，默认 "DONE"（[了解]）。
                        常用类型：
                          "DONE"       → [了解]（默认）
                          "THUMBSUP"   → [赞]
                          "OnIt"       → [收到]
                          "THINKING"   → [思考]

        Returns:
            飞书 API 响应 dict
        """
        path = f"/im/v1/messages/{message_id}/reactions"
        payload = {
            "reaction_type": {
                "emoji_type": emoji_type,
            }
        }
        return await self._post(path, payload)

    # ------------------------------------------------------------------
    # 资源管理
    # ------------------------------------------------------------------

    async def get_message_resource(
        self,
        message_id: str,
        file_key: str,
        resource_type: str = "image",
    ) -> tuple:
        """下载消息中的图片或文件资源

        调用飞书「获取消息中的资源文件」接口：
        GET /im/v1/messages/{message_id}/resources/{file_key}?type=image

        Args:
            message_id: 飞书消息 ID
            file_key: 资源的 image_key 或 file_key
            resource_type: 资源类型，"image" 或 "file"

        Returns:
            (bytes, media_type) 元组
        """
        token = await self._get_token()
        url = f"{_BASE_URL}/im/v1/messages/{message_id}/resources/{file_key}"
        headers = {"Authorization": f"Bearer {token}"}
        params = {"type": resource_type}
        session = await self._get_session()
        async with session.get(url, headers=headers, params=params) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(
                    f"[FeishuClient] 下载资源失败: HTTP {resp.status}, body={body[:200]}"
                )
            data = await resp.read()
            content_type = resp.headers.get("Content-Type", "image/png").split(";")[0].strip()
            return data, content_type

    async def close(self) -> None:
        """关闭 HTTP Session"""
        if self._session and not self._session.closed:
            await self._session.close()
            _rlog.info("feishu", "[FeishuClient] HTTP Session 已关闭")
