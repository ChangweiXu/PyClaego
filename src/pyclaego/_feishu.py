"""PyClaw-CC 飞书消息网关启动脚本

使用方式：
    # 1. 先启动 Core 服务器
    python pyclaego/core_server.py

    # 2. 再启动飞书网关
    python pyclaego/feishu_gateway.py

    # 3. 带参数启动（覆盖配置文件中的值）
    FEISHU_APP_ID=xxx FEISHU_APP_SECRET=yyy python pyclaego/feishu_gateway.py

配置说明：
    在 config.yaml（或 config.example.yaml）中配置 feishu: 节：
      feishu:
        app_id: ${FEISHU_APP_ID:}
        app_secret: ${FEISHU_APP_SECRET:}
        encrypt_key: ${FEISHU_ENCRYPT_KEY:}
        verification_token: ${FEISHU_VERIFICATION_TOKEN:}
        bot_user_id: ""
        dedupe_cache_size: 1000
        default_reply_type: "text"
"""

import asyncio
import sys

from pyclaego.message.feishu_client import FeishuClient
from pyclaego.message.feishu_gateway import FeishuGateway
from pyclaego.config import get_config
from pyclaego.logging import get_running_log

_rlog = get_running_log()


async def main() -> None:
    """启动飞书消息网关"""
    print("\n" + "=" * 60)
    print("  PyClaw-CC 飞书消息网关")
    print("=" * 60 + "\n")

    # 加载配置
    config = get_config()
    feishu_cfg: dict = config.get("feishu", {})
    client_cfg: dict = config.get("client", {})
    server_cfg: dict = config.get("server", {})

    # 校验飞书配置
    app_id = feishu_cfg.get("app_id", "")
    app_secret = feishu_cfg.get("app_secret", "")

    if not app_id or not app_secret:
        print("[ERROR] 未配置飞书 app_id 或 app_secret")
        print("  请在 config.yaml 中配置 feishu.app_id 和 feishu.app_secret")
        print("  或通过环境变量 FEISHU_APP_ID / FEISHU_APP_SECRET 设置\n")
        sys.exit(1)

    # 构建 CoreScheduler 连接地址
    server_url = client_cfg.get(
        "server_url",
        f"ws://{server_cfg.get('host', '127.0.0.1')}:{server_cfg.get('port', 8765)}"
    )

    print(f"CoreScheduler 地址: {server_url}")
    print(f"飞书 App ID:         {app_id[:8]}...(已隐藏)")
    print(f"回复类型:            {feishu_cfg.get('default_reply_type', 'text')}\n")

    # 创建飞书 HTTP 客户端
    feishu_client = FeishuClient(app_id=app_id, app_secret=app_secret)

    # 创建并启动网关
    gateway = FeishuGateway(
        server_url=server_url,
        feishu_client=feishu_client,
        feishu_config=feishu_cfg,
        user_id="feishu_bot",
    )

    try:
        await gateway.start()
    except KeyboardInterrupt:
        print("\n\n用户中断，正在关闭网关...")
    except Exception as e:
        _rlog.error("feishu", f"[feishu_gateway.py] 网关异常退出: {e}")
        print(f"\n网关异常退出: {e}")
        raise
    finally:
        print("\n飞书消息网关已退出。\n")


if __name__ == "__main__":
    asyncio.run(main())


def run() -> None:
    """Console script entry point (installed via [project.scripts])."""
    asyncio.run(main())
