"""PyClaw-CC Core 服务器启动脚本"""

import asyncio
import uvicorn
from pyclaego.core.scheduler import CoreScheduler
from pyclaego.config import get_config
from pyclaego.logging import get_running_log

_rlog = get_running_log()


async def _run_web_server(web_config: dict) -> None:
    """在当前事件循环中启动 uvicorn Web 服务器"""
    from pyclaego.web.app import app
    host = web_config.get("host", "0.0.0.0")
    port = web_config.get("port", 8000)
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    _rlog.info("core_service", f"[core_server.py] Web API 服务器启动于 http://{host}:{port}")
    print(f"[core_server.py] Web API Server started at http://{host}:{port}")
    await server.serve()


async def main():
    """启动 Core 服务器"""
    # 加载配置
    config = get_config()
    server_config = config.get_server_config()
    session_config = config.get("session", {})
    
    # 创建调度器（使用配置文件中的参数）
    scheduler = CoreScheduler(
        host=server_config.get("host", "127.0.0.1"),
        port=server_config.get("port", 8765),
        workspace_root=session_config.get("workspace_root", "./workspaces")
    )
    
    enable_web_server = server_config.get("enable_web_server", False)

    try:
        if enable_web_server:
            web_config = config.get("web", {})
            await asyncio.gather(
                scheduler.start(),
                _run_web_server(web_config),
                return_exceptions=False
            )
        else:
            await scheduler.start()
    except KeyboardInterrupt:
        _rlog.info("core_service", "[core_server.py] 用户中断...")
        await scheduler.stop()
        _rlog.info("core_service", "[core_server.py] 服务器已退出")


if __name__ == "__main__":
    asyncio.run(main())


def run() -> None:
    """Console script entry point (installed via [project.scripts])."""
    asyncio.run(main())
