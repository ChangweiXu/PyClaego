"""PyClaego 飞书消息网关启动脚本"""
import asyncio

from pyclaego.cli.feishu import main

if __name__ == "__main__":
    asyncio.run(main())

