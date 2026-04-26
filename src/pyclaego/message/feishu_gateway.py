"""飞书消息网关 - 整合事件监听与 CoreScheduler 路由"""

import asyncio
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import websockets

from .feishu_client import FeishuClient
from .feishu_event_listener import FeishuEventListener
from ..logging import get_running_log

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
    """飞书消息网关

    职责：
    1. 连接 CoreScheduler（WebSocket），类比 TUIClient
    2. 启动 FeishuEventListener（WebSocket 长连接收飞书消息）
    3. 将飞书消息路由至 CoreScheduler 处理
    4. 将 CoreScheduler 响应通过 FeishuClient 发回飞书用户

    Session 隔离策略：
    - p2p（单聊）：每个用户 open_id 绑定独立 Session，session_id = "feishu_p2p_{open_id}"
    - group（群聊）：每个群 chat_id 绑定独立 Session，session_id = "feishu_group_{chat_id}"

    连接策略：
    - 每个飞书 session（用户/群）维护一条持久 ws 连接，同一 session 的并发消息共享复用
    - 每条消息携带唯一 request_id（uuid），CoreScheduler 透传到 response
    - _dispatch_loop 按 request_id 将响应路由到对应 asyncio.Future，彻底消除竞争
    - ws 断开时，_dispatch_loop 自动清理 session，下次消息触发重建
    """

    def __init__(
        self,
        server_url: str,
        feishu_client: FeishuClient,
        feishu_config: Dict[str, Any],
        user_id: str = "feishu_bot",
    ) -> None:
        """初始化飞书网关

        Args:
            server_url: CoreScheduler WebSocket 地址，如 "ws://127.0.0.1:8765"
            feishu_client: FeishuClient 实例（用于发送消息）
            feishu_config: 飞书配置字典（来自 config.yaml 的 feishu: 节）
            user_id: 网关在 CoreScheduler 中使用的用户标识
        """
        self._server_url = server_url
        self._feishu_client = feishu_client
        self._feishu_config = feishu_config
        self._user_id = user_id

        # session_key → 持久 ws 连接（每个飞书 session 一条）
        self._sessions: Dict[str, Any] = {}
        # session_key → asyncio.Lock（保护 _sessions 的建立/重建操作）
        self._session_locks: Dict[str, asyncio.Lock] = {}
        # session_key → _dispatch_loop Task
        self._dispatch_loops: Dict[str, asyncio.Task] = {}
        
        # session_key → (receive_id, receive_id_type) 原始飞书 ID 映射
        # 用于在进度更新时能够正确发送消息给飞书
        self._session_receive_ids: Dict[str, tuple] = {}

        # 持久化映射文件路径（从 feishu_config["existing_session_cache"] 读取）
        _cache_path_str = feishu_config.get("existing_session_cache")
        self._cache_path: Optional[Path] = Path(_cache_path_str) if _cache_path_str else None
        # 保护缓存文件写入的锁（防止并发写导致文件损坏）
        self._cache_lock: asyncio.Lock = asyncio.Lock()

        # session_key → 重连 Task（在 _dispatch_loop 断开后触发）
        self._reconnect_tasks: Dict[str, asyncio.Task] = {}

        # request_id → asyncio.Future（存放该请求的最终 response dict）
        self._pending_requests: Dict[str, asyncio.Future] = {}
        self._pending_lock = asyncio.Lock()

        # 飞书事件监听器
        self._listener: Optional[FeishuEventListener] = None
        self._running = False

        # 后台处理 Task 集合（防止被 GC 回收）
        self._pending_tasks: Set[asyncio.Task] = set()

        # --- 进度卡片状态 ---
        # session_key → 飞书已发送进度卡片的 message_id
        self._progress_card_ids: Dict[str, str] = {}
        # session_key → 已累积的日志行列表
        self._progress_lines: Dict[str, List[str]] = {}
        # session_key → 上次 PATCH 时间戳（用于节流）
        self._progress_last_update: Dict[str, float] = {}
        # session_key → 延迟 flush Task（保证最后一条更新一定发出）
        self._progress_flush_tasks: Dict[str, asyncio.Task] = {}
        # session_key → asyncio.Lock（串行化同一 session 的进度更新，防止并发创建多张卡片）
        self._progress_locks: Dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # 核心启动流程
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动飞书网关

        流程：
        1. 创建 FeishuEventListener
        2. 在当前 asyncio 事件循环中启动后台线程监听
        3. 持续保活，直到 _running 被置为 False
        """
        _rlog.info("feishu", "[FeishuGateway] 正在启动飞书消息网关...")
        self._running = True

        # 加载已持久化的 session → feishu_id 映射，并为每个已知 session 预建 ws 连接
        self._load_session_cache()
        if self._session_receive_ids:
            _rlog.info(
                "feishu",
                f"[FeishuGateway] 预连接 {len(self._session_receive_ids)} 个已知 feishu session",
            )
            for _sk in list(self._session_receive_ids):
                task = asyncio.create_task(self._get_or_create_session_ws(_sk))
                self._pending_tasks.add(task)
                task.add_done_callback(self._pending_tasks.discard)

        # 构建事件监听器
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

        _rlog.info(
            "feishu",
            f"[FeishuGateway] 飞书网关已启动，等待消息（CoreScheduler: {self._server_url}）",
        )

        try:
            # 保活循环（WebSocket 长连接在后台线程维持）
            while self._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    async def stop(self) -> None:
        """停止网关"""
        self._running = False
        if self._listener:
            self._listener.stop()

        # 取消所有延迟 flush Task
        for task in list(self._progress_flush_tasks.values()):
            task.cancel()
        self._progress_flush_tasks.clear()

        # 取消所有重连 Task
        for task in list(self._reconnect_tasks.values()):
            task.cancel()
        self._reconnect_tasks.clear()

        # 取消所有 _dispatch_loop Task
        for task in list(self._dispatch_loops.values()):
            task.cancel()
        self._dispatch_loops.clear()

        # 关闭所有持久 ws 连接
        for ws in list(self._sessions.values()):
            try:
                await ws.close()
            except Exception:
                pass
        self._sessions.clear()

        # 对所有挂起的 Future 设置取消异常
        async with self._pending_lock:
            for fut in self._pending_requests.values():
                if not fut.done():
                    fut.cancel()
            self._pending_requests.clear()

        # 关闭 HTTP 客户端
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
            raw: Dict[str, Any] = json.loads(self._cache_path.read_text(encoding="utf-8"))
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
        """延迟重连：ws 断开后等待 ws_reconnect_delay 秒再尝试重新连接。

        分配为 Task，存储在 _reconnect_tasks[session_key] 中以便 stop() 时取消。
        """
        delay = float(self._feishu_config.get("ws_reconnect_delay", 5))
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if not self._running:
            return
        if session_key not in self._session_receive_ids:
            return
        _rlog.info("feishu", f"[FeishuGateway] 开始重连 session ws: {session_key}")
        try:
            await self._get_or_create_session_ws(session_key)
        except Exception as e:
            _rlog.error("feishu", f"[FeishuGateway] 重连失败 {session_key}: {e}")
        finally:
            self._reconnect_tasks.pop(session_key, None)

    # ------------------------------------------------------------------
    # Session WS 管理
    # ------------------------------------------------------------------

    def _get_session_lock(self, session_key: str) -> asyncio.Lock:
        """获取（或创建）指定 session_key 的锁"""
        if session_key not in self._session_locks:
            self._session_locks[session_key] = asyncio.Lock()
        return self._session_locks[session_key]

    async def _get_or_create_session_ws(self, session_key: str) -> Any:
        """获取或建立指定 session 的持久 ws 连接

        同一 session_key 只维护一条 ws。若 ws 已存在且健康，直接复用；
        否则建立新连接，完成 join_session 握手，并启动对应的 _dispatch_loop。

        Args:
            session_key: session 标识（feishu_p2p_{open_id} 或 feishu_group_{chat_id}）

        Returns:
            已完成握手的 WebSocketClientProtocol 对象

        Raises:
            Exception: 连接失败或 join_session 被拒绝时抛出
        """
        lock = self._get_session_lock(session_key)
        async with lock:
            # 检查是否已有健康连接
            existing = self._sessions.get(session_key)
            if existing is not None:
                return existing

            # 建立新连接
            _rlog.info("feishu", f"[FeishuGateway] 建立 session ws: {session_key}")
            ping_interval = int(self._feishu_config.get("ws_ping_interval", 30))
            ping_timeout = int(self._feishu_config.get("ws_ping_timeout", 10))
            try:
                ws = await websockets.connect(  # type: ignore
                    self._server_url,
                    ping_interval=ping_interval,
                    ping_timeout=ping_timeout,
                )
            except Exception as e:
                _rlog.error(
                    "feishu",
                    f"[FeishuGateway] 无法连接 CoreScheduler ({self._server_url}): {e}",
                )
                raise

            # 发送 join_session
            join_msg = {
                "type": "join_session",
                "session_id": session_key,
                "user_id": self._user_id,
            }
            await ws.send(json.dumps(join_msg))

            # 等待 session_joined 响应
            try:
                resp_str = await asyncio.wait_for(ws.recv(), timeout=10.0)
                resp = json.loads(resp_str)
                if resp.get("type") == "session_joined":
                    _rlog.info(
                        "feishu",
                        f"[FeishuGateway] Session 已加入: {resp.get('session_id')} "
                        f"(新建={resp.get('is_new', False)})",
                    )
                elif resp.get("type") == "error":
                    _rlog.error(
                        "feishu",
                        f"[FeishuGateway] 加入 Session 失败: {resp.get('content')}",
                    )
                    await ws.close()
                    raise RuntimeError(f"join_session 失败: {resp.get('content')}")
            except asyncio.TimeoutError:
                _rlog.error("feishu", "[FeishuGateway] 等待 join_session 响应超时")
                await ws.close()
                raise

            # 注册 ws 并启动 _dispatch_loop
            self._sessions[session_key] = ws
            dispatch_task = asyncio.create_task(
                self._dispatch_loop(session_key, ws)
            )
            self._dispatch_loops[session_key] = dispatch_task
            # 加入 _pending_tasks 防止 GC
            self._pending_tasks.add(dispatch_task)
            dispatch_task.add_done_callback(self._pending_tasks.discard)

            return ws

    async def _dispatch_loop(self, session_key: str, ws: Any) -> None:
        """持续读取 ws 上的所有消息，按 request_id 分发到对应 Future

        这是每条持久 ws 的专属读取协程，负责将 CoreScheduler 发回的所有响应
        路由到等待中的 _handle_and_stream 协程。

        断连处理：
        - 从 _sessions 移除该 session 的 ws 记录（下次消息触发重建）
        - 对所有属于该 session 尚未完成的 Future 设置 RuntimeError

        Args:
            session_key: 该 ws 对应的 session 标识（用于日志和清理）
            ws: 持久 WebSocket 连接对象
        """
        _rlog.info("feishu", f"[FeishuGateway] _dispatch_loop 已启动: {session_key}")
        try:
            async for raw in ws:
                try:
                    resp = json.loads(raw)
                except json.JSONDecodeError:
                    _rlog.warning("feishu", f"[FeishuGateway] 无法解析响应 JSON: {raw[:100]}")
                    continue

                request_id = resp.get("request_id", "")
                resp_type = resp.get("type", "")

                if not request_id:
                    if resp_type == "progress_update":
                        asyncio.create_task(
                            self._on_progress_update(session_key, resp)
                        )
                    elif resp_type == "cron_response":
                        asyncio.create_task(
                            self._on_cron_response(session_key, resp)
                        )
                    else:
                        _rlog.info(
                            "feishu",
                            f"[FeishuGateway] 收到无 request_id 的消息 type={resp_type}，忽略",
                        )
                    continue

                async with self._pending_lock:
                    fut = self._pending_requests.get(request_id)

                if fut is None:
                    _rlog.warning(
                        "feishu",
                        f"[FeishuGateway] 找不到 request_id={request_id} 对应的 Future，忽略",
                    )
                    continue

                if fut.done():
                    continue

                # 将响应分发给等待者
                if resp_type == "error":
                    fut.set_exception(
                        RuntimeError(resp.get("content", "CoreScheduler 返回错误"))
                    )
                else:
                    fut.set_result(resp)

        except asyncio.CancelledError:
            _rlog.info("feishu", f"[FeishuGateway] _dispatch_loop 被取消: {session_key}")
        except websockets.exceptions.ConnectionClosed:  # type: ignore
            _rlog.warning(
                "feishu",
                f"[FeishuGateway] CoreScheduler ws 断开: {session_key}，清理 session",
            )
        except Exception as e:
            _rlog.error("feishu", f"[FeishuGateway] _dispatch_loop 异常: {session_key}: {e}")
        finally:
            # 清理 session 记录，下次消息触发重建
            self._sessions.pop(session_key, None)
            self._dispatch_loops.pop(session_key, None)

            # 对所有尚未完成的 Future 设置断连异常
            async with self._pending_lock:
                pending_ids = list(self._pending_requests.keys())

            for rid in pending_ids:
                async with self._pending_lock:
                    fut = self._pending_requests.get(rid)
                if fut and not fut.done():
                    fut.set_exception(
                        RuntimeError(f"ws 连接断开: {session_key}")
                    )

            # 若网关仍在运行且该 session 仍在映射中，安排重连
            if self._running and session_key in self._session_receive_ids:
                reconnect_task = asyncio.create_task(
                    self._reconnect_after_delay(session_key)
                )
                self._reconnect_tasks[session_key] = reconnect_task
                self._pending_tasks.add(reconnect_task)
                reconnect_task.add_done_callback(self._pending_tasks.discard)

            _rlog.info("feishu", f"[FeishuGateway] _dispatch_loop 已退出: {session_key}")

    # ------------------------------------------------------------------
    # Session Key 计算
    # ------------------------------------------------------------------

    def _get_session_key(self, msg: Dict) -> str:
        """根据消息计算 Session Key，并记录原始飞书 ID 映射
        
        规则:
        - 单聊(p2p): feishu_p2p_{normalized_open_id}
        - 群聊(group): feishu_group_{normalized_chat_id}
        - 自动将飞书 ID 标准化为符合 Session ID 格式(小写字母+数字+下划线)
        - 同时记录 session_key → (原始receive_id, receive_id_type) 映射

        Args:
            msg: 标准化飞书消息 dict

        Returns:
            session_key 字符串
        """
        chat_type = msg.get("chat_type", "p2p")
        if chat_type == "p2p":
            open_id = msg['sender_open_id']
            normalized_id = normalize_feishu_id(open_id)
            session_key = f"feishu_p2p_{normalized_id}"
            # 记录原始 ID 映射
            self._session_receive_ids[session_key] = (open_id, "open_id")
        else:
            chat_id = msg['chat_id']
            normalized_id = normalize_feishu_id(chat_id)
            session_key = f"feishu_group_{normalized_id}"
            # 记录原始 ID 映射
            self._session_receive_ids[session_key] = (chat_id, "chat_id")
        # 持久化映射（异步，不阻塞主流程）
        asyncio.create_task(self._save_session_cache())
        return session_key

    # ------------------------------------------------------------------
    # 消息路由核心
    # ------------------------------------------------------------------

    async def _on_feishu_message(self, msg: Dict) -> None:
        """飞书消息回调 - 立即回复 Thinking，后台处理并推送最终回复

        收到消息后的处理流程：
        1. 立即向飞书发送 "Thinking..." 提示（即时反馈）
        2. 后台创建 Task 转发消息给 CoreScheduler，等待并回复最终结果

        Args:
            msg: 标准化飞书消息 dict
        """
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

        # 后台 Task：通过持久 ws 转发到 CoreScheduler 并等待 Future 结果
        task = asyncio.create_task(self._handle_and_stream(msg, text, sender))
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    async def _handle_and_stream(
        self, msg: Dict, text: str, sender: str
    ) -> None:
        """后台 Task：通过 session 持久 ws 发送消息，等待 Future 完成后回复飞书

        流程：
        1. 获取该 session 的持久 ws（复用或新建）
        2. 生成唯一 request_id，注册 asyncio.Future
        3. 发送 user_message（携带 request_id）
        4. 等待 _dispatch_loop 将响应路由到 Future
        5. 将 Future 结果（response content）发回飞书
        6. finally 中清理 Future

        Args:
            msg: 标准化飞书消息 dict
            text: 消息文本
            sender: 发送方 open_id
        """
        session_key = self._get_session_key(msg)

        # 获取持久 ws
        try:
            ws = await self._get_or_create_session_ws(session_key)
        except Exception as e:
            _rlog.error("feishu", f"[FeishuGateway] 获取 Session ws 失败: {e}")
            await self._reply_to_feishu(msg, f"[连接错误] 无法连接至服务器：{e}")
            return

        # 生成 request_id 并注册 Future
        request_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()

        async with self._pending_lock:
            self._pending_requests[request_id] = fut

        # 【2026年04月09日07:51:28修改】默认超时界线 20 分钟
        timeout: float = float(self._feishu_config.get("response_timeout", 1200.0))

        try:
            # 发送消息（携带 request_id）
            user_msg = {
                "type": "user_message",
                "content": text,
                "user_id": sender,
                "request_id": request_id,
            }
            if "content_parts" in msg:
                user_msg["content_parts"] = msg["content_parts"]
            await ws.send(json.dumps(user_msg))
            _rlog.info(
                "feishu",
                f"[FeishuGateway] 已发送 user_message request_id={request_id}: {text[:50]}",
            )

            # 等待 _dispatch_loop 分发结果（带超时）
            # asyncio.shield 防止外部 cancel 破坏 Future 本体
            resp = await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)

            content = resp.get("content", "")
            if content:
                await self._reply_to_feishu(msg, content)
            else:
                _rlog.warning(
                    "feishu",
                    f"[FeishuGateway] response 内容为空 request_id={request_id}",
                )

        except asyncio.TimeoutError:
            _rlog.warning(
                "feishu",
                f"[FeishuGateway] 等待响应超时 ({timeout}s) request_id={request_id}",
            )
            await self._reply_to_feishu(msg, "[超时] 服务器未在规定时间内响应，请稍后重试")
        except RuntimeError as e:
            # _dispatch_loop 设置的异常（ws 断开、CoreScheduler 错误等）
            _rlog.error("feishu", f"[FeishuGateway] 响应异常 request_id={request_id}: {e}")
            await self._reply_to_feishu(msg, f"[系统错误] {e}")
        except Exception as e:
            _rlog.error("feishu", f"[FeishuGateway] _handle_and_stream 异常: {e}")
            await self._reply_to_feishu(msg, f"[未知错误] {e}")
        finally:
            # 清理 Future，避免内存泄漏
            async with self._pending_lock:
                self._pending_requests.pop(request_id, None)
            # 清理进度卡片状态，下一次任务将创建新卡片
            self._clear_progress_state(session_key)

    # ------------------------------------------------------------------
    # 进度卡片管理
    # ------------------------------------------------------------------

    # 节流间隔（秒）：连续 PATCH 请求之间的最小间隔
    _PROGRESS_THROTTLE_SEC = 1.5
    # 卡片内容最大字节数（飞书限制 ~30KB，留余量）
    _PROGRESS_MAX_BYTES = 25_000

    @staticmethod
    def _build_progress_card(lines: List[str]) -> Dict:
        """构建进度日志卡片

        使用飞书交互卡片，以 Markdown 代码块展示日志内容，
        视觉上类似 Web UI 的 <pre> 日志面板。

        Args:
            lines: 日志行列表

        Returns:
            飞书卡片 JSON dict
        """
        line_count = len(lines)
        log_text = "\n".join(lines)

        # 如果超过字节限制，截断最早的行
        while len(log_text.encode("utf-8")) > FeishuGateway._PROGRESS_MAX_BYTES and len(lines) > 1:
            lines = lines[1:]
            log_text = "\n".join(lines)

        # 用 Markdown 代码块包裹，确保等宽字体
        md_content = f"```\n{log_text}\n```"

        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"⏳ 运行日志 ({line_count} 行)",
                },
                "template": "turquoise",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": md_content},
                }
            ],
        }

    async def _on_cron_response(self, session_key: str, resp: Dict) -> None:
        """将 cron 任务完成后的主动消息发送给飞书用户/群。

        由 _dispatch_loop 在收到 type=="cron_response" 且无 request_id 的消息时调用。

        Args:
            session_key: 飞书 session 标识
            resp:        CoreScheduler 广播的 cron 响应 dict
        """
        content: str = resp.get("content", "")
        if not content:
            return
        receive_info = self._session_receive_ids.get(session_key)
        if not receive_info:
            _rlog.warning(
                "feishu",
                f"[FeishuGateway] _on_cron_response: 未找到 session_key={session_key} 的原始 ID 映射，跳过",
            )
            return
        receive_id, receive_id_type = receive_info
        cron_name: str = resp.get("cron_name", "")
        try:
            await self._feishu_client.send_text(receive_id, receive_id_type, content)
            _rlog.info(
                "feishu",
                f"[FeishuGateway] cron_response 已发送: session={session_key} cron={cron_name}",
            )
        except Exception as e:
            _rlog.error("feishu", f"[FeishuGateway] cron_response 发送失败 ({session_key}): {e}")

    async def _on_progress_update(self, session_key: str, progress: Dict) -> None:
        """将 CoreScheduler 的进度消息累积到一张可更新的飞书卡片中。

        首条进度 → send_card（创建新卡片）；后续进度 → update_card（PATCH 原地更新）。
        通过节流（最小间隔 _PROGRESS_THROTTLE_SEC）避免触发飞书 API 限流，
        并使用延迟 flush 确保最后一条更新一定被发送。

        Args:
            session_key: 飞书 session 标识
            progress:    进度消息 dict（含 content 字段）
        """
        content: str = progress.get("content", "")
        if not content:
            return

        # 串行化同一 session 的进度更新，防止并发 send_card 创建多张卡片
        if session_key not in self._progress_locks:
            self._progress_locks[session_key] = asyncio.Lock()
        async with self._progress_locks[session_key]:
            # 从映射中获取原始飞书 ID
            receive_info = self._session_receive_ids.get(session_key)
            if not receive_info:
                _rlog.warning(
                    "feishu",
                    f"[FeishuGateway] _on_progress_update: 未找到 session_key={session_key} 的原始 ID 映射，跳过",
                )
                return

            receive_id, receive_id_type = receive_info

            # 累积日志行
            if session_key not in self._progress_lines:
                self._progress_lines[session_key] = []
            self._progress_lines[session_key].append(content)
            lines = self._progress_lines[session_key]
            card = self._build_progress_card(lines)

            now = time.monotonic()
            last = self._progress_last_update.get(session_key, 0.0)
            card_msg_id = self._progress_card_ids.get(session_key)

            if card_msg_id is None:
                # 首条进度：发送新卡片
                try:
                    resp = await self._feishu_client.send_card(
                        receive_id=receive_id,
                        receive_id_type=receive_id_type,
                        card=card,
                    )
                    msg_id = resp.get("data", {}).get("message_id", "")
                    if msg_id:
                        self._progress_card_ids[session_key] = msg_id
                        self._progress_last_update[session_key] = now
                        _rlog.info(
                            "feishu",
                            f"[FeishuGateway] 进度卡片已创建: {session_key} msg_id={msg_id}",
                        )
                    else:
                        _rlog.warning(
                            "feishu",
                            f"[FeishuGateway] send_card 未返回 message_id: {resp}",
                        )
                except Exception as e:
                    _rlog.error("feishu", f"[FeishuGateway] 进度卡片创建失败 ({session_key}): {e}")
            elif now - last >= self._PROGRESS_THROTTLE_SEC:
                # 距上次更新已过节流间隔 → 立即 PATCH
                await self._patch_progress_card(session_key, card_msg_id, card)
                self._progress_last_update[session_key] = now
                # 取消已有的延迟 flush（因为刚刚已经发送了）
                self._cancel_flush_task(session_key)
            else:
                # 在节流窗口内 → 安排延迟 flush，确保最后一次更新不丢失
                self._schedule_flush(session_key, card_msg_id, card)

    async def _patch_progress_card(self, session_key: str, msg_id: str, card: Dict) -> None:
        """PATCH 更新进度卡片内容"""
        try:
            await self._feishu_client.update_card(msg_id, card)
        except Exception as e:
            _rlog.error("feishu", f"[FeishuGateway] 进度卡片更新失败 ({session_key}): {e}")

    def _schedule_flush(self, session_key: str, msg_id: str, card: Dict) -> None:
        """安排一个延迟 flush Task，在节流窗口结束后发送最新状态"""
        self._cancel_flush_task(session_key)

        async def _delayed_flush() -> None:
            await asyncio.sleep(self._PROGRESS_THROTTLE_SEC)
            # 发送时用最新的累积行重建卡片（flush 期间可能又有新行）
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
        """取消指定 session 的延迟 flush Task"""
        task = self._progress_flush_tasks.pop(session_key, None)
        if task and not task.done():
            task.cancel()

    def _clear_progress_state(self, session_key: str) -> None:
        """清除指定 session 的进度卡片状态（在最终响应发送后调用）"""
        self._cancel_flush_task(session_key)
        self._progress_card_ids.pop(session_key, None)
        self._progress_lines.pop(session_key, None)
        self._progress_last_update.pop(session_key, None)

    async def _reply_to_feishu(self, original_msg: Dict, response_text: str) -> None:
        """将 CoreScheduler 的响应发回飞书

        默认策略：
        - 回复原消息（reply_text），保持消息线程上下文
        - 可在 config.yaml 的 feishu.default_reply_type 中配置为 rich_text 等

        Args:
            original_msg: 触发本次对话的原始飞书消息
            response_text: 要发送的响应文本
        """
        message_id = original_msg.get("message_id", "")
        reply_type = self._feishu_config.get("default_reply_type", "text")

        try:
            if reply_type == "rich_text":
                # 将响应文本格式化为富文本（按换行拆分为多行）
                rows = self._text_to_rich_text_rows(response_text)
                if message_id:
                    await self._feishu_client.reply_rich_text(
                        message_id=message_id,
                        title="",
                        content_rows=rows,
                    )
                else:
                    await self._feishu_client.send_rich_text(
                        receive_id=original_msg.get("sender_open_id", ""),
                        receive_id_type="open_id",
                        title="",
                        content_rows=rows,
                    )
            else:
                # 默认：纯文本回复
                if message_id:
                    await self._feishu_client.reply_text(
                        message_id=message_id,
                        text=response_text,
                    )
                else:
                    chat_type = original_msg.get("chat_type", "p2p")
                    if chat_type == "p2p":
                        await self._feishu_client.send_text(
                            receive_id=original_msg.get("sender_open_id", ""),
                            receive_id_type="open_id",
                            text=response_text,
                        )
                    else:
                        await self._feishu_client.send_text(
                            receive_id=original_msg.get("chat_id", ""),
                            receive_id_type="chat_id",
                            text=response_text,
                        )

            _rlog.info(
                "feishu",
                f"[FeishuGateway] 已回复飞书消息（type={reply_type}）: {response_text[:50]}",
            )

        except Exception as e:
            _rlog.error("feishu", f"[FeishuGateway] 回复飞书消息失败: {e}")

    # ------------------------------------------------------------------
    # 主动发送 API（供业务层调用）
    # ------------------------------------------------------------------

    async def send_text(
        self,
        receive_id: str,
        receive_id_type: str,
        text: str,
    ) -> Dict:
        """主动发送文本消息

        Args:
            receive_id: 接收方 ID
            receive_id_type: ID 类型（"open_id" | "chat_id" 等）
            text: 消息文本

        Returns:
            飞书 API 响应 dict
        """
        return await self._feishu_client.send_text(receive_id, receive_id_type, text)

    async def send_rich_text(
        self,
        receive_id: str,
        receive_id_type: str,
        title: str,
        content_rows: List[List[Dict]],
        lang: str = "zh_cn",
    ) -> Dict:
        """主动发送富文本消息

        Args:
            receive_id: 接收方 ID
            receive_id_type: ID 类型
            title: 富文本标题
            content_rows: 富文本行内容（格式见 FeishuClient.send_rich_text 文档）
            lang: 语言代码

        Returns:
            飞书 API 响应 dict
        """
        return await self._feishu_client.send_rich_text(
            receive_id, receive_id_type, title, content_rows, lang
        )

    async def send_card(
        self,
        receive_id: str,
        receive_id_type: str,
        card: Dict,
    ) -> Dict:
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
        content: Optional[Dict] = None,
        card: Optional[Dict] = None,
        open_ids: Optional[List[str]] = None,
        user_ids: Optional[List[str]] = None,
        department_ids: Optional[List[str]] = None,
        union_ids: Optional[List[str]] = None,
    ) -> Dict:
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
    def _text_to_rich_text_rows(text: str) -> List[List[Dict]]:
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
