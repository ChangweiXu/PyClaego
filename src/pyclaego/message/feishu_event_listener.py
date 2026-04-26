"""飞书事件监听器 - 使用 lark-oapi SDK 的 WebSocket 长连接接收消息事件"""

import asyncio
import json
import threading
from collections import OrderedDict
from typing import Awaitable, Callable, Dict, Optional

from ..logging import get_running_log

_rlog = get_running_log()


# 内部标准化消息格式（传递给回调函数）
# {
#     "source": "feishu",
#     "event_id": str,         # 幂等 key
#     "chat_type": "p2p" | "group",
#     "chat_id": str,          # 群聊 chat_id（p2p 时也有）
#     "sender_open_id": str,
#     "message_id": str,       # 原始消息 ID，回复时使用
#     "msg_type": str,         # "text" | "image" | ...
#     "text": str,             # 解析后的纯文本（非文本消息为空字符串）
#     "raw_event": dict,       # 原始飞书事件数据
# }


class _LRUSet:
    """有上限的 LRU 集合，用于幂等去重"""

    def __init__(self, max_size: int = 1000) -> None:
        self._max_size = max_size
        self._data: OrderedDict = OrderedDict()

    def contains(self, key: str) -> bool:
        if key in self._data:
            self._data.move_to_end(key)
            return True
        return False

    def add(self, key: str) -> None:
        self._data[key] = True
        self._data.move_to_end(key)
        if len(self._data) > self._max_size:
            self._data.popitem(last=False)


def _parse_message_event(data) -> Optional[Dict]:
    """将飞书 P2ImMessageReceiveV1 事件对象解析为内部标准化格式

    lark_oapi SDK 回调传入的是强类型对象（P2ImMessageReceiveV1），
    其 .event 字段为 P2ImMessageReceiveV1Data，包含：
      - .sender (EventSender)  → .sender_id (UserId) → .open_id
      - .message (EventMessage) → .message_id / .chat_id / .chat_type
                                  / .message_type / .content

    Args:
        data: lark_oapi P2ImMessageReceiveV1 事件对象

    Returns:
        标准化消息 dict，解析失败返回 None
    """
    try:
        # 取 event 字段（P2ImMessageReceiveV1Data 对象）
        event = getattr(data, "event", None)
        if event is None:
            _rlog.warning("feishu", "[FeishuEventListener] 事件对象无 event 字段")
            return None

        # 解析 sender
        sender_obj = getattr(event, "sender", None)
        sender_id_obj = getattr(sender_obj, "sender_id", None) if sender_obj else None
        sender_open_id: str = getattr(sender_id_obj, "open_id", "") or ""

        # 解析 message
        msg_obj = getattr(event, "message", None)
        if msg_obj is None:
            _rlog.warning("feishu", "[FeishuEventListener] 事件对象无 message 字段")
            return None

        message_id: str = getattr(msg_obj, "message_id", "") or ""
        chat_id: str = getattr(msg_obj, "chat_id", "") or ""
        chat_type: str = getattr(msg_obj, "chat_type", "p2p") or "p2p"
        # EventMessage 用 message_type 字段（非 msg_type）
        msg_type: str = getattr(msg_obj, "message_type", "text") or "text"
        raw_content: str = getattr(msg_obj, "content", "{}") or "{}"

        # 解析消息文本和图片内容
        text = ""
        image_keys: list = []  # 待下载的图片 key 列表（按顺序）
        ordered_parts: list = []  # 有序内容段落：{type: text/image_key, ...}
        try:
            content_obj = json.loads(raw_content)
            if msg_type == "text":
                text = content_obj.get("text", "")
                if text:
                    ordered_parts = [{"type": "text", "text": text}]
            elif msg_type == "image":
                img_key = content_obj.get("image_key", "")
                if img_key:
                    image_keys = [img_key]
                    ordered_parts = [{"type": "image_key", "image_key": img_key}]
            elif msg_type == "post":
                # 富文本取所有段落文字和图片
                _rlog.debug("feishu", f"[FeishuEventListener] post raw_content: {raw_content}")
                post_val = content_obj.get("post", None)
                # 兼容两种结构：
                #   A) {"post": {"zh_cn": {"title": "", "content": [[...]]}}}
                #   B) {"post": {"title": "", "content": [[...]]}}
                if post_val is None:
                    # 没有 post 字段，尝试直接从顶层解析
                    rows = content_obj.get("content", [])
                    lang_contents = [content_obj]
                elif isinstance(post_val, dict):
                    # 判断是否有语言嵌套
                    first_val = next(iter(post_val.values()), None) if post_val else None
                    if isinstance(first_val, dict) and "content" in first_val:
                        # 结构 A：有语言嵌套
                        lang_contents = list(post_val.values())
                    else:
                        # 结构 B：直接是 {"title": ..., "content": ...}
                        lang_contents = [post_val]
                else:
                    lang_contents = []

                for lang_content in lang_contents:
                    rows = lang_content.get("content", [])
                    # 按行顺序收集 ordered_parts，相邻文本行合并
                    pending_text_parts: list = []
                    for row in rows:
                        row_texts = []
                        row_images = []
                        for elem in row:
                            tag = elem.get("tag", "")
                            if tag == "text":
                                t = elem.get("text", "")
                                if t:
                                    row_texts.append(t)
                            elif tag == "img":
                                img_key = elem.get("image_key", "")
                                if img_key:
                                    row_images.append(img_key)
                        if row_texts:
                            pending_text_parts.extend(row_texts)
                        if row_images:
                            # 先把堆积的文本刷出去
                            if pending_text_parts:
                                ordered_parts.append({"type": "text", "text": " ".join(pending_text_parts)})
                                pending_text_parts = []
                            for img_key in row_images:
                                ordered_parts.append({"type": "image_key", "image_key": img_key})
                                image_keys.append(img_key)
                    # 余下文本
                    if pending_text_parts:
                        ordered_parts.append({"type": "text", "text": " ".join(pending_text_parts)})
                    # text 字段 = 所有文本段落拼接（用于日志）
                    text = " ".join(
                        p["text"] for p in ordered_parts if p["type"] == "text"
                    )
                    break
        except (json.JSONDecodeError, AttributeError):
            text = raw_content

        # event_id 从 header 取（v2 格式）
        header = getattr(data, "header", None)
        event_id: str = (getattr(header, "event_id", "") or "") if header else ""

        return {
            "source": "feishu",
            "event_id": event_id,
            "chat_type": chat_type,
            "chat_id": chat_id,
            "sender_open_id": sender_open_id,
            "message_id": message_id,
            "msg_type": msg_type,
            "text": text,
            "image_keys": image_keys,
            "ordered_parts": ordered_parts,
            "raw_event": data,
        }
    except Exception as e:
        _rlog.error("feishu", f"[FeishuEventListener] 事件解析失败: {e}")
        return None


class FeishuEventListener:
    """飞书消息事件监听器

    使用 lark-oapi SDK 的 WebSocket 长连接，在后台线程订阅
    im.message.receive_v1 事件，并通过 on_message 回调通知调用方。

    特性：
    - 基于官方 SDK 长连接，无需公网 IP 和 Webhook 配置
    - 事件幂等去重（通过 event_id LRU 缓存）
    - 自动过滤机器人自身发送的消息
    - 异步回调集成（将同步事件桥接到 asyncio 事件循环）
    """

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        on_message: Callable[[Dict], Awaitable[None]],
        encrypt_key: str = "",
        verification_token: str = "",
        bot_open_id: str = "",
        dedupe_cache_size: int = 1000,
    ) -> None:
        """初始化飞书事件监听器

        Args:
            app_id: 飞书应用 App ID
            app_secret: 飞书应用 App Secret
            on_message: 收到消息后的异步回调函数，参数为标准化消息 dict
            encrypt_key: 事件加密 Key（长连接方式可选）
            verification_token: 事件验证 Token（长连接方式可选）
            bot_open_id: 机器人自身的 open_id，用于过滤自身消息（可选）
            dedupe_cache_size: 幂等去重 LRU 缓存大小，默认 1000
        """
        self._app_id = app_id
        self._app_secret = app_secret
        self._on_message = on_message
        self._encrypt_key = encrypt_key
        self._verification_token = verification_token
        self._bot_open_id = bot_open_id
        self._dedup_set = _LRUSet(max_size=dedupe_cache_size)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws_client = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def _build_ws_client(self):
        """构建飞书 WebSocket 客户端（延迟导入 lark_oapi）"""
        try:
            import lark_oapi as lark  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "[FeishuEventListener] 需要安装 lark-oapi 包: pip install lark-oapi"
            ) from exc

        def on_message_receive(data) -> None:
            """同步事件处理函数，由 SDK 在其线程中调用

            data 为 lark_oapi P2ImMessageReceiveV1 强类型对象，
            直接传给 _parse_message_event 做属性访问解析。
            """
            try:
                # 从 header 取 event_id（v2 格式）；v1 格式兼容 uuid
                header = getattr(data, "header", None)
                event_id: str = (getattr(header, "event_id", "") or "") if header else ""
                if not event_id:
                    event_id = getattr(data, "uuid", "") or ""

                # 幂等去重
                if event_id and self._dedup_set.contains(event_id):
                    _rlog.info("feishu", f"[FeishuEventListener] 跳过重复事件: {event_id}")
                    return
                if event_id:
                    self._dedup_set.add(event_id)

                # 解析内部格式（传入完整 data 对象，属性访问方式）
                msg = _parse_message_event(data)
                if msg is None:
                    return

                # 过滤机器人自身消息
                if self._bot_open_id and msg["sender_open_id"] == self._bot_open_id:
                    _rlog.info(
                        "feishu", "[FeishuEventListener] 过滤机器人自身消息"
                    )
                    return

                # 桥接到 asyncio 事件循环
                if self._loop and not self._loop.is_closed():
                    asyncio.run_coroutine_threadsafe(
                        self._on_message(msg), self._loop
                    )

            except Exception as e:
                _rlog.error("feishu", f"[FeishuEventListener] 消息处理异常: {e}")

        # 构建事件分发器
        handler = (
            lark.EventDispatcherHandler.builder(
                self._encrypt_key,
                self._verification_token,
            )
            .register_p2_im_message_receive_v1(on_message_receive)
            .build()
        )

        # 构建 WebSocket 客户端
        ws_client = lark.ws.Client(
            self._app_id,
            self._app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.INFO,
        )
        return ws_client

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """在后台线程中启动 WebSocket 长连接

        Args:
            loop: 调用方的 asyncio 事件循环，用于回调桥接

        注意：此方法非阻塞，会立即返回，WebSocket 连接在后台线程维持。

        实现说明：
        lark_oapi.ws.Client 内部使用一个模块级全局 event loop（在 import 时通过
        asyncio.get_event_loop() 获取），其 start() 方法通过 loop.run_until_complete()
        驱动协程。若直接在已有 asyncio 主循环的线程中调用会引发
        "This event loop is already running"，并产生
        "RuntimeWarning: coroutine '_connect' was never awaited"。

        解决方案：在后台线程中先创建并设置一个全新的 event loop，再通过
        lark_oapi.ws.client 模块直接替换其全局 `loop` 变量，使 SDK 内部的
        run_until_complete / create_task 调用都落在这个新 loop 上。
        """
        self._loop = loop
        self._ws_client = self._build_ws_client()

        def _run():
            _rlog.info("feishu", "[FeishuEventListener] WebSocket 线程启动")
            # 为本线程创建独立 event loop，避免与主线程 asyncio loop 冲突
            thread_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(thread_loop)

            # 替换 lark_oapi.ws.client 模块级全局 loop，使 SDK 内部的
            # loop.run_until_complete / loop.create_task 使用本线程的 loop
            try:
                import lark_oapi.ws.client as _lark_ws_client  # type: ignore
                _lark_ws_client.loop = thread_loop
            except Exception as patch_err:
                _rlog.warning(
                    "feishu",
                    f"[FeishuEventListener] 无法替换 lark_oapi loop 变量: {patch_err}",
                )

            try:
                self._ws_client.start()
            except Exception as e:
                if not self._stop_event.is_set():
                    _rlog.error(
                        "feishu", f"[FeishuEventListener] WebSocket 异常退出: {e}"
                    )
            finally:
                thread_loop.close()
            _rlog.info("feishu", "[FeishuEventListener] WebSocket 线程结束")

        self._thread = threading.Thread(target=_run, daemon=True, name="feishu-ws")
        self._thread.start()
        _rlog.info("feishu", "[FeishuEventListener] 后台线程已启动，等待飞书事件...")

    def stop(self) -> None:
        """停止监听"""
        self._stop_event.set()
        if self._ws_client:
            try:
                self._ws_client.stop()
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        _rlog.info("feishu", "[FeishuEventListener] 已停止")
