"""PyClaego TUI 客户端启动脚本 (PersonalSpace 协议)

用法
----
    # 默认 PS / widget
    python tui_client.py

    # 指定 PS / widget / user
    python tui_client.py -p alice
    python tui_client.py -p alice -w w_chat_default -u me

    # 指定服务器
    python tui_client.py --server ws://127.0.0.1:8765

向后兼容：
    第一个位置参数会被当作 ``ps_id`` 处理（旧版本是 session_id）。
"""

from __future__ import annotations

import argparse
import asyncio

from pyclaego.config import get_config
from pyclaego.message.tui_client import TUIClient


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="PyClaego TUI 客户端 (PersonalSpace 协议)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python tui_client.py\n"
            "  python tui_client.py -p alice\n"
            "  python tui_client.py -p alice -w w_chat_default -u me\n"
            "  python tui_client.py --server ws://127.0.0.1:8765\n"
        ),
    )
    parser.add_argument(
        "-p", "--ps-id",
        default="default",
        help="PersonalSpace ID (默认: default)",
    )
    parser.add_argument(
        "-w", "--widget-id",
        default="w_chat_default",
        help="Widget ID (默认: w_chat_default)",
    )
    parser.add_argument(
        "-u", "--user-id",
        default="default_user",
        help="用户 ID (默认: default_user)",
    )
    parser.add_argument(
        "--server",
        default=None,
        help="WebSocket 服务器地址 (默认: 从 config.yaml 读 client.server_url)",
    )
    # 兼容旧调用：python tui_client.py <ps_id_or_session_id>
    parser.add_argument(
        "legacy_ps_id",
        nargs="?",
        default=None,
        help=argparse.SUPPRESS,
    )

    args = parser.parse_args()
    if args.legacy_ps_id and args.ps_id == "default":
        args.ps_id = args.legacy_ps_id
    return args


def _resolve_server_url(cli_value: str | None) -> str:
    if cli_value:
        return cli_value
    try:
        cfg = get_config()
        return cfg.get_client_config().get("server_url", "ws://127.0.0.1:8765")
    except Exception:
        return "ws://127.0.0.1:8765"


async def _async_main() -> None:
    args = parse_args()
    server_url = _resolve_server_url(args.server)

    print("\n" + "=" * 60)
    print("  PyClaego TUI 客户端 (PersonalSpace 协议)")
    print("=" * 60)
    print(f"  Server  : {server_url}")
    print(f"  PS ID   : {args.ps_id}")
    print(f"  Widget  : {args.widget_id}")
    print(f"  User ID : {args.user_id}")
    print("=" * 60 + "\n")

    client = TUIClient(
        server_url=server_url,
        ps_id=args.ps_id,
        widget_id=args.widget_id,
        user_id=args.user_id,
    )
    try:
        await client.start()
    except KeyboardInterrupt:
        print("\n\n用户中断…")
    finally:
        print("\nTUI 客户端已退出。\n")


def main() -> None:
    """Console script entry point."""
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
