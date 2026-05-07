"""PyClaego Core 服务器启动脚本"""
import asyncio

from pyclaego.cli.core import main

if __name__ == "__main__":
    asyncio.run(main())
