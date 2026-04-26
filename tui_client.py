"""PyClaw-CC TUI 客户端启动脚本"""

import asyncio
import argparse
from pyclaego.message.tui_client import TUIClient
from pyclaego.config import get_config


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description='PyClaw-CC TUI 客户端',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
示例:
  # 创建新 Session（自动生成 session_id）
  python tui_client.py
  
  # 使用指定的 User ID 创建新 Session
  python tui_client.py --user-id alice
  
  # 加入已有的 Session
  python tui_client.py --session-id sess_abc123xyz
  
  # 指定 User ID 加入已有的 Session
  python tui_client.py --user-id alice --session-id sess_abc123xyz
        '''
    )
    
    parser.add_argument(
        '-u', '--user-id',
        type=str,
        default='default_user',
        help='用户 ID (默认: default_user)'
    )
    
    parser.add_argument(
        '-s', '--session-id',
        type=str,
        default=None,
        help='Session ID (不指定则创建新 Session)'
    )
    
    # 兼容旧的位置参数形式：python tui_client.py <session_id>
    parser.add_argument(
        'legacy_session_id',
        nargs='?',
        default=None,
        help=argparse.SUPPRESS  # 隐藏此参数，仅用于向后兼容
    )
    
    args = parser.parse_args()
    
    # 如果使用了旧的位置参数，优先使用它
    if args.legacy_session_id and not args.session_id:
        args.session_id = args.legacy_session_id
    
    return args


async def main():
    """启动 TUI 客户端"""
    print("\n" + "="*60)
    print("  PyClaw-CC TUI 客户端 (Session Mode)")
    print("="*60 + "\n")
    
    # 解析命令行参数
    args = parse_args()
    
    # 显示当前配置
    print(f"User ID: {args.user_id}")
    if args.session_id:
        print(f"Session ID: {args.session_id} (加入已有 Session)")
    else:
        print(f"Session ID: (将创建新 Session)")
    print()
    
    # 加载配置
    config = get_config()
    client_config = config.get_client_config()
    
    # 创建客户端（使用配置文件中的服务器地址）
    server_url = client_config.get("server_url", "ws://127.0.0.1:8765")
    client = TUIClient(
        server_url=server_url,
        session_id=args.session_id,
        user_id=args.user_id
    )
    
    try:
        await client.start()
    except KeyboardInterrupt:
        print("\n\n用户中断...")
    finally:
        print("\nTUI 客户端已退出。\n")


if __name__ == "__main__":
    asyncio.run(main())


def run() -> None:
    """Console script entry point (installed via [project.scripts])."""
    asyncio.run(main())
