"""Web 服务器启动脚本"""

import argparse
import uvicorn
from pyclaego.web.app import app
from pyclaego.config import get_config


def main():
    # 解析命令行参数
    parser = argparse.ArgumentParser(description="PyClaw-CC Web API Server")
    parser.add_argument(
        "--port", "-p",
        type=int,
        help="Web 服务器端口号 (默认从配置文件读取 web.port，配置缺失时默认 8000)"
    )
    parser.add_argument(
        "--host",
        type=str,
        help="Web 服务器监听地址 (默认从配置文件读取 web.host，配置缺失时默认 0.0.0.0)"
    )
    args = parser.parse_args()
    
    # 读取配置
    config = get_config()
    web_config = config.get("web", {})
    
    # 优先使用命令行参数，其次使用配置文件，最后使用默认值
    host = args.host or web_config.get("host", "0.0.0.0")
    port = args.port or web_config.get("port", 8000)
    
    print("=" * 60)
    print("PyClaw-CC Web API Server")
    print("=" * 60)
    print(f"Web API: http://{host}:{port}")
    print(f"WebSocket: ws://{host}:{port}/chat/{{session_id}}")
    print(f"API Docs: http://{host}:{port}/docs")
    print("=" * 60)
    
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info"
    )


if __name__ == "__main__":
    main()
