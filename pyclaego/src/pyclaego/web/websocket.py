"""WebSocket 路由实现"""

import asyncio
import json
from typing import Any

import websockets
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..config import get_config
from ..logging import get_running_log
from ..utility import validate_session_id

_rlog = get_running_log()
router = APIRouter()

# 从配置读取 CoreScheduler 地址
config = get_config()
CORE_SCHEDULER_HOST = config.get("server", {}).get("host", "127.0.0.1")
CORE_SCHEDULER_PORT = config.get("server", {}).get("port", 18765)
CORE_SCHEDULER_URL = f"ws://{CORE_SCHEDULER_HOST}:{CORE_SCHEDULER_PORT}"

_rlog.info("web_api", f"[WebSocket Router] CoreScheduler URL: {CORE_SCHEDULER_URL}")


@router.websocket("/chat/{session_id}")
async def websocket_chat_endpoint(websocket: WebSocket, session_id: str):
    """WebSocket 聊天接口
    
    Args:
        websocket: WebSocket 连接
        session_id: Session ID
    """
    # 验证 session_id 格式
    if not validate_session_id(session_id):
        await websocket.close(
            code=1003,
            reason=f"Invalid session_id format: '{session_id}'"
        )
        _rlog.warning("web_api", f"[WebSocket] 非法 session_id: {session_id}")
        return
    
    # 接受 WebSocket 连接
    await websocket.accept()
    _rlog.info("web_api", f"[WebSocket] 客户端连接到 session: {session_id}")
    
    core_ws = None
    
    try:
        # 连接到 CoreScheduler（max_size=20MB，支持图片等大消息）
        core_ws = await websockets.connect(CORE_SCHEDULER_URL, max_size=20 * 1024 * 1024)
        _rlog.info("web_api", f"[WebSocket] 已连接到 CoreScheduler: {CORE_SCHEDULER_URL}")
        
        # 发送 join_session 消息
        join_msg = {
            "type": "join_session",
            "session_id": session_id,
            "user_id": "web_user"
        }
        await core_ws.send(json.dumps(join_msg))
        
        # 等待 session_joined 响应
        join_response = await core_ws.recv()
        join_data = json.loads(join_response)
        
        if join_data.get("type") == "error":
            await websocket.send_text(json.dumps(join_data))
            _rlog.error("web_api", f"[WebSocket] 加入 session 失败: {join_data.get('content')}")
            return
        
        # 发送 session_joined 确认给客户端
        await websocket.send_text(join_response)
        _rlog.info("web_api", f"[WebSocket] 已加入 session: {session_id}")
        
        # 创建双向代理任务
        client_to_core = asyncio.create_task(
            _forward_client_to_core(websocket, core_ws, session_id)
        )
        core_to_client = asyncio.create_task(
            _forward_core_to_client(core_ws, websocket, session_id)
        )
        
        # 等待任一任务完成(连接断开)
        done, pending = await asyncio.wait(
            [client_to_core, core_to_client],
            return_when=asyncio.FIRST_COMPLETED
        )
        
        # 取消未完成的任务
        for task in pending:
            task.cancel()
        
    except websockets.exceptions.ConnectionClosed:
        _rlog.info("web_api", f"[WebSocket] CoreScheduler 连接断开: {session_id}")
    except Exception as e:
        _rlog.error("web_api", f"[WebSocket] 错误: {e}")
    finally:
        if core_ws:
            await core_ws.close()
        await websocket.close()
        _rlog.info("web_api", f"[WebSocket] 连接已关闭: {session_id}")


async def _forward_client_to_core(
    client_ws: WebSocket,
    core_ws: Any,
    session_id: str
):
    """转发客户端消息到 CoreScheduler
    
    Args:
        client_ws: 客户端 WebSocket 连接
        core_ws: CoreScheduler WebSocket 连接
        session_id: Session ID
    """
    try:
        async for message in client_ws.iter_text():
            await core_ws.send(message)
            _rlog.debug("web_api", f"[Forward] 客户端→Core: session={session_id}, len={len(message)}")
    except WebSocketDisconnect:
        _rlog.info("web_api", f"[Forward] 客户端断开: {session_id}")
    except Exception as e:
        _rlog.error("web_api", f"[Forward] 客户端→Core 错误: {e}")


async def _forward_core_to_client(
    core_ws: Any,
    client_ws: WebSocket,
    session_id: str
):
    """转发 CoreScheduler 消息到客户端
    
    Args:
        core_ws: CoreScheduler WebSocket 连接
        client_ws: 客户端 WebSocket 连接
        session_id: Session ID
    """
    try:
        async for message in core_ws:
            await client_ws.send_text(message)
            _rlog.debug("web_api", f"[Forward] Core→客户端: session={session_id}, len={len(message)}")
    except websockets.exceptions.ConnectionClosed:
        _rlog.info("web_api", f"[Forward] CoreScheduler 断开: {session_id}")
    except Exception as e:
        _rlog.error("web_api", f"[Forward] Core→客户端 错误: {e}")
