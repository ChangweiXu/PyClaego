"""HTTP 任务查询 API"""

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from ..logging import get_running_log
from ..task_manager import TaskArtifactStore, TaskManager

_rlog = get_running_log()
router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.get("/")
async def get_all_tasks() -> dict[str, Any]:
    """获取所有任务树
    
    Returns:
        {
          "sessions": {
            "session_a": [...],
            "session_b": [...]
          },
          "total_sessions": 2
        }
    """
    _rlog.info("web_api", "[TaskAPI] GET /api/tasks/ - 获取所有任务树")
    
    task_manager = TaskManager.get_instance()
    all_tasks = task_manager.export_all_tasks()
    
    return {
        "sessions": all_tasks,
        "total_sessions": len(all_tasks)
    }


@router.get("/snapshot")
async def get_tasks_snapshot() -> dict[str, Any]:
    """获取所有任务树（规范化格式）
    
    Returns:
        {
          "sessions": {
            "session_a": { "session_id": ..., "task_count": ..., "root_tasks": [...] },
            ...
          },
          "total_sessions": 2,
          "total_tasks": 10
        }
    """
    _rlog.info("web_api", "[TaskAPI] GET /api/tasks/snapshot - 获取规范化任务树")
    
    task_manager = TaskManager.get_instance()
    all_tasks = task_manager.export_all_tasks(include_completed=True)
    
    return {
        "sessions": all_tasks["sessions"],
        "total_sessions": all_tasks["total_sessions"],
        "total_tasks": all_tasks["total_tasks"],
    }


@router.get("/sessions")
async def get_active_sessions() -> dict[str, Any]:
    """获取所有活跃 Session 列表
    
    Returns:
        {
          "sessions": ["session_a", "session_b"],
          "total": 2
        }
    """
    _rlog.info("web_api", "[TaskAPI] GET /api/tasks/sessions - 获取活跃 Session 列表")
    
    task_manager = TaskManager.get_instance()
    sessions = task_manager.get_active_sessions()
    
    return {
        "sessions": sessions,
        "total": len(sessions)
    }


@router.get("/sessions/{session_id}")
async def get_session_tasks(session_id: str) -> dict[str, Any]:
    """获取指定 Session 的任务树
    
    Args:
        session_id: Session ID
        
    Returns:
        {
          "session_id": "session_a",
          "tasks": [...],
          "total_tasks": 5
        }
        
    Raises:
        HTTPException: 404 - Session 不存在
    """
    _rlog.info("web_api", f"[TaskAPI] GET /api/tasks/sessions/{session_id} - 获取 Session 任务树")
    
    task_manager = TaskManager.get_instance()
    tasks = task_manager.export_session_tasks(session_id)
    
    if tasks is None:
        _rlog.warning("web_api", f"[TaskAPI] Session 不存在: {session_id}")
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    
    # 计算任务总数（包括子任务）
    def count_tasks(task_list: list[dict]) -> int:
        count = len(task_list)
        for task in task_list:
            if "children" in task:
                count += count_tasks(task["children"])
        return count
    
    total = count_tasks(tasks)
    
    return {
        "session_id": session_id,
        "tasks": tasks,
        "total_tasks": total
    }


@router.get("/agent-tree/{ps_id}/{widget_id}")
async def get_agent_tree(ps_id: str, widget_id: str) -> dict[str, Any]:
    """获取指定 Widget 的 Agent 树（用于前端 AgentTreePanel）。

    后端从 TaskManager 的任务树中递归提取主 Agent 和子 Agent 的层级结构，
    前端无需自行遍历 TaskNode 拼接。

    Args:
        ps_id: PersonalSpace ID
        widget_id: Widget ID

    Returns:
        {
          "ps_id": "...",
          "widget_id": "...",
          "agents": [
            {
              "id": "req_abc",
              "label": "Main Agent",
              "request_id": "req_abc",
              "subagent_id": null,
              "task_type": "user_message",
              "children": [
                {
                  "id": "req_abc:sub_xyz",
                  "label": "code_reviewer · xyz12345",
                  "request_id": "req_abc",
                  "subagent_id": "sub_xyz",
                  "task_type": "subagent_spawn",
                  "children": []
                }
              ]
            }
          ]
        }
    """
    session_id = f"{ps_id}__{widget_id}"
    _rlog.info("web_api", f"[TaskAPI] GET /api/tasks/agent-tree/{ps_id}/{widget_id}")

    task_manager = TaskManager.get_instance()
    agents = task_manager.build_agent_tree(session_id)

    return {
        "ps_id": ps_id,
        "widget_id": widget_id,
        "agents": agents,
    }


@router.get("/tasks/{task_id}")
async def get_task_detail(task_id: str) -> dict[str, Any]:
    """获取指定任务的详细信息
    
    Args:
        task_id: Task ID
        
    Returns:
        {
          "task": {
            "task_id": "...",
            "name": "...",
            "status": "...",
            ...
          }
        }
        
    Raises:
        HTTPException: 404 - 任务不存在
    """
    _rlog.info("web_api", f"[TaskAPI] GET /api/tasks/tasks/{task_id} - 获取任务详情")
    
    task_manager = TaskManager.get_instance()
    task = task_manager.get_task(task_id)
    
    if task is None:
        _rlog.warning("web_api", f"[TaskAPI] 任务不存在: {task_id}")
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    
    return {
        "task": task.to_dict()
    }


@router.get("/stats")
async def get_task_stats() -> dict[str, Any]:
    """获取任务统计信息
    
    Returns:
        {
          "total_sessions": 2,
          "total_tasks": 10,
          "by_status": {
            "pending": 1,
            "running": 3,
            "completed": 5,
            "failed": 1,
            "cancelled": 0
          }
        }
    """
    _rlog.info("web_api", "[TaskAPI] GET /api/tasks/stats - 获取任务统计")
    
    task_manager = TaskManager.get_instance()
    all_tasks = task_manager.export_all_tasks()
    
    # 统计信息
    total_sessions = len(all_tasks)
    total_tasks = 0
    by_status = {
        "pending": 0,
        "running": 0,
        "completed": 0,
        "failed": 0,
        "cancelled": 0
    }
    
    def count_recursive(task_list: list[dict]) -> None:
        nonlocal total_tasks
        for task in task_list:
            total_tasks += 1
            status = task.get("status", "unknown")
            if status in by_status:
                by_status[status] += 1
            
            # 递归统计子任务
            if "children" in task:
                count_recursive(task["children"])
    
    # 遍历所有 Session 的任务
    for session_tasks in all_tasks.values():
        count_recursive(session_tasks)
    
    return {
        "total_sessions": total_sessions,
        "total_tasks": total_tasks,
        "by_status": by_status
    }


# ============================================================
# Artifact endpoints (Phase B)
# ============================================================

@router.get("/tasks/{task_id}/artifacts")
async def list_task_artifacts(task_id: str) -> dict[str, Any]:
    """列出某 task 关联的所有 artifact 元数据。

    Returns:
        {
          "task_id": "...",
          "artifacts": [
            {
              "artifact_id": "abc123",
              "kind": "tool_args",
              "name": "args[read_file]",
              "mime": "application/json",
              "size": 87,
              "ext": "json",
              "created_at": 1714000000.123,
              "extra": {...}
            },
            ...
          ],
          "digest": {"by_kind": {...}, "total_size": 12345}
        }
    """
    store = TaskArtifactStore.get_instance()
    artifacts = store.list_for_task(task_id)
    return {
        "task_id": task_id,
        "artifacts": artifacts,
        "digest": store.digest(task_id),
    }


@router.get("/artifacts/{task_id}/{artifact_id}")
async def get_task_artifact(task_id: str, artifact_id: str):
    """读取一个 artifact 的原始 payload (按其 mime 返回)。"""
    store = TaskArtifactStore.get_instance()
    blob = store.fetch(task_id, artifact_id)
    if blob is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    data, mime, name = blob
    headers = {"X-Artifact-Name": name}
    return Response(content=data, media_type=mime, headers=headers)
