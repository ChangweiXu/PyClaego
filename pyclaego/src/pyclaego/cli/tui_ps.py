"""PyClaego TUI 客户端 (v2 - PersonalSpace 协议)

最小化 TUI：只展示一个 chat widget，让你能跟它对话。

用法
----
    python pyclaego/tui_ps.py
    python pyclaego/tui_ps.py -p alice         # ps_id
    python pyclaego/tui_ps.py -p alice -u me   # ps_id + user_id
    python pyclaego/tui_ps.py -w w_chat_default
    python pyclaego/tui_ps.py --server ws://127.0.0.1:8765

协议（与 src/core/ps_gateway.py 对应）
-------------------------------------
    出: {type:"open",  request_id, ps_id}
    出: {type:"chat",  request_id, ps_id, widget_id, content, user_id}
    入: {type:"ack"|"reply"|"error"|"event", ...}
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from typing import Any

import websockets

# ANSI 颜色（连不上时自动降级；macOS 默认 zsh OK）
_C_USER = "\033[36m"  # cyan
_C_BOT = "\033[32m"   # green
_C_SYS = "\033[90m"   # gray
_C_ERR = "\033[31m"   # red
_C_RST = "\033[0m"


def _print(prefix: str, text: str, color: str = "") -> None:
    sys.stdout.write(f"{color}{prefix}{_C_RST} {text}\n")
    sys.stdout.flush()


def _new_rid() -> str:
    return uuid.uuid4().hex[:12]


class PSChatTUI:
    def __init__(
        self,
        server_url: str,
        ps_id: str,
        widget_id: str,
        user_id: str,
    ) -> None:
        self.server_url = server_url
        self.ps_id = ps_id
        self.widget_id = widget_id
        self.user_id = user_id

        self._ws: websockets.WebSocketClientProtocol | None = None
        # request_id -> 是否已收到 reply（仅用于"已发送, 等回复"提示）
        self._pending: dict[str, str] = {}
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    async def run(self) -> None:
        _print("[sys]", f"连接 {self.server_url} ...", _C_SYS)
        try:
            async with websockets.connect(self.server_url, max_size=20 * 1024 * 1024) as ws:
                self._ws = ws
                _print("[sys]", "已连接。", _C_SYS)

                # open PS
                rid = _new_rid()
                await self._send({
                    "type": "open",
                    "request_id": rid,
                    "ps_id": self.ps_id,
                })

                # 并行：接收 + 输入
                recv_task = asyncio.create_task(self._recv_loop())
                input_task = asyncio.create_task(self._input_loop())
                done, pending = await asyncio.wait(
                    {recv_task, input_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                for t in done:
                    exc = t.exception()
                    if exc:
                        raise exc
        except (websockets.exceptions.ConnectionClosedError, ConnectionRefusedError) as e:
            _print("[err]", f"连接断开: {e}", _C_ERR)
        except KeyboardInterrupt:
            _print("[sys]", "用户中断", _C_SYS)

    # ------------------------------------------------------------------
    # 协议 I/O
    # ------------------------------------------------------------------

    async def _send(self, msg: dict[str, Any]) -> None:
        assert self._ws is not None
        await self._ws.send(json.dumps(msg, ensure_ascii=False))

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        async for raw in self._ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                _print("[err]", f"无法解析: {raw!r}", _C_ERR)
                continue
            self._render(msg)

    def _render(self, msg: dict[str, Any]) -> None:
        t = msg.get("type")
        rid = msg.get("request_id", "")

        if t == "ack":
            action = msg.get("action") or msg.get("ps_id") or ""
            _print("[ack]", f"{action} request_id={rid}", _C_SYS)
            return

        if t == "reply":
            wid = msg.get("widget_id", "")
            content = msg.get("content", "")
            cancelled = msg.get("cancelled")
            tag = f"[{wid}]" + (" (cancelled)" if cancelled else "")
            _print(tag, content, _C_BOT)
            self._pending.pop(rid, None)
            return

        if t == "error":
            code = msg.get("code", "?")
            content = msg.get("message") or msg.get("content") or ""
            _print(f"[error:{code}]", content, _C_ERR)
            self._pending.pop(rid, None)
            return

        if t == "event":
            _print("[event]", json.dumps(msg, ensure_ascii=False), _C_SYS)
            return

        _print("[?]", json.dumps(msg, ensure_ascii=False), _C_SYS)

    # ------------------------------------------------------------------
    # 输入循环（用 run_in_executor 把阻塞 input 推到线程，保持事件循环响应）
    # ------------------------------------------------------------------

    async def _input_loop(self) -> None:
        loop = asyncio.get_running_loop()
        sys.stdout.write(
            f"{_C_SYS}输入消息回车发送；/quit 退出；/help 查看帮助。{_C_RST}\n"
        )
        sys.stdout.flush()
        while not self._stop.is_set():
            try:
                line = await loop.run_in_executor(None, sys.stdin.readline)
            except (EOFError, KeyboardInterrupt):
                break
            if not line:  # EOF
                break
            text = line.rstrip("\n").strip()
            if not text:
                continue

            if text in ("/quit", "/exit", "/q"):
                _print("[sys]", "退出。", _C_SYS)
                break
            if text == "/help":
                _print("[sys]", "/quit 退出  |  /info 显示连接信息  |  其它内容直接发送", _C_SYS)
                continue
            if text == "/info":
                _print(
                    "[sys]",
                    f"server={self.server_url} ps={self.ps_id} widget={self.widget_id} user={self.user_id}",
                    _C_SYS,
                )
                continue

            rid = _new_rid()
            self._pending[rid] = text
            try:
                await self._send({
                    "type": "chat",
                    "request_id": rid,
                    "ps_id": self.ps_id,
                    "widget_id": self.widget_id,
                    "content": text,
                    "user_id": self.user_id,
                })
            except websockets.exceptions.ConnectionClosed:
                _print("[err]", "WebSocket 已断开，无法发送", _C_ERR)
                break


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="PyClaego TUI v2 (PersonalSpace 协议)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-p", "--ps-id", default="default", help="PersonalSpace ID (默认: default)")
    p.add_argument(
        "-w", "--widget-id",
        default="w_chat_default",
        help="Widget ID (默认: w_chat_default)",
    )
    p.add_argument("-u", "--user-id", default="default_user", help="用户 ID")
    p.add_argument(
        "--server",
        default=None,
        help="WebSocket 服务器地址（默认: 从 config 读 client.server_url，缺省 ws://127.0.0.1:8765）",
    )
    return p.parse_args()


def _resolve_server_url(cli_value: str | None) -> str:
    if cli_value:
        return cli_value
    try:
        from pyclaego.config import get_config
        cfg = get_config()
        return cfg.get_client_config().get("server_url", "ws://127.0.0.1:8765")
    except Exception:
        return "ws://127.0.0.1:8765"


async def _amain() -> None:
    args = parse_args()
    server_url = _resolve_server_url(args.server)

    print("\n" + "=" * 60)
    print("  PyClaego TUI v2 (PersonalSpace 协议)")
    print("=" * 60)
    print(f"  Server   : {server_url}")
    print(f"  PS ID    : {args.ps_id}")
    print(f"  Widget   : {args.widget_id}")
    print(f"  User ID  : {args.user_id}")
    print("=" * 60 + "\n")

    tui = PSChatTUI(
        server_url=server_url,
        ps_id=args.ps_id,
        widget_id=args.widget_id,
        user_id=args.user_id,
    )
    await tui.run()


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
