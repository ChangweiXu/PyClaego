"""核心调度器 - WebSocket 服务器，包装 PSGateway。

协议 v2（与 src/core/ps_gateway.py 一致）：

入站
----
    {type:"open"|"close"|"chat"|"control",
     request_id, ps_id, widget_id?, content?, user_id?, ...}

出站
----
    {type:"ack"|"reply"|"error"|"event",
     request_id, ps_id?, widget_id?, content?, ...}

每个 WebSocket 连接获得唯一 ``conn_id``（``id(websocket)``）。
PSGateway 通过 ``publish_fn(conn_id, msg)`` 向特定连接发送消息。
"""

from __future__ import annotations

import asyncio
import json
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

import websockets

from ..config import get_config
from ..logging import get_running_log
from ..message.feishu_gateway import FeishuGateway
from ..personal_space import PersonalSpaceManager
from ..task_manager import TaskManager, TextSubscriber
from .ps_gateway import PSGateway

_rlog = get_running_log()


class CoreScheduler:
    """中心化调度器：单一职责 = 把 WS 字节流接入 PSGateway。

    与旧版（Session 模式）相比：
      * 不再持有 ``SessionManager`` / ``client_sessions`` 等映射；
      * 连接级别状态（``conn_id -> websockets``）只用于 publish；
      * 业务路由全部委托给 :class:`PSGateway`。
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        root_path: str | None = None,
        max_active: int | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.root_path = root_path
        self.max_active = max_active

        self.running = False
        # conn_id -> websocket
        self._conns: dict[int, websockets.WebSocketServerProtocol] = {}

        self.ps_manager: PersonalSpaceManager | None = None
        self.gateway: PSGateway | None = None
        self.feishu_gateway: FeishuGateway | None = None
        self.cron_scheduler: Any | None = None  # WidgetCronScheduler
        # self.llm_router_server: Any | None = None  # uvicorn.Server

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self.running = True

        # 初始化 PSManager + Gateway（懒到此处避免 import 期副作用）
        ps_kwargs: dict[str, Any] = {}
        if self.root_path is not None:
            ps_kwargs["root_path"] = Path(self.root_path)
        if self.max_active is not None:
            ps_kwargs["max_active"] = self.max_active

        self.ps_manager = PersonalSpaceManager.instance(**ps_kwargs)

        # ── 初始化 ToolAgentManager（加载 builtin + global 层） ────────────
        try:
            from ..tool_agent import get_tool_agent_manager
            tam = get_tool_agent_manager()
            tam.load_builtins()
            tam.load_globals()
            tam.register_all_to_subagent_profiles()
            _rlog.info("core_service", "[CoreScheduler] ToolAgentManager 初始化完成")
        except Exception as exc:
            _rlog.warning("core_service", f"[CoreScheduler] ToolAgentManager 初始化失败: {exc}")

        self.gateway = PSGateway(self.ps_manager, self._publish)

        # FeishuGateway (in-process) — only started when feishu config is present
        cfg = get_config()
        feishu_cfg = cfg.get("feishu", {})
        if feishu_cfg and feishu_cfg.get("enabled", False) and feishu_cfg.get("app_id"):
            try:
                from ..message.feishu_client import FeishuClient
                feishu_client = FeishuClient(
                    app_id=feishu_cfg["app_id"],
                    app_secret=feishu_cfg["app_secret"],
                )
                self.feishu_gateway = FeishuGateway(
                    ps_gateway=self.gateway,
                    ps_manager=self.ps_manager,
                    feishu_client=feishu_client,
                    feishu_config=feishu_cfg,
                )
                _rlog.info("core_service", "[CoreScheduler] FeishuGateway 已初始化（将在端口就绪后启动）")
            except Exception as e:
                _rlog.warning(
                    "core_service",
                    f"[CoreScheduler] FeishuGateway 启动失败（忽略）: {e}",
                )

        # Phase 9: WidgetCronScheduler — 扫描 widget.json 里的 cron[]
        # 启动失败不应拖垮服务器，仅警告。
        try:
            from ..personal_space.cron import WidgetCronScheduler
            self.cron_scheduler = WidgetCronScheduler(
                ps_root=self.ps_manager.root_path,
                handle_inbound=self.gateway.handle_inbound,
            )
            self.cron_scheduler.start()
        except Exception as e:
            _rlog.warning(
                "core_service",
                f"[CoreScheduler] WidgetCronScheduler 启动失败（忽略）: {e}\n{traceback.format_exc()}",
            )
            self.cron_scheduler = None

        # LLM Router — 内建转发代理，受 server.enable_llm_router 控制
        # try:
        #     import uvicorn as _uvicorn

        #     from ..llm_router import create_app, load_router_config
        #     _router_cfg = load_router_config()
        #     if cfg.get("server", {}).get("enable_llm_router", False):
        #         _router_app = create_app(_router_cfg)
        #         _router_uv_cfg = _uvicorn.Config(
        #             _router_app,
        #             host=_router_cfg.server.host,
        #             port=_router_cfg.server.port,
        #             log_level="warning",
        #         )
        #         self.llm_router_server = _uvicorn.Server(_router_uv_cfg)
        #         _rlog.info(
        #             "core_service",
        #             f"[CoreScheduler] LLM Router 已配置（将在端口就绪后启动）: "
        #             f"http://{_router_cfg.server.host}:{_router_cfg.server.port}",
        #         )
        #     else:
        #         _rlog.info("core_service", "[CoreScheduler] LLM Router 已禁用（server.enable_llm_router=false）")
        # except Exception as e:
        #     _rlog.warning(
        #         "core_service",
        #         f"[CoreScheduler] LLM Router 初始化失败（忽略）: {e}\n{traceback.format_exc()}",
        #     )
        #     self.llm_router_server = None

        # 注册 TextSubscriber（进程级单次注册）
        _pyclaego_root = Path(get_config().get("pyclaego", {}).get("root_path", "~/.pyclaego")).expanduser()
        TaskManager.get_instance().subscribe(
            TextSubscriber(output_dir=_pyclaego_root / ".cache" / "task_output")
        )
        # 启动 TaskBridgeServer（进程级单次启动）
        from ..web.task_bridge import TaskBridgeServer
        self.task_bridge = TaskBridgeServer(host="127.0.0.1", port=18766)
        TaskManager.get_instance().subscribe(self.task_bridge)
        await self.task_bridge.start()

        ts = self._ts()
        _rlog.info("core_service", f"[CoreScheduler] [{ts}] PSGateway 已就绪 (root={self.ps_manager.root_path})")
        _rlog.info("core_service", f"[CoreScheduler] [{ts}] WebSocket 启动于 ws://{self.host}:{self.port}")
        print(f"[CoreScheduler] Core Server (PS mode) started at ws://{self.host}:{self.port}")

        async with websockets.serve(
            self._handle_client,
            self.host,
            self.port,
            max_size=20 * 1024 * 1024,
        ):
            # 端口已绑定后再启动 FeishuGateway，避免其 lark_oapi 初始化阻塞端口绑定
            if self.feishu_gateway is not None:
                asyncio.create_task(self.feishu_gateway.start())
                _rlog.info("core_service", "[CoreScheduler] FeishuGateway 已启动（进程内模式）")
            # if self.llm_router_server is not None:
            #     asyncio.create_task(self.llm_router_server.serve())
            #     _rlog.info("core_service", "[CoreScheduler] LLM Router 已启动")
            await asyncio.Future()  # 永远运行

    async def stop(self) -> None:
        self.running = False
        try:
            if self.feishu_gateway is not None:
                await self.feishu_gateway.stop()
        except Exception as e:
            _rlog.error("core_service", f"[CoreScheduler] feishu_gateway.stop 异常: {e}")
        # try:
        #     if self.llm_router_server is not None:
        #         self.llm_router_server.should_exit = True
        # except Exception as e:
        #     _rlog.error("core_service", f"[CoreScheduler] llm_router_server.stop 异常: {e}")
        try:
            if self.cron_scheduler is not None:
                self.cron_scheduler.shutdown()
        except Exception as e:
            _rlog.error("core_service", f"[CoreScheduler] cron_scheduler.shutdown 异常: {e}")
        try:
            if self.gateway is not None:
                await self.gateway.shutdown()
        except Exception as e:
            _rlog.error("core_service", f"[CoreScheduler] gateway.shutdown 异常: {e}\n{traceback.format_exc()}")
        try:
            if self.ps_manager is not None:
                await self.ps_manager.shutdown()
        except Exception as e:
            _rlog.error("core_service", f"[CoreScheduler] ps_manager.shutdown 异常: {e}\n{traceback.format_exc()}")
        _rlog.info("core_service", f"[CoreScheduler] [{self._ts()}] 已停止")

    # ------------------------------------------------------------------
    # WebSocket 处理
    # ------------------------------------------------------------------

    async def _handle_client(self, websocket: websockets.WebSocketServerProtocol) -> None:
        conn_id = str(id(websocket))
        self._conns[int(conn_id)] = websocket
        await self.gateway.register_connection(conn_id)

        _rlog.info(
            "core_service",
            f"[CoreScheduler] [{self._ts()}] 客户端 #{conn_id} 已连接，当前连接数: {len(self._conns)}",
        )

        try:
            async for raw in websocket:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError as e:
                    await self._safe_send(websocket, {
                        "type": "error",
                        "code": "bad_json",
                        "message": f"无法解析 JSON: {e}",
                        "timestamp": datetime.now().isoformat(),
                    })
                    continue

                # gateway.handle_inbound 自身已 catch 异常
                await self.gateway.handle_inbound(conn_id, msg)

        except websockets.exceptions.ConnectionClosed:
            _rlog.info("core_service", f"[CoreScheduler] [{self._ts()}] 客户端 #{conn_id} 断开")
        except Exception as e:
            _rlog.error(
                "core_service",
                f"[CoreScheduler] [{self._ts()}] 处理连接异常 #{conn_id}: {e}\n{traceback.format_exc()}",
            )
        finally:
            try:
                await self.gateway.unregister_connection(conn_id)
            finally:
                self._conns.pop(int(conn_id), None)
                _rlog.info(
                    "core_service",
                    f"[CoreScheduler] [{self._ts()}] 客户端 #{conn_id} 清理完毕，当前连接数: {len(self._conns)}",
                )

    async def _publish(self, conn_id: str, msg: dict[str, Any]) -> None:
        """PSGateway 出站回调：按 conn_id 路由消息。"""
        if conn_id.startswith("feishu:") and self.feishu_gateway is not None:
            await self.feishu_gateway.publish(conn_id, msg)
            return
        try:
            ws = self._conns.get(int(conn_id))
        except (ValueError, TypeError):
            ws = None
        if ws is None:
            _rlog.debug(
                "core_service",
                f"[CoreScheduler] publish 时找不到 conn={conn_id}，已丢弃 type={msg.get('type')}",
            )
            return
        await self._safe_send(ws, msg)

    async def _safe_send(self, ws: websockets.WebSocketServerProtocol, msg: dict[str, Any]) -> None:
        try:
            await ws.send(json.dumps(msg, ensure_ascii=False))
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as e:
            _rlog.error(
                "core_service",
                f"[CoreScheduler] [{self._ts()}] send 失败: {e}",
            )

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    def list_connections(self) -> set[int]:
        return set(self._conns.keys())

    def _ts(self) -> str:
        return datetime.now().strftime("%H:%M:%S")


async def main() -> None:
    scheduler = CoreScheduler(host="127.0.0.1", port=8765)
    try:
        await scheduler.start()
    except KeyboardInterrupt:
        _rlog.info("core_service", "[scheduler.py] 用户中断，Core 服务器退出")
        await scheduler.stop()


if __name__ == "__main__":
    asyncio.run(main())
