"""飞书消息网关 — 进程内集成版（无 WS 客户端）

架构变化（相比旧版）：
- 不再通过 WebSocket 连接 CoreScheduler；直接持有 ``PSGateway`` 引用
- 每条飞书消息 → 独立的 ``ps_id``（per-user/group PS）
- 出站消息通过 ``CoreScheduler._publish`` 分发回本类的 ``publish()``
- 进度卡片由 ``event`` 类型消息驱动（而非旧版的 ``progress_update``）
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..logging import get_running_log
from ..personal_space import PersonalSpaceManager
from ..personal_space.personal_space import KIND_FEISHU_CHAT
from .feishu_client import FeishuClient
from .feishu_event_listener import FeishuEventListener

if TYPE_CHECKING:
    from ..core.ps_gateway import PSGateway

_rlog = get_running_log()


def normalize_feishu_id(feishu_id: str) -> str:
    """将飞书 ID 转换为符合 Session ID 格式的字符串
    
    规则:
    - 将非法字符(非小写字母、数字、下划线)替换为下划线
    - 转换为小写
    
    Args:
        feishu_id: 飞书的 open_id 或 chat_id
        
    Returns:
        标准化后的 ID (小写字母、数字、下划线)
        
    Examples:
        >>> normalize_feishu_id("ou_123ABC-def")
        "ou_123abc_def"
        >>> normalize_feishu_id("oc_a1B2c3")
        "oc_a1b2c3"
    """
    # 转换为小写
    normalized = feishu_id.lower()
    # 将非法字符替换为下划线
    normalized = re.sub(r'[^a-z0-9_]', '_', normalized)
    return normalized


class FeishuGateway:
    """飞书消息网关（进程内版）

    职责：
    1. 启动 FeishuEventListener，接收飞书推送
    2. 将飞书消息转化为 PSGateway 协议（open + chat）并直接调用
    3. 接收 PSGateway 出站消息（通过 ``publish()``），路由回飞书用户

    Session 隔离策略：
    - p2p（单聊）：``feishu_p2p_{normalized_open_id}`` → 独立 PS
    - group（群聊）：``feishu_group_{normalized_chat_id}`` → 独立 PS
    """

    # 进度卡片节流间隔（秒）
    _PROGRESS_THROTTLE_SEC = 1.5
    # 卡片内容最大字节数（飞书限制 ~30KB，留余量）
    _PROGRESS_MAX_BYTES = 25_000

    def __init__(
        self,
        ps_gateway: PSGateway,
        ps_manager: PersonalSpaceManager,
        feishu_client: FeishuClient,
        feishu_config: dict[str, Any],
    ) -> None:
        self._ps_gateway = ps_gateway
        self._ps_manager = ps_manager
        self._feishu_client = feishu_client
        self._feishu_config = feishu_config

        # session_key → (receive_id, receive_id_type) 原始飞书 ID 映射
        self._session_receive_ids: dict[str, tuple] = {}

        # 持久化映射文件路径
        _cache_path_str = feishu_config.get("existing_session_cache")
        self._cache_path: Path | None = Path(_cache_path_str) if _cache_path_str else None
        self._cache_lock: asyncio.Lock = asyncio.Lock()

        # 飞书事件监听器
        self._listener: FeishuEventListener | None = None
        self._running = False

        # 后台 Task 集合（防止被 GC 回收）
        self._pending_tasks: set[asyncio.Task] = set()

        # --- 进度卡片状态 ---
        # session_key → 飞书已发送进度卡片的 message_id
        self._progress_card_ids: dict[str, str] = {}
        # session_key → 已累积的日志行列表
        self._progress_lines: dict[str, list[str]] = {}
        # session_key → 上次 PATCH 时间戳
        self._progress_last_update: dict[str, float] = {}

        # --- 查询确认卡片状态 ---
        # session_key → 已发送的查询卡片 message_id
        self._query_card_ids: dict[str, str] = {}
        # session_key → 延迟 flush Task
        self._progress_flush_tasks: dict[str, asyncio.Task] = {}
        # session_key → asyncio.Lock（串行化同一 session 的进度更新）
        self._progress_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动飞书事件监听。"""
        _rlog.info("feishu", "[FeishuGateway] 正在启动飞书消息网关（进程内模式）...")
        self._running = True

        self._load_session_cache()

        self._listener = FeishuEventListener(
            app_id=self._feishu_config.get("app_id", ""),
            app_secret=self._feishu_config.get("app_secret", ""),
            on_message=self._on_feishu_message,
            encrypt_key=self._feishu_config.get("encrypt_key", ""),
            verification_token=self._feishu_config.get("verification_token", ""),
            bot_open_id=self._feishu_config.get("bot_user_id", ""),
            dedupe_cache_size=self._feishu_config.get("dedupe_cache_size", 1000),
        )

        loop = asyncio.get_running_loop()
        self._listener.start(loop)

        _rlog.info("feishu", "[FeishuGateway] 飞书网关已启动，等待消息")

        try:
            while self._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    async def stop(self) -> None:
        """停止网关。"""
        self._running = False
        if self._listener:
            self._listener.stop()

        for task in list(self._progress_flush_tasks.values()):
            task.cancel()
        self._progress_flush_tasks.clear()

        await self._feishu_client.close()
        _rlog.info("feishu", "[FeishuGateway] 网关已停止")

    # ------------------------------------------------------------------
    # Session 映射持久化
    # ------------------------------------------------------------------

    def _load_session_cache(self) -> None:
        """同步加载持久化的 session_key → feishu_id 映射。

        在事件循环启动前调用，无需加锁。文件不存在时静默返回。
        """
        if self._cache_path is None:
            return
        try:
            if not self._cache_path.exists():
                return
            raw: dict[str, Any] = json.loads(self._cache_path.read_text(encoding="utf-8"))
            for k, v in raw.items():
                self._session_receive_ids[k] = tuple(v)  # type: ignore[assignment]
            _rlog.info(
                "feishu",
                f"[FeishuGateway] 已加载 session 映射缓存: {len(raw)} 条记录 ({self._cache_path})",
            )
        except Exception as e:
            _rlog.error("feishu", f"[FeishuGateway] 加载 session 映射缓存失败: {e}")

    async def _save_session_cache(self) -> None:
        """将当前 _session_receive_ids 原子写入缓存文件。

        先写到 .tmp 临时文件，再用 os.replace() 原子替换，防止写入中崩溃导致文件损坏。
        """
        if self._cache_path is None:
            return
        async with self._cache_lock:
            try:
                self._cache_path.parent.mkdir(parents=True, exist_ok=True)
                data = {k: list(v) for k, v in self._session_receive_ids.items()}
                tmp_path = self._cache_path.with_suffix(".tmp")
                tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(tmp_path, self._cache_path)
            except Exception as e:
                _rlog.error("feishu", f"[FeishuGateway] 写入 session 映射缓存失败: {e}")

    async def _reconnect_after_delay(self, session_key: str) -> None:
        # Kept as stub for backwards compat — no-op in in-process mode
        pass

    # ------------------------------------------------------------------
    # 出站回调（CoreScheduler → FeishuGateway）
    # ------------------------------------------------------------------

    async def publish(self, conn_id: str, msg: dict[str, Any]) -> None:
        """接收 PSGateway 出站消息并路由至飞书用户。

        由 ``CoreScheduler._publish`` 在 ``conn_id.startswith("feishu:")`` 时调用。
        """
        msg_type = msg.get("type")
        session_key = conn_id[len("feishu:"):]  # strip "feishu:" prefix

        if msg_type == "ack":
            return  # 忽略 ack，飞书不需要
        elif msg_type == "reply":
            await self._on_reply(session_key, msg)
        elif msg_type == "event":
            task = asyncio.create_task(self._on_event(session_key, msg))
            self._pending_tasks.add(task)
            task.add_done_callback(self._pending_tasks.discard)
        elif msg_type == "error":
            await self._on_error(session_key, msg)
        else:
            _rlog.debug("feishu", f"[FeishuGateway] 忽略消息类型 type={msg_type} session={session_key}")

    async def _on_reply(self, session_key: str, msg: dict) -> None:
        """将 reply 消息的 content 发回飞书用户。"""
        content: str = msg.get("content", "")
        if not content:
            return
        self._clear_progress_state(session_key)
        await self._send_to_session(session_key, content)

    async def _on_event(self, session_key: str, msg: dict) -> None:
        """将 event 消息推送给飞书用户。

        - ``query.opened``   → 发送查询确认卡片
        - ``query.resolved`` → 更新查询卡片为已完成
        - ``query.cleared``  → 更新查询卡片为已取消
        - 其他              → 追加到进度卡片
        """
        ev_type: str = msg.get("event", "")

        if ev_type == "query.opened":
            await self._send_query_card(session_key, msg)
            return

        if ev_type in ("query.resolved", "query.cleared"):
            await self._close_query_card(session_key, msg)
            return

        content: str = msg.get("content", "")
        if not content:
            return
        await self._update_progress_card(session_key, content)

    async def _on_error(self, session_key: str, msg: dict) -> None:
        """将 error 消息以文本形式发回飞书用户。"""
        content: str = msg.get("content") or msg.get("message") or "系统错误"
        self._clear_progress_state(session_key)
        await self._send_to_session(session_key, f"[错误] {content}")

    # ------------------------------------------------------------------
    # 查询确认卡片
    # ------------------------------------------------------------------

    @staticmethod
    def _build_query_card(msg: dict) -> dict:
        """根据 query.opened 事件构建飞书确认卡片（纯展示，无按钮回调）。"""
        prompt: str = msg.get("prompt", "")
        choices: list[dict] = msg.get("choices", [])
        tool_name: str = msg.get("tool_name") or ""
        origin: str = msg.get("origin") or ""

        # 标题副标题
        if tool_name:
            badge = "安全规则" if origin == "rule" else "Agent 询问"
            subtitle = f"工具：`{tool_name}`（{badge}）\n\n"
        else:
            subtitle = ""

        # 选项列表
        choice_lines = "\n".join(
            f"**{i + 1}.** {c['label']}"
            + (f"  —  {c['description']}" if c.get("description") else "")
            for i, c in enumerate(choices)
        )

        body = f"{subtitle}{prompt}\n\n{choice_lines}"
        footer = "💬 请回复**数字**（如 `1`）或选项**名称**（如 `allow`）"

        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "🔒 待确认操作"},
                "template": "orange",
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": body}},
                {"tag": "hr"},
                {"tag": "div", "text": {"tag": "lark_md", "content": footer}},
            ],
        }

    @staticmethod
    def _build_query_resolved_card(value: str, cancelled: bool = False) -> dict:
        """构建已处理的查询卡片（替换原卡片）。"""
        if cancelled:
            content = "🚫 操作已取消"
            template = "grey"
        else:
            content = f"✅ 已选择：**{value}**"
            template = "green"
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "🔒 确认操作"},
                "template": template,
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": content}},
            ],
        }

    async def _send_query_card(self, session_key: str, msg: dict) -> None:
        """发送查询确认卡片，并记录其 message_id。"""
        receive_info = self._session_receive_ids.get(session_key)
        if not receive_info:
            _rlog.warning(
                "feishu",
                f"[FeishuGateway] _send_query_card: 无 session 映射 {session_key}",
            )
            return
        receive_id, receive_id_type = receive_info
        card = self._build_query_card(msg)
        try:
            resp = await self._feishu_client.send_card(
                receive_id=receive_id,
                receive_id_type=receive_id_type,
                card=card,
            )
            msg_id = resp.get("data", {}).get("message_id", "")
            if msg_id:
                self._query_card_ids[session_key] = msg_id
                _rlog.info(
                    "feishu",
                    f"[FeishuGateway] 查询卡片已发送 session={session_key} msg_id={msg_id}",
                )
        except Exception as e:
            _rlog.error(
                "feishu",
                f"[FeishuGateway] 发送查询卡片失败 ({session_key}): {e}",
            )

    async def _close_query_card(self, session_key: str, msg: dict) -> None:
        """将查询卡片更新为已处理状态，并清除记录。"""
        msg_id = self._query_card_ids.pop(session_key, None)
        if not msg_id:
            return
        cancelled = msg.get("event") == "query.cleared"
        value: str = msg.get("value", "")
        card = self._build_query_resolved_card(value=value, cancelled=cancelled)
        try:
            await self._feishu_client.update_card(msg_id, card)
            _rlog.info(
                "feishu",
                f"[FeishuGateway] 查询卡片已更新 session={session_key} "
                f"msg_id={msg_id} cancelled={cancelled}",
            )
        except Exception as e:
            _rlog.error(
                "feishu",
                f"[FeishuGateway] 更新查询卡片失败 ({session_key}): {e}",
            )

    # ------------------------------------------------------------------
    # Session Key 计算
    # ------------------------------------------------------------------

    def _get_session_key(self, msg: dict) -> str:
        """根据消息计算 session_key，并记录原始飞书 ID 映射。"""
        chat_type = msg.get("chat_type", "p2p")
        if chat_type == "p2p":
            open_id = msg['sender_open_id']
            normalized_id = normalize_feishu_id(open_id)
            session_key = f"feishu_p2p_{normalized_id}"
            self._session_receive_ids[session_key] = (open_id, "open_id")
        else:
            chat_id = msg['chat_id']
            normalized_id = normalize_feishu_id(chat_id)
            session_key = f"feishu_group_{normalized_id}"
            self._session_receive_ids[session_key] = (chat_id, "chat_id")
        asyncio.create_task(self._save_session_cache())
        return session_key

    # ------------------------------------------------------------------
    # 入站（飞书 → PSGateway）
    # ------------------------------------------------------------------

    async def _on_feishu_message(self, msg: dict) -> None:
        """飞书消息回调 — 即时反馈 + 后台转发。"""
        text = msg.get("text", "").strip()
        image_keys: list = msg.get("image_keys", [])
        if not text and not image_keys:
            _rlog.info("feishu", "[FeishuGateway] 收到空消息（无文字也无图片），跳过")
            return

        sender = msg.get("sender_open_id", "unknown")
        chat_type = msg.get("chat_type", "p2p")
        _rlog.info(
            "feishu",
            f"[FeishuGateway] 收到消息 chat_type={chat_type} sender={sender}: {text[:50]}",
        )

        # 立即用 [了解] 表情回应消息，给用户即时反馈
        try:
            message_id = msg.get("message_id", "")
            if message_id:
                await self._feishu_client.add_reaction(message_id, "THINKING")
        except Exception as e:
            _rlog.error("feishu", f"[FeishuGateway] 发送 Reaction 失败: {e}")

        # 下载图片并按原始顺序构建 content_parts
        if image_keys:
            import base64
            ordered_parts: list = msg.get("ordered_parts", [])
            message_id = msg.get("message_id", "")
            # 预下载所有图片
            img_cache: dict = {}
            for img_key in image_keys:
                try:
                    img_bytes, media_type = await self._feishu_client.get_message_resource(
                        message_id, img_key
                    )
                    img_cache[img_key] = (img_bytes, media_type)
                except Exception as e:
                    _rlog.error(
                        "feishu",
                        f"[FeishuGateway] 下载图片失败 image_key={img_key}: {e}",
                    )
            # 按 ordered_parts 顺序组装（fallback：无 ordered_parts 时用旧逻辑）
            if ordered_parts:
                content_parts = []
                for part in ordered_parts:
                    if part["type"] == "text":
                        content_parts.append({"type": "text", "text": part["text"]})
                    elif part["type"] == "image_key":
                        cached = img_cache.get(part["image_key"])
                        if cached:
                            img_bytes, media_type = cached
                            b64data = base64.b64encode(img_bytes).decode("utf-8")
                            content_parts.append(
                                {
                                    "type": "image",
                                    "source_type": "base64",
                                    "data": b64data,
                                    "media_type": media_type,
                                }
                            )
            else:
                # fallback：text 在前，images 在后
                content_parts = []
                if text:
                    content_parts.append({"type": "text", "text": text})
                for img_key, (img_bytes, media_type) in img_cache.items():
                    b64data = base64.b64encode(img_bytes).decode("utf-8")
                    content_parts.append(
                        {
                            "type": "image",
                            "source_type": "base64",
                            "data": b64data,
                            "media_type": media_type,
                        }
                    )
            if content_parts:
                msg["content_parts"] = content_parts

        task = asyncio.create_task(self._fire_chat(msg))
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    async def _fire_chat(self, msg: dict) -> None:
        """后台 Task：将飞书消息路由至 PSGateway。"""
        session_key = self._get_session_key(msg)
        conn_id = f"feishu:{session_key}"
        ps_id = session_key

        # 确保 PS 以 feishu_chat kind 引导（幂等）
        try:
            await self._ps_manager.open_connection(conn_id, ps_id, init_kind=KIND_FEISHU_CHAT)
        except Exception as e:
            _rlog.error("feishu", f"[FeishuGateway] open_connection 失败 {ps_id}: {e}")
            await self._send_to_session(session_key, f"[连接错误] {e}")
            return

        request_id = str(uuid.uuid4())
        user_id = msg.get("sender_open_id", "feishu_user")
        text = msg.get("text", "")

        # Intercept /stop: send as control frame (bypasses _processing_lock server-side)
        if text.strip() == "/stop":
            try:
                await self._ps_gateway.handle_inbound(conn_id, {
                    "type": "control",
                    "action": "stop",
                    "request_id": request_id,
                    "ps_id": ps_id,
                    "widget_id": "w_chat_default",
                })
                await self._send_to_session(session_key, "⚠️ 已发送停止信号")
            except Exception as e:
                await self._send_to_session(session_key, f"[停止失败] {e}")
            return

        chat_msg: dict[str, Any] = {
            "type": "chat",
            "ps_id": ps_id,
            "widget_id": "w_chat_default",
            "request_id": request_id,
            "content": text,
            "user_id": user_id,
        }
        if "content_parts" in msg:
            chat_msg["content_parts"] = msg["content_parts"]

        try:
            await self._ps_gateway.handle_inbound(conn_id, chat_msg)
        except Exception as e:
            _rlog.error("feishu", f"[FeishuGateway] handle_inbound 失败 {session_key}: {e}")
            self._clear_progress_state(session_key)
            await self._send_to_session(session_key, f"[系统错误] {e}")

    # ------------------------------------------------------------------
    # 发送工具
    # ------------------------------------------------------------------

    async def _send_to_session(self, session_key: str, text: str) -> None:
        """按 session_key 查表，将文本发送给对应飞书用户/群。"""
        receive_info = self._session_receive_ids.get(session_key)
        if not receive_info:
            _rlog.warning("feishu", f"[FeishuGateway] 找不到 session receive_id 映射: {session_key}")
            return
        receive_id, receive_id_type = receive_info
        reply_type = self._feishu_config.get("default_reply_type", "text")
        try:
            if reply_type == "rich_text":
                rows = self._text_to_rich_text_rows(text)
                await self._feishu_client.send_rich_text(
                    receive_id=receive_id, receive_id_type=receive_id_type,
                    title="", content_rows=rows,
                )
            else:
                await self._feishu_client.send_text(receive_id, receive_id_type, text)
            _rlog.info("feishu", f"[FeishuGateway] 已发送消息至 {session_key}: {text[:50]}")
        except Exception as e:
            _rlog.error("feishu", f"[FeishuGateway] 发送消息失败 ({session_key}): {e}")

    # ------------------------------------------------------------------
    # 进度卡片管理
    # ------------------------------------------------------------------

    @staticmethod
    def _build_progress_card(lines: list[str]) -> dict:
        """构建进度日志卡片（Markdown 代码块）。"""
        line_count = len(lines)
        log_text = "\n".join(lines)

        while len(log_text.encode("utf-8")) > FeishuGateway._PROGRESS_MAX_BYTES and len(lines) > 1:
            lines = lines[1:]
            log_text = "\n".join(lines)

        md_content = f"```\n{log_text}\n```"

        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"⏳ 运行日志 ({line_count} 行)"},
                "template": "turquoise",
            },
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": md_content}}],
        }

    async def _update_progress_card(self, session_key: str, content: str) -> None:
        """累积进度行并更新飞书进度卡片（含节流）。"""
        if session_key not in self._progress_locks:
            self._progress_locks[session_key] = asyncio.Lock()
        async with self._progress_locks[session_key]:
            receive_info = self._session_receive_ids.get(session_key)
            if not receive_info:
                return
            receive_id, receive_id_type = receive_info

            if session_key not in self._progress_lines:
                self._progress_lines[session_key] = []
            self._progress_lines[session_key].append(content)
            lines = self._progress_lines[session_key]
            card = self._build_progress_card(lines)

            now = time.monotonic()
            last = self._progress_last_update.get(session_key, 0.0)
            card_msg_id = self._progress_card_ids.get(session_key)

            if card_msg_id is None:
                try:
                    resp = await self._feishu_client.send_card(
                        receive_id=receive_id, receive_id_type=receive_id_type, card=card,
                    )
                    msg_id = resp.get("data", {}).get("message_id", "")
                    if msg_id:
                        self._progress_card_ids[session_key] = msg_id
                        self._progress_last_update[session_key] = now
                except Exception as e:
                    _rlog.error("feishu", f"[FeishuGateway] 进度卡片创建失败 ({session_key}): {e}")
            elif now - last >= self._PROGRESS_THROTTLE_SEC:
                await self._patch_progress_card(session_key, card_msg_id, card)
                self._progress_last_update[session_key] = now
                self._cancel_flush_task(session_key)
            else:
                self._schedule_flush(session_key, card_msg_id, card)

    async def _patch_progress_card(self, session_key: str, msg_id: str, card: dict) -> None:
        try:
            await self._feishu_client.update_card(msg_id, card)
        except Exception as e:
            _rlog.error("feishu", f"[FeishuGateway] 进度卡片更新失败 ({session_key}): {e}")

    def _schedule_flush(self, session_key: str, msg_id: str, card: dict) -> None:
        self._cancel_flush_task(session_key)

        async def _delayed_flush() -> None:
            await asyncio.sleep(self._PROGRESS_THROTTLE_SEC)
            latest_lines = self._progress_lines.get(session_key, [])
            if latest_lines:
                latest_card = self._build_progress_card(latest_lines)
                await self._patch_progress_card(session_key, msg_id, latest_card)
                self._progress_last_update[session_key] = time.monotonic()
            self._progress_flush_tasks.pop(session_key, None)

        task = asyncio.create_task(_delayed_flush())
        self._progress_flush_tasks[session_key] = task
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    def _cancel_flush_task(self, session_key: str) -> None:
        task = self._progress_flush_tasks.pop(session_key, None)
        if task and not task.done():
            task.cancel()

    def _clear_progress_state(self, session_key: str) -> None:
        self._cancel_flush_task(session_key)
        self._progress_card_ids.pop(session_key, None)
        self._progress_lines.pop(session_key, None)
        self._progress_last_update.pop(session_key, None)

    # ------------------------------------------------------------------
    # 主动发送 API（供业务层调用）
    # ------------------------------------------------------------------

    async def send_text(self, receive_id: str, receive_id_type: str, text: str) -> dict:
        return await self._feishu_client.send_text(receive_id, receive_id_type, text)

    async def send_rich_text(
        self, receive_id: str, receive_id_type: str, title: str,
        content_rows: list[list[dict]], lang: str = "zh_cn",
    ) -> dict:
        return await self._feishu_client.send_rich_text(
            receive_id, receive_id_type, title, content_rows, lang
        )

    async def send_card(
        self,
        receive_id: str,
        receive_id_type: str,
        card: dict,
    ) -> dict:
        """主动发送交互卡片消息

        Args:
            receive_id: 接收方 ID
            receive_id_type: ID 类型
            card: 卡片 JSON dict

        Returns:
            飞书 API 响应 dict
        """
        return await self._feishu_client.send_card(receive_id, receive_id_type, card)

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
        """批量群发消息（透传至 FeishuClient.batch_send）

        Args:
            msg_type: 消息类型（"text" | "post" | "interactive" 等）
            content: 消息内容 dict（非 interactive 类型）
            card: 卡片内容 dict（interactive 类型）
            open_ids: 接收用户 open_id 列表
            user_ids: 接收用户 user_id 列表
            department_ids: 接收部门 ID 列表
            union_ids: 接收用户 union_id 列表

        Returns:
            飞书 API 响应 dict，含 message_id 和各类无效 ID 列表
        """
        return await self._feishu_client.batch_send(
            msg_type=msg_type,
            content=content,
            card=card,
            open_ids=open_ids,
            user_ids=user_ids,
            department_ids=department_ids,
            union_ids=union_ids,
        )

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _text_to_rich_text_rows(text: str) -> list[list[dict]]:
        """将普通文本转换为飞书富文本行格式

        按换行符拆分，每行作为一个 text element。

        Args:
            text: 普通文本

        Returns:
            富文本行列表（list of list of dict）
        """
        rows = []
        for line in text.split("\n"):
            if line.strip():
                rows.append([{"tag": "text", "text": line}])
            else:
                # 空行用空格占位，保持段落间距
                rows.append([{"tag": "text", "text": " "}])
        return rows if rows else [[{"tag": "text", "text": text}]]
