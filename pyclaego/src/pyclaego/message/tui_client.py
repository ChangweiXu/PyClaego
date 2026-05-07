"""PyClaego TUI 客户端 (PersonalSpace 协议)

基于 Textual 的富文本 TUI，专为 PS Gateway 协议测试设计。

功能
----
- 自动 ``open`` PS 并展示返回的 widget 列表
- ``/widget <id>`` 切换当前活动 widget
- 每条 ``chat`` 自带 ``request_id``，并跟踪 ack/reply 时延
- ``ack`` / ``reply`` / ``error`` / ``event`` 分色渲染，可切换 raw JSON 模式
- 支持 ``/reconnect``、``/open <ps>``、``/close``、``/list``、``/clear``、``/info``、
  ``/raw``、``/quit`` 等斜杠命令

协议
----
出: ``{type:"open"|"close"|"chat", request_id, ps_id, [widget_id], [content], [user_id]}``
入: ``{type:"ack"|"reply"|"error"|"event", request_id, ...}``
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

import websockets
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Input, Label, RichLog, Static


def _new_rid() -> str:
    return uuid.uuid4().hex[:12]


def _now_hms() -> str:
    return time.strftime("%H:%M:%S")


class StatusPanel(Static):
    """右侧状态面板：显示连接 / PS / widget 列表 / pending 请求。"""


class PSChatApp(App):
    """PS 协议 TUI 主应用。"""

    CSS = """
    Screen {
        background: $surface;
    }
    #body {
        height: 1fr;
    }
    #chat_log {
        width: 3fr;
        border: solid $primary;
        background: $surface-darken-1;
    }
    #right_panel {
        width: 1fr;
    }
    #status {
        height: auto;
        border: solid $accent;
        padding: 0 1;
        color: $text;
        background: $surface-darken-2;
    }
    #progress_label {
        background: $warning-darken-2;
        color: $text;
        padding: 0 1;
        height: 1;
    }
    #progress_log {
        height: 1fr;
        border: solid $warning;
        background: $surface-darken-2;
    }
    #message_input {
        dock: bottom;
        height: 3;
        border: solid $primary;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "退出"),
        Binding("ctrl+d", "quit", "退出"),
        Binding("ctrl+l", "clear_log", "清屏"),
        Binding("ctrl+r", "toggle_raw", "Raw JSON"),
    ]

    def __init__(
        self,
        server_url: str = "ws://127.0.0.1:8765",
        ps_id: str = "default",
        widget_id: str = "w_chat_default",
        user_id: str = "default_user",
    ) -> None:
        super().__init__()
        self.server_url = server_url
        self.ps_id = ps_id
        self.widget_id = widget_id
        self.user_id = user_id

        self.websocket: websockets.WebSocketClientProtocol | None = None
        self.connected: bool = False
        self.ps_opened: bool = False
        self.show_raw: bool = False

        # request_id -> dict(action, sent_at, content?)
        self._pending: dict[str, dict[str, Any]] = {}
        # 当前 PS 已知 widget_ids（来自 open ack）
        self._widget_ids: list[str] = []

        self._listener_task: asyncio.Task | None = None

        # UI refs
        self.log_widget: RichLog | None = None
        self.status_widget: StatusPanel | None = None
        self.progress_widget: RichLog | None = None
        self.input_widget: Input | None = None

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            yield RichLog(id="chat_log", wrap=True, highlight=True, markup=True)
            with Vertical(id="right_panel"):
                yield StatusPanel(id="status")
                yield Label(" Progress Updates", id="progress_label")
                yield RichLog(id="progress_log", wrap=True, highlight=False, markup=True)
        yield Input(placeholder="输入消息或 /help 查看命令…", id="message_input")
        yield Footer()

    async def on_mount(self) -> None:
        self.title = "PyClaego TUI"
        self.sub_title = f"PS={self.ps_id}  widget={self.widget_id}"

        self.log_widget = self.query_one("#chat_log", RichLog)
        self.status_widget = self.query_one("#status", StatusPanel)
        self.progress_widget = self.query_one("#progress_log", RichLog)
        self.input_widget = self.query_one("#message_input", Input)

        self._sys(f"连接 [bold]{self.server_url}[/bold] …")
        self._refresh_status()

        await self._connect_and_open()

        self.input_widget.focus()

    async def on_unmount(self) -> None:
        await self._cleanup_socket()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def _connect_and_open(self) -> None:
        try:
            self.websocket = await websockets.connect(
                self.server_url, max_size=20 * 1024 * 1024
            )
            self.connected = True
            self._sys("[green]✓ 已连接[/green]")
        except Exception as e:
            self.connected = False
            self._err(f"连接失败: {e}")
            self._sys("[dim]请确认 core_server 已启动 (python core_server.py)[/dim]")
            self._refresh_status()
            return

        # 启动消息监听
        self._listener_task = asyncio.create_task(self._recv_loop())

        # 自动 open PS
        await self._send_open(self.ps_id)
        self._refresh_status()

    async def _cleanup_socket(self) -> None:
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
            try:
                await self._listener_task
            except (asyncio.CancelledError, Exception):
                pass
        if self.websocket is not None:
            try:
                await self.websocket.close()
            except Exception:
                pass
        self.websocket = None
        self.connected = False
        self.ps_opened = False

    # ------------------------------------------------------------------
    # Protocol I/O
    # ------------------------------------------------------------------

    async def _send(self, msg: dict[str, Any]) -> bool:
        if not self.connected or self.websocket is None:
            self._err("未连接到服务器")
            return False
        try:
            await self.websocket.send(json.dumps(msg, ensure_ascii=False))
            return True
        except websockets.exceptions.ConnectionClosed:
            self.connected = False
            self.ps_opened = False
            self._err("WebSocket 已断开")
            self._refresh_status()
            return False

    async def _send_open(self, ps_id: str) -> None:
        rid = _new_rid()
        self._pending[rid] = {"action": "open", "sent_at": time.monotonic()}
        await self._send({"type": "open", "request_id": rid, "ps_id": ps_id})

    async def _send_close(self, ps_id: str) -> None:
        rid = _new_rid()
        self._pending[rid] = {"action": "close", "sent_at": time.monotonic()}
        await self._send({"type": "close", "request_id": rid, "ps_id": ps_id})

    async def _send_chat(self, content: str) -> None:
        rid = _new_rid()
        self._pending[rid] = {
            "action": "chat",
            "sent_at": time.monotonic(),
            "content": content,
        }
        ok = await self._send(
            {
                "type": "chat",
                "request_id": rid,
                "ps_id": self.ps_id,
                "widget_id": self.widget_id,
                "content": content,
                "user_id": self.user_id,
            }
        )
        if not ok:
            self._pending.pop(rid, None)
        else:
            self._refresh_status()

    async def _recv_loop(self) -> None:
        assert self.websocket is not None
        try:
            async for raw in self.websocket:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    self._err(f"无法解析: {raw!r}")
                    continue
                self._render(msg)
                self._refresh_status()
        except websockets.exceptions.ConnectionClosed:
            self.connected = False
            self.ps_opened = False
            self._err("✗ 与服务器的连接已断开")
            self._refresh_status()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._err(f"监听器错误: {e}")

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render(self, msg: dict[str, Any]) -> None:
        if self.show_raw:
            self._line("[dim cyan]raw[/dim cyan]", json.dumps(msg, ensure_ascii=False))
            return

        t = msg.get("type")
        rid = msg.get("request_id", "")
        latency = self._take_latency(rid)
        lat_tag = f" [dim]({latency*1000:.0f}ms)[/dim]" if latency is not None else ""

        if t == "ack":
            action = msg.get("action") or ""
            extra = ""
            if action == "open":
                wids = msg.get("widget_ids") or []
                if isinstance(wids, list):
                    self._widget_ids = list(wids)
                    self.ps_opened = True
                    if self.widget_id not in self._widget_ids and self._widget_ids:
                        # 默认选第一个
                        self.widget_id = self._widget_ids[0]
                        self.sub_title = f"PS={self.ps_id}  widget={self.widget_id}"
                    extra = f" widgets=[{', '.join(self._widget_ids)}]"
            elif action == "close":
                self.ps_opened = False
                self._widget_ids = []
            self._line(
                "[bold blue]ack[/bold blue]",
                f"{action} [dim]rid={rid}[/dim]{lat_tag}{extra}",
            )
            return

        if t == "reply":
            wid = msg.get("widget_id", "?")
            content = msg.get("content", "")
            cancelled = msg.get("cancelled")
            tag_text = f"[bold green]{wid}[/bold green]"
            if cancelled:
                tag_text += " [yellow](cancelled)[/yellow]"
            self._line(tag_text + lat_tag, content)
            return

        if t == "error":
            code = msg.get("code", "?")
            content = msg.get("message") or msg.get("content") or ""
            self._line(
                f"[bold red]error:{code}[/bold red]" + lat_tag,
                content,
            )
            return

        if t == "event":
            ev_type: str = msg.get("event", "")

            if ev_type == "query.opened":
                self._render_query_opened(msg)
                return

            if ev_type in ("query.resolved", "query.cleared"):
                self._render_query_closed(msg)
                return

            self._line(
                "[magenta]event[/magenta]",
                json.dumps(msg, ensure_ascii=False),
            )
            return

        if t == "progress_update":
            msg_ps = msg.get("ps_id") or msg.get("session_id", "")
            if msg_ps and msg_ps != self.ps_id:
                # Filtered: belongs to a different personal space
                return
            content = msg.get("content", "")
            self._progress(msg_ps or self.ps_id, content)
            return

        self._line("[dim]?[/dim]", json.dumps(msg, ensure_ascii=False))

    def _take_latency(self, rid: str) -> float | None:
        info = self._pending.get(rid)
        if not info:
            return None
        # 只在 reply / error 时弹出
        return time.monotonic() - info["sent_at"]

    def _line(self, tag: str, body: str) -> None:
        if self.log_widget is None:
            return
        self.log_widget.write(f"[dim]{_now_hms()}[/dim] {tag} {body}")

    def _sys(self, body: str) -> None:
        self._line("[dim cyan]sys[/dim cyan]", body)

    def _err(self, body: str) -> None:
        self._line("[bold red]err[/bold red]", body)

    def _progress(self, ps_id: str, content: str) -> None:
        """Write a progress update line to the right-sidebar progress log."""
        if self.progress_widget is None:
            return
        self.progress_widget.write(
            f"[dim]{_now_hms()}[/dim] [dim yellow]{ps_id}[/dim yellow] {content}"
        )

    def _render_query_opened(self, msg: dict[str, Any]) -> None:
        """Render a query.opened event as a distinct confirmation prompt."""
        if self.log_widget is None:
            return
        prompt: str = msg.get("prompt", "")
        choices: list = msg.get("choices", [])
        tool_name: str = msg.get("tool_name") or ""
        origin: str = msg.get("origin") or ""

        header_parts = ["[bold yellow]🔒 QUERY[/bold yellow]"]
        if tool_name:
            badge = "rule" if origin == "rule" else "tool"
            header_parts.append(f"[dim]tool=[/dim][cyan]{tool_name}[/cyan][dim] ({badge})[/dim]")
        self.log_widget.write("─" * 50)
        self.log_widget.write(f"[dim]{_now_hms()}[/dim] {' '.join(header_parts)}")
        self.log_widget.write(prompt)
        for i, c in enumerate(choices):
            desc = f"  [dim]{c['description']}[/dim]" if c.get("description") else ""
            self.log_widget.write(
                f"  [bold]{i + 1}.[/bold] [green]{c['label']}[/green] "
                f"[dim]({c['value']})[/dim]{desc}"
            )
        self.log_widget.write(
            "[dim]Reply with number (e.g. [bold]1[/bold]) or choice value[/dim]"
        )
        self.log_widget.write("─" * 50)

    def _render_query_closed(self, msg: dict[str, Any]) -> None:
        """Render a query.resolved or query.cleared event."""
        if self.log_widget is None:
            return
        ev_type: str = msg.get("event", "")
        if ev_type == "query.cleared":
            self.log_widget.write(
                f"[dim]{_now_hms()}[/dim] [yellow]⚠ Query cancelled[/yellow]"
            )
        else:
            value: str = msg.get("value", "?")
            by: str = msg.get("by", "")
            by_tag = f" [dim](by {by})[/dim]" if by else ""
            self.log_widget.write(
                f"[dim]{_now_hms()}[/dim] [green]✓ Query resolved:[/green] "
                f"[bold]{value}[/bold]{by_tag}"
            )

    # ------------------------------------------------------------------
    # Status panel
    # ------------------------------------------------------------------

    def _refresh_status(self) -> None:
        if self.status_widget is None:
            return
        conn = "[green]●[/green] connected" if self.connected else "[red]●[/red] offline"
        ps_state = "[green]opened[/green]" if self.ps_opened else "[yellow]not opened[/yellow]"
        widgets = (
            "\n  ".join(
                ("[reverse]" + w + "[/reverse]") if w == self.widget_id else w
                for w in self._widget_ids
            )
            if self._widget_ids
            else "[dim](unknown — open PS first)[/dim]"
        )
        pending_count = len(self._pending)
        # 仅显示最近几条 pending
        pending_lines: list[str] = []
        for rid, info in list(self._pending.items())[-5:]:
            elapsed = time.monotonic() - info["sent_at"]
            preview = info.get("content") or info.get("action", "")
            preview = (preview[:24] + "…") if len(preview) > 25 else preview
            pending_lines.append(
                f"  [dim]{rid}[/dim] {preview} [dim]{elapsed:0.1f}s[/dim]"
            )
        pending_block = "\n".join(pending_lines) if pending_lines else "  [dim](none)[/dim]"

        self.status_widget.update(
            "[bold]Connection[/bold]\n"
            f"  {conn}\n"
            f"  server: [dim]{self.server_url}[/dim]\n"
            "\n"
            "[bold]Personal Space[/bold]\n"
            f"  ps_id : {self.ps_id}\n"
            f"  state : {ps_state}\n"
            f"  user  : {self.user_id}\n"
            "\n"
            "[bold]Widgets[/bold]\n"
            f"  {widgets}\n"
            "\n"
            f"[bold]Pending[/bold] ({pending_count})\n"
            f"{pending_block}\n"
            "\n"
            f"[dim]raw mode: {'on' if self.show_raw else 'off'}[/dim]"
        )

    # ------------------------------------------------------------------
    # Input / commands
    # ------------------------------------------------------------------

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = (event.value or "").strip()
        event.input.value = ""
        if not text:
            return

        if text.startswith("/"):
            await self._handle_command(text)
            return

        if not self.connected:
            self._err("未连接，输入 /reconnect 重试")
            return
        if not self.ps_opened:
            self._err("PS 未打开，输入 /open <ps_id> 或 /reconnect")
            return

        # 显示用户消息
        self._line(f"[bold cyan]{self.user_id}[/bold cyan]", text)
        await self._send_chat(text)

    async def _handle_command(self, text: str) -> None:
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("/quit", "/exit", "/q"):
            await self._cleanup_socket()
            self.exit()
            return

        if cmd == "/help":
            self._sys(
                "命令列表：\n"
                "\n"
                "[客户端命令]\n"
                "  /quit | /q                 退出\n"
                "  /info                      显示当前配置\n"
                "  /list                      重新展示已知 widget 列表\n"
                "  /widget <id>               切换当前活动 widget\n"
                "  /open <ps_id>              切换 PS 并重新 open（清空进度面板）\n"
                "  /close                     关闭当前 PS\n"
                "  /reconnect                 重连服务器\n"
                "  /clear                     清空聊天日志和进度面板\n"
                "  /raw                       切换 raw JSON 渲染\n"
                "  /user <user_id>            切换 user_id\n"
                "  /send <json>               发送原始 JSON 帧（高级）\n"
                "  右侧下方面板               实时显示 progress_update 消息（按当前 ps_id 过滤）\n"
                "\n"
                "[服务端命令 — 发至当前 widget 处理]\n"
                "  /stop                      取消当前正在运行的 agent 任务\n"
                "  /compress                  手动触发记忆压缩（等待 agent 完成后执行）\n"
                "  /rebuild_index             重建记忆 SQLite 索引（等待 agent 完成后执行）\n"
                "  /memories            ⚡    查看记忆资产概览（SoulV6，立即执行）\n"
                "  /pin <tool_call_id>  ⚡    固定工具结果（SoulV6，立即执行）\n"
                "  /unpin <tool_call_id> ⚡   取消固定（SoulV6，立即执行）\n"
                "  /close_loop <q>            关闭匹配的 open loop（SoulV6）\n"
                "  /forget <md_path>          归档指定记忆文档（SoulV6）\n"
                "  /why <query>         ⚡    解释最近一次记忆召回来源（SoulV6，立即执行）\n"
                "  /export_memory <dir> ⚡    导出记忆目录（SoulV6，立即执行）\n"
                "  其他未知 /cmd              自动转发到服务端，由 CommandDispatcher 处理\n"
                "\n"
                "  ⚡ = 可在 agent 运行时立即响应（不等待任务锁）"
            )
            return

        if cmd == "/info":
            self._sys(
                f"server={self.server_url} ps={self.ps_id} widget={self.widget_id} "
                f"user={self.user_id} connected={self.connected} ps_opened={self.ps_opened}"
            )
            return

        if cmd == "/clear":
            if self.log_widget:
                self.log_widget.clear()
            if self.progress_widget:
                self.progress_widget.clear()
            return

        if cmd == "/raw":
            self.show_raw = not self.show_raw
            self._sys(f"raw mode = {self.show_raw}")
            self._refresh_status()
            return

        if cmd == "/list":
            if self._widget_ids:
                self._sys(f"widgets: {', '.join(self._widget_ids)}")
            else:
                self._sys("(暂无 widget 信息，请先 /open <ps_id>)")
            return

        if cmd == "/widget":
            if not arg:
                self._err("用法: /widget <widget_id>")
                return
            if self._widget_ids and arg not in self._widget_ids:
                self._sys(
                    f"[yellow]警告[/yellow] {arg} 不在已知 widget 列表中，仍然切换"
                )
            self.widget_id = arg
            self.sub_title = f"PS={self.ps_id}  widget={self.widget_id}"
            self._sys(f"已切换 widget = {arg}")
            self._refresh_status()
            return

        if cmd == "/user":
            if not arg:
                self._err("用法: /user <user_id>")
                return
            self.user_id = arg
            self._sys(f"已切换 user_id = {arg}")
            self._refresh_status()
            return

        if cmd == "/open":
            target = arg or self.ps_id
            self.ps_id = target
            self.sub_title = f"PS={self.ps_id}  widget={self.widget_id}"
            self._sys(f"打开 PS = {target}")
            # Clear progress log when switching PS
            if self.progress_widget:
                self.progress_widget.clear()
            await self._send_open(target)
            self._refresh_status()
            return

        if cmd == "/close":
            self._sys(f"关闭 PS = {self.ps_id}")
            await self._send_close(self.ps_id)
            return

        if cmd == "/reconnect":
            self._sys("重新连接 …")
            await self._cleanup_socket()
            self._refresh_status()
            await self._connect_and_open()
            self._refresh_status()
            return

        if cmd == "/send":
            if not arg:
                self._err("用法: /send <json>")
                return
            try:
                payload = json.loads(arg)
            except json.JSONDecodeError as e:
                self._err(f"JSON 解析失败: {e}")
                return
            if isinstance(payload, dict) and "request_id" not in payload:
                payload["request_id"] = _new_rid()
            self._sys(f"→ {json.dumps(payload, ensure_ascii=False)}")
            await self._send(payload)
            return

        if cmd == "/stop":
            if not self.connected:
                self._err("未连接到服务器")
                return
            rid = _new_rid()
            self._pending[rid] = {"action": "stop", "sent_at": time.monotonic()}
            await self._send({
                "type": "control",
                "action": "stop",
                "request_id": rid,
                "ps_id": self.ps_id,
                "widget_id": self.widget_id,
            })
            return

        # 未知命令 — 转发到服务端作为 chat（服务端 CommandDispatcher 会处理）
        if not self.connected:
            self._err("未连接，输入 /reconnect 重试")
            return
        if not self.ps_opened:
            self._err("PS 未打开，输入 /open <ps_id> 或 /reconnect")
            return
        self._line(f"[bold cyan]{self.user_id}[/bold cyan]", text)
        await self._send_chat(text)

    # ------------------------------------------------------------------
    # Bindings
    # ------------------------------------------------------------------

    def action_clear_log(self) -> None:
        if self.log_widget:
            self.log_widget.clear()

    def action_toggle_raw(self) -> None:
        self.show_raw = not self.show_raw
        self._sys(f"raw mode = {self.show_raw}")
        self._refresh_status()


class TUIClient:
    """对外封装：与旧版 API 保持兼容（构造参数）。"""

    def __init__(
        self,
        server_url: str = "ws://127.0.0.1:8765",
        ps_id: str = "default",
        widget_id: str = "w_chat_default",
        user_id: str = "default_user",
        # 兼容旧调用：若调用方仍传 session_id，则把它当作 ps_id
        session_id: str | None = None,
    ) -> None:
        if session_id and ps_id == "default":
            ps_id = session_id
        self.app = PSChatApp(
            server_url=server_url,
            ps_id=ps_id,
            widget_id=widget_id,
            user_id=user_id,
        )

    async def start(self) -> None:
        await self.app.run_async()

    def run(self) -> None:
        self.app.run()
