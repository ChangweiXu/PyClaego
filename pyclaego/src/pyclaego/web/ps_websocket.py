"""v2 PS-aware chat WebSocket proxy.

协议（与 ``PSGateway`` 一致）：
- 客户端 → 服务端：``{type: "open"|"chat"|"close", ps_id, widget_id?, content?, request_id?, source?}``
- 服务端 → 客户端：``{type: "ack"|"reply"|"error"|"event", ...}``

Web Server 进程不直接持有 ``PSGateway``（它在 Core Server 进程里）。本路由把每个浏览器
WS 连接代理到 ``ws://CORE_HOST:CORE_PORT``，用 ``conn_id`` 串起一对一映射。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import websockets
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..config import get_config
from ..logging import get_running_log

_rlog = get_running_log()
router = APIRouter()

_cfg = get_config()
_CORE_HOST = _cfg.get("server", {}).get("host", "127.0.0.1")
_CORE_PORT = _cfg.get("server", {}).get("port", 18765)
_CORE_URL = f"ws://{_CORE_HOST}:{_CORE_PORT}"


@router.websocket("/ws/v2/chat")
async def websocket_v2_chat_endpoint(websocket: WebSocket):
    """通用 PS 协议 WS 入口。客户端自行发送 open/chat/close。"""
    await websocket.accept()
    _rlog.info("web_api", "[ws/v2/chat] 浏览器已连接")

    core_ws = None
    try:
        core_ws = await websockets.connect(
            _CORE_URL,
            max_size=20 * 1024 * 1024,
            ping_interval=30,   # send keepalive pings every 30 s
            ping_timeout=None,  # never drop on slow pong — long LLM calls need this
        )
        client_to_core = asyncio.create_task(_pipe_client_to_core(websocket, core_ws))
        core_to_client = asyncio.create_task(_pipe_core_to_client(core_ws, websocket))
        done, pending = await asyncio.wait(
            [client_to_core, core_to_client], return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
    except Exception as e:
        _rlog.error("web_api", f"[ws/v2/chat] 错误: {e}")
        try:
            await websocket.send_text(
                json.dumps({"type": "error", "content": f"core_unreachable: {e}"})
            )
        except Exception:
            pass
    finally:
        if core_ws is not None:
            try:
                await core_ws.close()
            except Exception:
                pass
        try:
            await websocket.close()
        except Exception:
            pass
        _rlog.info("web_api", "[ws/v2/chat] 连接已关闭")


async def _pipe_client_to_core(client_ws: WebSocket, core_ws: Any) -> None:
    try:
        async for message in client_ws.iter_text():
            await core_ws.send(message)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        _rlog.error("web_api", f"[ws/v2/chat] client→core 错误: {e}")


async def _pipe_core_to_client(core_ws: Any, client_ws: WebSocket) -> None:
    try:
        async for message in core_ws:
            await client_ws.send_text(message)
    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        _rlog.error("web_api", f"[ws/v2/chat] core→client 错误: {e}")
