"""``PSGateway`` —— PersonalSpace 模型下的核心调度器（不感知 WebSocket）。

设计目标：
- **协议解耦**：不直接依赖 ``websockets``，由调用方提供 ``publish_fn``
  把出站消息送回客户端。便于纯 asyncio 单元测试。
- **PS / Widget 路由**：将入站 ``chat`` 消息按 ``ps_id`` / ``widget_id``
  路由到对应 ``Widget.process_message``。
- **连接计数**：维护 ``conn_id → set[ps_id]``；同一连接可订阅多个 PS。
  在 ``close_connection`` 时一次性 ``PS.close_connection`` 全部。
- **WidgetTaskHandler 注入**：在调用 widget 之前创建 ``WidgetTaskHandler``，让 Widget
  本身不依赖 ``TaskManager`` 单例（保持 Phase 1.2 的可测性约定）。
- **请求 fire-and-forget**：``handle_inbound`` 立即返回 ``ack``，真正的
  处理在后台 task 中跑；完成后通过 ``publish_fn`` 推送 ``reply`` /
  ``error``。这样客户端可以并发发多条消息（``/stop`` 等命令将来
  通过新的 ``type:"control"`` 即时入口处理）。

入站协议（v2）::

    {"conn_id": "...", "request_id": "...",
     "type": "open" | "close" | "chat" | "control",
     "ps_id": "alice", "widget_id": "w_chat_default",
     "content": "hi", "user_id": "default_user"}

出站协议（v2）::

    {"conn_id": "...", "request_id": "...",
     "type": "ack" | "reply" | "error" | "event",
     "ps_id": ..., "widget_id": ..., "content": ..., "timestamp": "...",
     ...}

Phase 2.1 仅实现 ``open`` / ``close`` / ``chat``。``control`` 留作 ``/stop``
等的占位，后续 phase 再接入。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from ..config import PYCLAEGO_DEFAULT_LOGS_ROOT
from ..logging import get_running_log
from ..personal_space import PersonalSpaceManager
from ..personal_space.stream_state import MAIN_AGENT, StreamStateManager
from ..security_executor.query_service import ResolveKind, get_query_service
from ..task_manager import (
    KIND_STREAM_CONTENT,
    TaskArtifactStore,
    TaskManager,
    TaskType,
    WidgetTaskHandler,
)

_rlog = get_running_log()


PublishFn = Callable[[str, dict[str, Any]], Awaitable[None]]
"""``async (conn_id, message) -> None``：把 ``message`` 发送到指定连接。
连接已断开时实现方应静默吞掉异常。"""


# 入站消息类型常量
T_OPEN = "open"
T_CLOSE = "close"
T_CHAT = "chat"
T_CONTROL = "control"

# 出站消息类型常量
R_ACK = "ack"
R_REPLY = "reply"
R_ERROR = "error"
R_EVENT = "event"


class PSGateway:
    """PersonalSpace 路由网关。线程不安全，按 asyncio 单循环约束使用。"""

    def __init__(
        self,
        ps_manager: PersonalSpaceManager,
        publish_fn: PublishFn,
        *,
        task_manager: TaskManager | None = None,
    ) -> None:
        self.ps_manager: PersonalSpaceManager = ps_manager
        self.publish: PublishFn = publish_fn
        self._task_manager: TaskManager | None = task_manager  # None → lazy

        # conn_id → 已 open 的 ps_id 集合
        self._conn_ps: dict[str, set[str]] = {}
        # ps_id → 已订阅的 conn_id 集合（_conn_ps 的反向索引）
        self._ps_conns: dict[str, set[str]] = {}
        # (ps_id, widget_id) → 当前并发处理中的请求计数
        # 只有计数归零时才发送 busy=False，避免并发请求提前清除忙碌状态
        self._widget_inflight: dict[tuple, int] = {}
        # 跟踪后台正在跑的 chat 任务，便于 shutdown 时 cancel
        self._inflight: set[asyncio.Task] = set()

        # 流式消息状态管理器（支持重连时回放历史 chunk）
        # data_dir 使用 PYCLAEGO_DEFAULT_LOGS_ROOT，持久化到 ~/.pyclaego/.logs/streams/
        self.stream_state: StreamStateManager = StreamStateManager(
            data_dir=PYCLAEGO_DEFAULT_LOGS_ROOT
        )

        # Register this gateway's publish_to_ps as the QueryService broadcast fn
        get_query_service().set_broadcast_fn(self.publish_to_ps)

    # ------------------------------------------------------------------
    # 连接生命周期
    # ------------------------------------------------------------------

    async def register_connection(self, conn_id: str) -> None:
        """新连接握手：仅在内存里登记 conn_id；不预绑定任何 PS。"""
        self._conn_ps.setdefault(conn_id, set())
        _rlog.info("core_service", f"[PSGateway] 连接已注册: {conn_id}")

    async def unregister_connection(self, conn_id: str) -> None:
        """连接断开：把该 conn 在所有 PS 上的引用计数还回去。"""
        ps_ids = self._conn_ps.pop(conn_id, set())
        for ps_id in ps_ids:
            try:
                await self.ps_manager.close_connection(conn_id, ps_id)
            except Exception:
                _rlog.exception(
                    "core_service", f"[PSGateway] close_connection({conn_id}, {ps_id}) 异常"
                )
            # Remove from reverse index
            conns = self._ps_conns.get(ps_id)
            if conns is not None:
                conns.discard(conn_id)
                if not conns:
                    del self._ps_conns[ps_id]
        _rlog.info("core_service", f"[PSGateway] 连接已注销: {conn_id}（释放 PS={sorted(ps_ids)}）")

    async def publish_to_ps(self, ps_id: str, msg: dict[str, Any]) -> None:
        """广播消息到所有已订阅该 PS 的连接。

        适用于与特定请求无关的出站消息（events、reply）；
        与特定请求绑定的消息（ack、error）仍走 self.publish(conn_id, ...)。
        """
        conn_ids = list(self._ps_conns.get(ps_id, ()))
        for cid in conn_ids:
            try:
                await self.publish(cid, msg)
            except Exception:
                _rlog.debug("core_service", f"[PSGateway] publish_to_ps: conn={cid} 不可达（已丢弃）")

    # ------------------------------------------------------------------
    # 入站分发
    # ------------------------------------------------------------------

    async def handle_inbound(self, conn_id: str, msg: dict[str, Any]) -> None:
        """处理一条入站消息。立即返回；耗时的 chat 在后台 task 中执行。"""
        if conn_id not in self._conn_ps:
            # 兼容：未注册的 conn 当成自动注册（测试 / 简单客户端方便）
            await self.register_connection(conn_id)

        msg_type = msg.get("type")
        request_id = msg.get("request_id", "")

        try:
            if msg_type == T_OPEN:
                await self._handle_open(conn_id, msg)
            elif msg_type == T_CLOSE:
                await self._handle_close(conn_id, msg)
            elif msg_type == T_CHAT:
                await self._handle_chat(conn_id, msg)
            elif msg_type == T_CONTROL:
                await self._handle_control(conn_id, msg)
            else:
                await self._send_error(
                    conn_id, request_id,
                    code="unknown_type",
                    message=f"未知消息类型: {msg_type!r}",
                )
        except Exception as e:
            _rlog.exception("core_service", f"[PSGateway] 分发异常 conn={conn_id} msg={msg_type}")
            await self._send_error(
                conn_id, request_id, code="internal", message=str(e)
            )

    # ------------------------------------------------------------------
    # 协议处理：open / close
    # ------------------------------------------------------------------

    async def _handle_open(self, conn_id: str, msg: dict[str, Any]) -> None:
        ps_id = msg.get("ps_id")
        request_id = msg.get("request_id", "")
        if not ps_id:
            await self._send_error(
                conn_id, request_id, code="bad_request",
                message="open 消息缺少 ps_id",
            )
            return
        try:
            ps = await self.ps_manager.open_connection(conn_id, ps_id)
        except ValueError as e:
            await self._send_error(
                conn_id, request_id, code="bad_ps_id", message=str(e)
            )
            return
        self._conn_ps.setdefault(conn_id, set()).add(ps_id)
        self._ps_conns.setdefault(ps_id, set()).add(conn_id)

        # ── 重连流回放 ──────────────────────────────────────────
        # 策略：新连接接入时，一次性将该 PS 下所有活跃流的完整历史
        # chunk 推入该连接的队列。后续新增 chunk 通过 publish_to_ps
        # 广播，自然衔接。客户端通过 seq 号去重。
        #
        # 若客户端携带 resume_streams（WS 重连且保有内存状态），
        # 则仅回放 last_seq 之后的增量，避免重复。
        #
        # resume_streams 支持两种格式:
        #   旧: {wid: {req: last_seq_int}}                         — 主 Agent
        #   新: {wid: {req: {subagent_id: last_seq}}}               — 多维
        resume_streams: dict[str, dict[str, int | dict[str, int]]] = (
            msg.get("resume_streams") or {}
        )
        replayed: dict[str, dict[str, int]] = {}  # widget_id → {request_id: replayed_count}

        if resume_streams:
            # ── 增量回放：客户端知道 last_seq ────────────────────
            for w_id, req_map in resume_streams.items():
                replayed[w_id] = {}
                for r_id, seq_val in req_map.items():
                    if isinstance(seq_val, dict):
                        # 新格式：{subagent_id: last_seq}
                        for s_id, last_seq in seq_val.items():
                            try:
                                chunks, _, _ = await self.stream_state.get_chunks_since(
                                    ps_id, w_id, r_id, last_seq=last_seq, subagent_id=s_id
                                )
                            except Exception:
                                chunks = []
                            for chunk in chunks:
                                try:
                                    await self.publish(conn_id, chunk)
                                except Exception:
                                    pass
                            replayed[w_id][f"{r_id}/{s_id}"] = len(chunks)
                    else:
                        # 旧格式：last_seq 为整数（主 Agent）
                        try:
                            chunks, _, _ = await self.stream_state.get_chunks_since(
                                ps_id, w_id, r_id, last_seq=seq_val, subagent_id=MAIN_AGENT
                            )
                        except Exception:
                            chunks = []
                        for chunk in chunks:
                            try:
                                await self.publish(conn_id, chunk)
                            except Exception:
                                pass
                        replayed[w_id][r_id] = len(chunks)
        else:
            # ── 全量回放：页面刷新等无状态场景 ──────────────────
            # 仅回放未结束（真正活跃）的流，已结束的流数据由 History REST 提供。
            try:
                active = await self.stream_state.get_active_streams(ps_id)
            except Exception:
                active = []
            for st in active:
                if st.get("finished"):
                    continue
                w_id = st["widget_id"]
                r_id = st["request_id"]
                try:
                    chunks, _, _ = await self.stream_state.get_chunks_since(
                        ps_id, w_id, r_id, last_seq=0, subagent_id=MAIN_AGENT
                    )
                except Exception:
                    chunks = []
                replayed.setdefault(w_id, {})[r_id] = len(chunks)
                for chunk in chunks:
                    try:
                        await self.publish(conn_id, chunk)
                    except Exception:
                        pass

        if replayed:
            _rlog.info(
                "core_service",
                f"[PSGateway] open: conn={conn_id} ps={ps_id} replayed={replayed}"
            )

        # ── 获取活跃流信息（供客户端知晓哪些流还在进行中） ──────────
        active_streams: list[dict[str, Any]] = []
        try:
            active_streams = await self.stream_state.get_active_streams(ps_id)
        except Exception:
            pass

        ack: dict[str, Any] = {
            "type": R_ACK,
            "conn_id": conn_id,
            "request_id": request_id,
            "ps_id": ps_id,
            "action": T_OPEN,
            "widget_ids": ps.list_widget_ids(),
            "timestamp": _now(),
        }
        if replayed:
            ack["replayed"] = replayed
        if active_streams:
            ack["active_streams"] = active_streams

        # ── 注入待处理 query（刷新重连时恢复 QueryPrompt 卡片）──────────
        # QueryService._queues 仍持有未 resolve 的 Future，重连时将队列
        # 序列化后附在 ack 里，前端 bridge 收到后重建 pendingQueries 状态。
        qs = get_query_service()
        pending_queries_map: dict[str, list[dict[str, Any]]] = {}
        for w_id in ps.list_widget_ids():
            pending = qs.get_pending(f"{ps_id}__{w_id}")
            if pending:
                pending_queries_map[w_id] = pending
        if pending_queries_map:
            ack["pending_queries"] = pending_queries_map

        await self.publish(conn_id, ack)

    async def _handle_close(self, conn_id: str, msg: dict[str, Any]) -> None:
        ps_id = msg.get("ps_id")
        request_id = msg.get("request_id", "")
        if not ps_id:
            await self._send_error(
                conn_id, request_id, code="bad_request",
                message="close 消息缺少 ps_id",
            )
            return
        await self.ps_manager.close_connection(conn_id, ps_id)
        self._conn_ps.get(conn_id, set()).discard(ps_id)
        self._ps_conns.get(ps_id, set()).discard(conn_id)
        await self.publish(conn_id, {
            "type": R_ACK,
            "conn_id": conn_id,
            "request_id": request_id,
            "ps_id": ps_id,
            "action": T_CLOSE,
            "timestamp": _now(),
        })

    # ------------------------------------------------------------------
    # 协议处理：chat
    # ------------------------------------------------------------------

    async def _handle_chat(self, conn_id: str, msg: dict[str, Any]) -> None:
        ps_id = msg.get("ps_id")
        widget_id = msg.get("widget_id")
        request_id = msg.get("request_id", "")
        if not ps_id or not widget_id:
            await self._send_error(
                conn_id, request_id, code="bad_request",
                message="chat 消息需要 ps_id 与 widget_id",
            )
            return

        # 立即 ack（让客户端确认服务端已收到）
        await self.publish(conn_id, {
            "type": R_ACK,
            "conn_id": conn_id,
            "request_id": request_id,
            "ps_id": ps_id,
            "widget_id": widget_id,
            "action": T_CHAT,
            "timestamp": _now(),
        })

        # 后台执行，不阻塞读取循环
        task = asyncio.create_task(
            self._dispatch_chat(conn_id, msg)
        )
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def _dispatch_chat(self, conn_id: str, msg: dict[str, Any]) -> None:
        ps_id: str = msg["ps_id"]
        widget_id: str = msg["widget_id"]
        request_id: str = msg.get("request_id", "")
        user_id: str = msg.get("user_id") or "default_user"
        content: str = msg.get("content") or ""

        # ── Query interception: if this widget has pending confirmations,
        # route the message to QueryService instead of the agent. ─────────
        session_id = f"{ps_id}__{widget_id}"
        qs = get_query_service()
        if qs.has_pending(session_id):
            outcome = await qs.try_resolve(session_id, content)
            if outcome.kind == ResolveKind.ACCEPTED:
                return  # future resolved; agent loop will resume
            if outcome.kind == ResolveKind.STOPPED:
                # /stop already cleared queue; now cancel the widget task
                # and clean up stream state
                await self._cancel_widget_and_cleanup_stream(ps_id, widget_id)
                return
            if outcome.kind == ResolveKind.REJECTED:
                # Send feedback to the sender only
                await self.publish(conn_id, {
                    "type": R_EVENT,
                    "event": "query.rejected",
                    "request_id": request_id,
                    "ps_id": ps_id,
                    "widget_id": widget_id,
                    "query_id": outcome.query_id,
                    "reason": outcome.reason,
                    "timestamp": _now(),
                })
                return
            # PASS_THROUGH — fall through to normal handling

        # 保证连接已开 PS（兼容直接发 chat 的客户端）
        if ps_id not in self._conn_ps.get(conn_id, set()):
            try:
                await self.ps_manager.open_connection(conn_id, ps_id)
                self._conn_ps.setdefault(conn_id, set()).add(ps_id)
                self._ps_conns.setdefault(ps_id, set()).add(conn_id)
            except ValueError as e:
                await self._send_error(
                    conn_id, request_id, code="bad_ps_id", message=str(e)
                )
                return

        try:
            ps = await self.ps_manager.get(ps_id)
            widget = await ps.get_widget(widget_id)
        except KeyError as e:
            await self._send_error(
                conn_id, request_id, code="widget_not_found", message=str(e)
            )
            return
        except Exception as e:
            _rlog.exception("core_service", f"[PSGateway] 取 widget 失败 ps={ps_id} w={widget_id}")
            await self._send_error(
                conn_id, request_id, code="widget_load_error", message=str(e)
            )
            return

        # 创建 WidgetTaskHandler（这里是唯一与 TaskManager 单例交互的地方）
        try:
            tm = self._task_manager or TaskManager.get_instance()
            task_id = await tm.create_task(
                session_id=widget.belongs_to.key(),
                task_type=TaskType.USER_MESSAGE,
                name=f"chat: {content[:40]}",
                parent_id=None,
                description="user chat via PSGateway",
                user_id=user_id,
                source="chat",
                ps_id=ps_id,
                widget_id=widget_id,
                request_id=request_id,
            )
            task_handler = WidgetTaskHandler.from_belonging(
                belonging=widget.belongs_to,
                user_id=user_id,
                task_id=task_id,
            )
        except Exception as e:
            _rlog.exception("core_service", "[PSGateway] 创建 WidgetTaskHandler 失败")
            await self._send_error(
                conn_id, request_id, code="task_create_failed", message=str(e)
            )
            return

        ps.inc_in_flight()
        wkey = (ps_id, widget_id)
        self._widget_inflight[wkey] = self._widget_inflight.get(wkey, 0) + 1
        # Notify all subscribers immediately so card status dots go busy.
        try:
            await self.publish_to_ps(ps_id, {
                "type": R_EVENT,
                "event": "widget.status_changed",
                "request_id": request_id,
                "ps_id": ps_id,
                "widget_id": widget_id,
                "busy": True,
                "timestamp": _now(),
            })
        except Exception:
            pass
        try:
            # ── 启动流状态跟踪 ────────────────────────────────────
            await self.stream_state.start_stream(ps_id, widget_id, request_id, subagent_id=MAIN_AGENT)

            # ── 构建流式回调工厂（主/子 Agent 共用） ──────────────
            def _make_stream_callback(subagent_id: str | None = None):
                """创建流式回调闭包。

                - subagent_id=None → 主 Agent（reply 不带 subagent_id 字段）
                - subagent_id=<str>  → 子 Agent（reply 带 subagent_id 字段）
                """
                s_id = subagent_id or MAIN_AGENT
                _round_count = 0
                _thinking_open = False
                _stream_terminated = False

                async def _on_chunk(chunk: Any) -> None:
                    """将 StreamChunk 转换为 WebSocket reply 消息并广播。"""
                    nonlocal _round_count, _thinking_open, _stream_terminated
                    chunk_type = getattr(chunk, "type", "unknown")
                    content_val = ""
                    extra: dict[str, Any] = {}

                    # ── 🆕 fail：流异常终止 ──────────────────────────
                    if chunk_type == "fail":
                        error_code = getattr(chunk, "error_code", "unknown") or "unknown"
                        error_message = getattr(chunk, "error_message", "") or ""
                        if _thinking_open:
                            _thinking_open = False
                        out_fail: dict[str, Any] = {
                            "type": R_REPLY, "request_id": request_id,
                            "ps_id": ps_id, "widget_id": widget_id,
                            "content": error_message, "source": "chat",
                            "done": True, "chunk_type": "fail",
                            "error_code": error_code,
                            "timestamp": _now(),
                        }
                        if subagent_id is not None:
                            out_fail["subagent_id"] = subagent_id
                        try:
                            seq = await self.stream_state.add_chunk(
                                ps_id, widget_id, request_id, out_fail, subagent_id=s_id
                            )
                            out_fail["seq"] = seq
                        except Exception:
                            pass
                        try:
                            await self.stream_state.end_stream(
                                ps_id, widget_id, request_id, subagent_id=s_id
                            )
                        except Exception:
                            pass
                        try:
                            await self.publish_to_ps(ps_id, out_fail)
                        except Exception:
                            pass
                        _stream_terminated = True
                        return

                    if chunk_type == "text_delta":
                        raw_text = getattr(chunk, "text_delta", "") or ""
                        if _thinking_open:
                            content_val = "\n\n</details>\n\n" + raw_text
                            _thinking_open = False
                        else:
                            content_val = raw_text
                    elif chunk_type == "thinking_delta":
                        raw = getattr(chunk, "thinking_delta", "") or ""
                        if raw:
                            if not _thinking_open:
                                content_val = "<details open><summary>💭 Thinking</summary>\n\n" + raw
                                _thinking_open = True
                            else:
                                content_val = raw
                    elif chunk_type == "tool_call_start":
                        if _thinking_open:
                            _thinking_open = False
                            try:
                                await self.publish_to_ps(ps_id, {
                                    "type": R_REPLY, "request_id": request_id,
                                    "ps_id": ps_id, "widget_id": widget_id,
                                    "content": "\n\n</details>\n\n",
                                    "source": "chat", "done": False,
                                    "chunk_type": "thinking_delta",
                                    "timestamp": _now(),
                                })
                            except Exception:
                                pass
                        extra["tool_call_name"] = getattr(chunk, "tool_call_name", "")
                        extra["tool_call_id"] = getattr(chunk, "tool_call_id", "")
                    elif chunk_type == "tool_call_end":
                        if _thinking_open:
                            _thinking_open = False
                            try:
                                await self.publish_to_ps(ps_id, {
                                    "type": R_REPLY, "request_id": request_id,
                                    "ps_id": ps_id, "widget_id": widget_id,
                                    "content": "\n\n</details>\n\n",
                                    "source": "chat", "done": False,
                                    "chunk_type": "thinking_delta",
                                    "timestamp": _now(),
                                })
                            except Exception:
                                pass
                        extra["tool_call_id"] = getattr(chunk, "tool_call_id", "")
                    elif chunk_type == "finish":
                        _round_count += 1
                        usage = getattr(chunk, "usage", {}) or {}
                        model = getattr(chunk, "produced_by_model", "") or ""
                        tokens = usage.get("total_tokens", usage.get("completion_tokens", 0))
                        sep_parts = [f"*Round {_round_count} complete"]
                        if model:
                            sep_parts.append(model)
                        if tokens:
                            sep_parts.append(f"{tokens} tokens")
                        if _thinking_open:
                            sep_content = "\n\n</details>\n\n---\n" + " · ".join(sep_parts)
                            _thinking_open = False
                        else:
                            sep_content = "---\n" + " · ".join(sep_parts)

                        out_sep: dict[str, Any] = {
                            "type": R_REPLY,
                            "request_id": request_id,
                            "ps_id": ps_id,
                            "widget_id": widget_id,
                            "content": sep_content,
                            "source": "chat",
                            "done": False,
                            "chunk_type": "round_separator",
                            "round": _round_count,
                            "timestamp": _now(),
                        }
                        if subagent_id is not None:
                            out_sep["subagent_id"] = subagent_id
                        try:
                            seq = await self.stream_state.add_chunk(
                                ps_id, widget_id, request_id, out_sep, subagent_id=s_id
                            )
                            out_sep["seq"] = seq
                        except Exception:
                            pass
                        try:
                            await self.publish_to_ps(ps_id, out_sep)
                        except Exception:
                            pass
                        return

                    # ── 构建通用 reply 消息 ──────────────────────────
                    if not content_val and not extra:
                        return

                    out: dict[str, Any] = {
                        "type": R_REPLY,
                        "request_id": request_id,
                        "ps_id": ps_id,
                        "widget_id": widget_id,
                        "content": content_val,
                        "source": "chat",
                        "done": False,
                        "chunk_type": chunk_type,
                        "timestamp": _now(),
                    }
                    if extra:
                        out.update(extra)
                    if subagent_id is not None:
                        out["subagent_id"] = subagent_id

                    try:
                        seq = await self.stream_state.add_chunk(
                            ps_id, widget_id, request_id, out, subagent_id=s_id
                        )
                        out["seq"] = seq
                    except Exception:
                        pass

                    try:
                        await self.publish_to_ps(ps_id, out)
                    except Exception:
                        pass

                # ── 暴露 _stream_terminated 供外部 finally 块判断 ──
                _on_chunk._stream_terminated = _stream_terminated  # type: ignore[attr-defined]

                # ── 后置钩子：流正常完成后统一处理 done + Artifact 保存 ──
                async def _finalize(agent_task_id: str = "") -> None:
                    """流正常完成后的后置处理。

                    由调用方（主 Agent 的 dispatch_chat 或子 Agent 的
                    request_subagent_call）在 Agent 返回后触发。
                    fail 路径（_stream_terminated）跳过。
                    """
                    if _stream_terminated:
                        return

                    # 1) 从 stream_state 读取并拼接完整流文本
                    stream_text = ""
                    try:
                        chunks, _, _ = await self.stream_state.get_chunks_since(
                            ps_id, widget_id, request_id, 0, subagent_id=s_id
                        )
                        if chunks:
                            stream_text = "".join(
                                c.get("content", "") for c in chunks if c.get("content")
                            )
                    except Exception:
                        pass

                    # 2) 发送 done:True（前端据此标记流结束）
                    out_done: dict[str, Any] = {
                        "type": R_REPLY, "request_id": request_id,
                        "ps_id": ps_id, "widget_id": widget_id,
                        "content": "",
                        "full_content": stream_text,
                        "source": "chat", "done": True,
                        "timestamp": _now(),
                    }
                    if subagent_id is not None:
                        out_done["subagent_id"] = subagent_id
                    try:
                        seq = await self.stream_state.add_chunk(
                            ps_id, widget_id, request_id, out_done, subagent_id=s_id
                        )
                        out_done["seq"] = seq
                    except Exception:
                        pass
                    try:
                        await self.stream_state.end_stream(
                            ps_id, widget_id, request_id, subagent_id=s_id
                        )
                    except Exception:
                        pass
                    try:
                        await self.publish_to_ps(ps_id, out_done)
                    except Exception:
                        pass

                    # 3) 保存完整流文本为 Task Artifact
                    if agent_task_id and stream_text.strip():
                        try:
                            TaskArtifactStore.get_instance().attach(
                                agent_task_id,
                                KIND_STREAM_CONTENT,
                                stream_text,
                                name="stream_content",
                                mime="text/markdown",
                            )
                        except Exception:
                            pass

                _on_chunk._finalize = _finalize  # type: ignore[attr-defined]
                return _on_chunk

            # ── 保存工厂供子 Agent spawn 使用 ──────────────────────
            self._stream_factory = _make_stream_callback  # type: ignore[assignment]

            # ── 为主 Agent 创建回调 ───────────────────────────────
            _on_stream_chunk = _make_stream_callback(None)
            _on_stream_chunk._factory = _make_stream_callback  # type: ignore[attr-defined]  # 供子 Agent 使用

            response = await widget.process_message(
                {"content": content, **(
                    {"content_parts": msg["content_parts"]}
                    if "content_parts" in msg else {}
                )},
                source="chat",
                request_id=request_id,
                task_handler=task_handler,
                user_id=user_id,
                stream_callback=_on_stream_chunk,
            )
            try:
                if response.get("cancelled"):
                    await tm.cancel_task(task_id)
                else:
                    await tm.complete_task(task_id, result=response)
            except Exception:
                _rlog.exception("core_service", "[PSGateway] complete_task 失败")

            # 🆕 若流已被 fail chunk 终止，跳过正常 done 路径
            # 使用回调闭包的 _stream_terminated 属性
            if getattr(_on_stream_chunk, '_stream_terminated', False):
                # fail chunk 已发送 done:True + end_stream，无需重复
                pass
            elif response.get("cancelled"):
                out_cancel = {
                    "type": R_REPLY,
                    "request_id": request_id,
                    "ps_id": ps_id,
                    "widget_id": widget_id,
                    "content": "",
                    "full_content": response.get("content", ""),
                    "source": response.get("source", "chat"),
                    "done": False,
                    "cancelled": True,
                    "timestamp": _now(),
                }
                # 任务被取消：级联删除该 request 下所有 Agent 流（不持久化）
                try:
                    await self.stream_state.delete_stream(
                        ps_id, widget_id, request_id, subagent_id=None
                    )
                except Exception:
                    pass
                try:
                    await self.publish_to_ps(ps_id, out_cancel)
                except Exception:
                    pass
            else:
                # ── 正常完成：_finalize 统一处理 done 信号 + Artifact 保存 ──
                if hasattr(_on_stream_chunk, '_finalize'):
                    try:
                        await _on_stream_chunk._finalize(agent_task_id=task_id)
                    except Exception:
                        pass

        except Exception as e:
            _rlog.exception("core_service", "[PSGateway] widget 处理失败")
            try:
                await tm.fail_task(task_id, str(e))
            except Exception:
                pass
            # 🆕 若 fail chunk 尚未发送（agent 异常穿透），清理流状态并通知前端
            if not getattr(_on_stream_chunk, '_stream_terminated', False):
                try:
                    await self.stream_state.delete_stream(
                        ps_id, widget_id, request_id, subagent_id=None
                    )
                except Exception:
                    pass
                await self._send_error(
                    conn_id, request_id, code="widget_error", message=str(e),
                    ps_id=ps_id, widget_id=widget_id,
                )
            return
        finally:
            ps.dec_in_flight()
            wkey = (ps_id, widget_id)
            remaining = self._widget_inflight.get(wkey, 1) - 1
            if remaining <= 0:
                self._widget_inflight.pop(wkey, None)
                # All concurrent requests for this widget are done — notify idle.
                try:
                    await self.publish_to_ps(ps_id, {
                        "type": R_EVENT,
                        "event": "widget.status_changed",
                        "request_id": request_id,
                        "ps_id": ps_id,
                        "widget_id": widget_id,
                        "busy": False,
                        "timestamp": _now(),
                    })
                except Exception:
                    pass
            else:
                self._widget_inflight[wkey] = remaining

    # ------------------------------------------------------------------
    # 协议处理：control
    # ------------------------------------------------------------------

    async def _cancel_widget_and_cleanup_stream(
        self, ps_id: str, widget_id: str
    ) -> None:
        """取消 widget 当前任务并清理 StreamStateManager 中对应的流记录。

        1. 从 widget 直接读取 current_request_id（必须在 cancel 之前）
        2. 取消 widget 的 asyncio 任务
        3. 从 StreamStateManager 精准删除该流（内存 + JSONL）
        4. 广播 stream_cancelled 事件
        5. 清理 QueryService 待处理队列

        异常全量捕获，不向调用方传播。
        """
        # 1. 取消 widget asyncio 任务
        try:
            ps = await self.ps_manager.get(ps_id)
            widget = await ps.get_widget(widget_id)
        except Exception:
            _rlog.exception("core_service", "[PSGateway] _cancel: 获取 widget 失败")
            return

        # 2. 从 widget 直接读取当前 request_id（必须在 cancel 之前，
        #    因为 cancel() 会触发 process_message 的 finally 将 _current_request_id 清为 None）
        req_id: str | None = getattr(widget, 'current_request_id', None)

        widget.cancel()

        # 3. 删除流
        if req_id:
            try:
                deleted = await self.stream_state.delete_stream(
                    ps_id, widget_id, req_id
                )
                _rlog.info(
                    "core_service",
                    f"[PSGateway] _cancel: stream deleted ps={ps_id} w={widget_id} req={req_id} found={deleted}"
                )
            except Exception:
                _rlog.exception("core_service", "[PSGateway] _cancel: delete_stream 失败")
        else:
            _rlog.warning(
                "core_service",
                f"[PSGateway] _cancel: 未找到 request_id for ps={ps_id} w={widget_id}"
            )

        # 4. 广播 stream_cancelled 事件
        try:
            await self.publish_to_ps(ps_id, {
                "type": R_EVENT,
                "event": "stream_cancelled",
                "ps_id": ps_id,
                "widget_id": widget_id,
                "request_id": req_id,
                "timestamp": _now(),
            })
        except Exception:
            pass

        # 5. 清理 QueryService
        try:
            session_id = f"{ps_id}__{widget_id}"
            await get_query_service().clear_all(session_id, reason="user_stop")
        except Exception:
            pass

    async def _handle_control(self, conn_id: str, msg: dict[str, Any]) -> None:
        """Handle control messages (e.g. action='stop')."""
        action = msg.get("action", "")
        request_id = msg.get("request_id", "")
        ps_id = msg.get("ps_id")
        widget_id = msg.get("widget_id")

        if action == "stop":
            if not ps_id or not widget_id:
                await self._send_error(
                    conn_id, request_id, code="bad_request",
                    message="control/stop 需要 ps_id 与 widget_id",
                )
                return
            # 统一清理：取消任务 + 删除流 + 广播事件 + 清 QueryService
            await self._cancel_widget_and_cleanup_stream(ps_id, widget_id)
            await self.publish(conn_id, {
                "type": R_ACK,
                "conn_id": conn_id,
                "request_id": request_id,
                "ps_id": ps_id,
                "widget_id": widget_id,
                "action": "stop",
                "cancelled": True,
                "timestamp": _now(),
            })
        else:
            await self._send_error(
                conn_id, request_id, code="unknown_action",
                message=f"未知 control action: {action!r}",
            )

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    async def _send_error(
        self,
        conn_id: str,
        request_id: str,
        *,
        code: str,
        message: str,
        ps_id: str | None = None,
        widget_id: str | None = None,
    ) -> None:
        msg: dict[str, Any] = {
            "type": R_ERROR,
            "conn_id": conn_id,
            "request_id": request_id,
            "code": code,
            "content": message,  # use "content" to match frontend PSReply type
            "timestamp": _now(),
        }
        if ps_id:
            msg["ps_id"] = ps_id
        if widget_id:
            msg["widget_id"] = widget_id
        try:
            await self.publish(conn_id, msg)
        except Exception:
            _rlog.exception("core_service", f"[PSGateway] publish error 失败 conn={conn_id}")

    async def shutdown(self) -> None:
        """关闭：取消所有未完成的 chat task。"""
        for t in list(self._inflight):
            t.cancel()
        if self._inflight:
            await asyncio.gather(*self._inflight, return_exceptions=True)
        self._inflight.clear()

    # ------------------------------------------------------------------
    # 自省
    # ------------------------------------------------------------------

    def list_connections(self) -> dict[str, set[str]]:
        return {cid: set(ps_ids) for cid, ps_ids in self._conn_ps.items()}

    def list_ps_subscribers(self) -> dict[str, set[str]]:
        """Return a copy of the ps_id → conn_ids reverse index (for introspection)."""
        return {ps_id: set(conns) for ps_id, conns in self._ps_conns.items()}


def _now() -> str:
    return datetime.now().isoformat()


__all__ = [
    "R_ACK",
    "R_ERROR",
    "R_EVENT",
    "R_REPLY",
    "T_CHAT",
    "T_CLOSE",
    "T_CONTROL",
    "T_OPEN",
    "PSGateway",
    "PublishFn",
]
