"""PyClaego TUI 客户端启动脚本"""
import asyncio

from pyclaego.cli.tui import main

if __name__ == "__main__":
    asyncio.run(main())

