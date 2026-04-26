"""FastAPI 应用实例"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from pathlib import Path
from typing import Optional

app = FastAPI(
    title="PyClaw-CC Web API",
    description="PyClaw-CC Agent 管理系统 Web 接口",
    version="1.0.0"
)

# CORS 配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境需要限制
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 获取当前模块目录
CURRENT_DIR = Path(__file__).parent
STATIC_DIR = CURRENT_DIR / "static"

# 挂载静态文件目录
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/", response_class=HTMLResponse)
async def root():
    """主页 - WebUI 界面"""
    html_file = STATIC_DIR / "index.html"
    if html_file.exists():
        return html_file.read_text(encoding="utf-8")
    else:
        return """
        <html>
            <head>
                <title>PyClaw-CC Web Interface</title>
            </head>
            <body>
                <h1>PyClaw-CC Web Interface</h1>
                <p style="color: red;">Error: index.html not found</p>
                <p>Please check if the static files are properly installed.</p>
            </body>
        </html>
        """

@app.get("/tasks_v1", response_class=HTMLResponse)
async def tasks_page():
    """任务管理器页面"""
    html_file = STATIC_DIR / "tasks.html"
    if html_file.exists():
        return html_file.read_text(encoding="utf-8")
    else:
        return """
        <html>
            <head>
                <title>PyClaw-CC Tasks</title>
            </head>
            <body>
                <h1>PyClaw-CC 任务管理器</h1>
                <p style="color: red;">Error: tasks.html not found</p>
                <p>Please check if the static files are properly installed.</p>
            </body>
        </html>
        """

@app.get("/tasks", response_class=HTMLResponse)
async def tasks2_page():
    """任务详情仪表盘页面"""
    html_file = STATIC_DIR / "tasks2.html"
    if html_file.exists():
        return html_file.read_text(encoding="utf-8")
    else:
        return """
        <html>
            <head>
                <title>PyClaw-CC Task Dashboard</title>
            </head>
            <body>
                <h1>PyClaw-CC 任务详情仪表盘</h1>
                <p style="color: red;">Error: tasks2.html not found</p>
                <p>Please check if the static files are properly installed.</p>
            </body>
        </html>
        """

@app.get("/tasks3", response_class=HTMLResponse)
async def tasks3_page():
    """任务图谱仪表盘 V3 页面"""
    html_file = STATIC_DIR / "tasks3.html"
    if html_file.exists():
        return html_file.read_text(encoding="utf-8")
    else:
        return """
        <html>
            <head><title>PyClaw-CC Task Dashboard V3</title></head>
            <body>
                <h1>PyClaw-CC 任务图谱仪表盘</h1>
                <p style="color: red;">Error: tasks3.html not found</p>
            </body>
        </html>
        """

@app.get("/api/info")
async def api_info():
    """API 信息接口（JSON）"""
    return {
        "service": "PyClaw-CC Web API",
        "version": "1.0.0",
        "endpoints": {
            "web_ui": "/ (HTTP)",
            "tasks": "/tasks (HTTP)",
            "task_dashboard": "/task2 (HTTP)",
            "chat": "/chat/{session_id} (WebSocket)",
            "tasks_ws": "/ws/tasks (WebSocket)",
            "sessions": "/api/sessions (HTTP)",
            "task_api": "/api/tasks/* (HTTP)",
            "health": "/health (HTTP)",
            "api_info": "/api/info (HTTP)"
        }
    }

@app.get("/api/sessions")
async def list_sessions(user_id: Optional[str] = None):
    """获取 Session 列表
    
    注意：此接口返回 workspace 目录中所有 Session。
    前端可以基于 LocalStorage 过滤出最近使用的 Session。
    
    Args:
        user_id: 可选，按用户ID过滤
        
    Returns:
        Session 列表，包含 session_id, workspace_path 等信息
    """
    from ..config import get_config
    from pathlib import Path
    import json as json_module
    
    # 读取配置
    config = get_config()
    session_config = config.get("session", {})
    workspace_root = session_config.get("workspace_root", "./workspaces")
    
    # 获取所有已存在的 workspace 目录
    workspace_path = Path(workspace_root).expanduser()
    sessions = []
    
    if workspace_path.exists():
        for session_dir in workspace_path.iterdir():
            if session_dir.is_dir() and not session_dir.name.startswith('.'):
                session_id = session_dir.name
                metadata_file = session_dir / "metadata.json"
                
                session_info = {
                    "session_id": session_id,
                    "workspace_path": str(session_dir),
                    "created_at": None,
                    "user_id": None
                }
                
                # 尝试读取 metadata.json
                if metadata_file.exists():
                    try:
                        with open(metadata_file, 'r', encoding='utf-8') as f:
                            metadata = json_module.load(f)
                            session_info["created_at"] = metadata.get("created_at")
                            session_info["user_id"] = metadata.get("user_id")
                    except Exception:
                        pass
                
                # 用户过滤
                if user_id is None or session_info["user_id"] == user_id:
                    sessions.append(session_info)
    
    # 按创建时间倒序排列（最新的在前）
    sessions.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    
    return {
        "sessions": sessions,
        "total": len(sessions)
    }

@app.get("/health")
async def health():
    """健康检查接口"""
    return {"status": "healthy"}

# 导入并注册路由
from .websocket import router as websocket_router
from .task_websocket import router as task_websocket_router, get_bridge_client
from .task_api import router as task_api_router

app.include_router(websocket_router)
app.include_router(task_websocket_router)
app.include_router(task_api_router)

# 启动时自动启动桥接客户端
@app.on_event("startup")
async def startup_event():
    """应用启动时的初始化"""
    from ..logging import get_running_log
    _rlog = get_running_log()
    
    # 启动任务桥接客户端
    bridge_client = get_bridge_client()
    await bridge_client.start()
    _rlog.info("web_api", "[App] 任务桥接客户端已启动")

@app.on_event("shutdown")
async def shutdown_event():
    """应用关闭时的清理"""
    from ..logging import get_running_log
    _rlog = get_running_log()
    
    # 停止任务桥接客户端
    bridge_client = get_bridge_client()
    await bridge_client.stop()
    _rlog.info("web_api", "[App] 任务桥接客户端已停止")
