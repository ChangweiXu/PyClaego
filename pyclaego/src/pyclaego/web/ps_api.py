"""PersonalSpace REST API — Phase 10 / 4.3.

新前端（Dashboard）和未来的 PS 管理界面通过这些端点直接访问 PSManager / WidgetClassRegistry。
所有端点位于 ``/api/v2/``，与历史的 ``/api/sessions``、``/api/tasks`` 共存互不干扰。

关键设计：
- 与 ``CoreScheduler`` **同进程内**调用 PSManager（FS 操作 + 加载/卸载）。Web Server 进程
  与 Core 进程当前是**两个进程**，因此本路由实际工作是在 Web Server 进程里维护一份
  PSManager 视图（同样指向同一份磁盘）。配置/widget CRUD 走文件，Core 进程通过
  watchfiles 自动 pick up；运行态调用（chat/highlight）走 WebSocket 桥到 Core。
- highlight 端点尝试**调用磁盘上的 hook**（在 Web 进程里临时实例化），否则回退到空字典。
  真正的实时 highlight 在未来通过 ``/ws/v2/data`` 推送。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from ..config import PYCLAEGO_DEFAULT_LOGS_ROOT, get_config
from ..logging import get_running_log
from ..personal_space import (
    PersonalSpaceManager,
    WidgetClassRegistry,
    WidgetCommand,
    WidgetManifest,
)

_rlog = get_running_log()
router = APIRouter(prefix="/api/v2", tags=["personal_space"])


def _count_jsonl_lines(filepath: Path) -> int:
    """Count non-empty lines in a JSONL file (fast approximation)."""
    try:
        return sum(1 for _ in filepath.read_text(encoding="utf-8").splitlines() if _.strip())
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# 单例工厂：确保 web 进程内只有一份 PSManager（懒构造，与 Core 进程无依赖）
# ---------------------------------------------------------------------------


def _get_psm() -> PersonalSpaceManager:
    ps_section = get_config().get("personal_space") or {}
    root = ps_section.get("root_path")
    max_active = ps_section.get("max_active")
    kwargs: dict[str, Any] = {}
    if root is not None:
        kwargs["root_path"] = Path(root).expanduser()
    if max_active is not None:
        kwargs["max_active"] = int(max_active)
    return PersonalSpaceManager.instance(**kwargs)


def _get_registry() -> WidgetClassRegistry:
    return WidgetClassRegistry.instance()


# ---------------------------------------------------------------------------
# Pydantic 模型
# ---------------------------------------------------------------------------

# widget_id: 与 ps_id 相同规则 — 不能以 _ 开头/结尾，不能含连续 __
_WIDGET_ID_PATTERN = re.compile(r"^(?!_)(?!.*__)[A-Za-z0-9._-]+(?<!_)$")


class CreateWidgetRequest(BaseModel):
    widget_id: str = Field(..., min_length=1, max_length=64)
    widget_class: str = Field(..., min_length=1)
    title: str | None = None
    config: dict[str, Any] | None = None

    @field_validator("widget_id")
    @classmethod
    def _validate_widget_id(cls, v: str) -> str:
        if not _WIDGET_ID_PATTERN.match(v):
            raise ValueError(
                "widget_id must not start or end with '_', "
                "and must not contain consecutive '__'"
            )
        return v


class UpdateWidgetConfigRequest(BaseModel):
    config: dict[str, Any]


class UpdateWidgetManifestRequest(BaseModel):
    title: str | None = None


class CreatePSRequest(BaseModel):
    title: str | None = None
    description: str | None = None


class UpdateWidgetCronRequest(BaseModel):
    cron: list[dict[str, Any]]


# PS IDs that clash with static frontend routes
_RESERVED_PS_IDS: frozenset = frozenset({"tasks", "tasks2", "ps"})


# ---------------------------------------------------------------------------
# WidgetClass registry
# ---------------------------------------------------------------------------


@router.get("/widget_classes")
def list_widget_classes() -> dict[str, Any]:
    """返回 ``WidgetClassRegistry`` 中所有已注册的 class 元数据。"""
    reg = _get_registry()
    reg.load()
    items = []
    for class_id in reg.list():
        spec = reg.get(class_id)
        items.append({
            "class_id": spec.class_id,
            "title": spec.title,
            "description": spec.description,
            "source": spec.source,
            "config_schema": spec.config_schema,
            "has_hook": spec.hook_class is not None,
            "defaults": spec.defaults,
        })
    return {"widget_classes": items, "total": len(items)}


# ---------------------------------------------------------------------------
# LLM providers
# ---------------------------------------------------------------------------


@router.get("/llm_providers")
def list_llm_providers() -> dict[str, Any]:
    """返回 llm.yaml 中配置的所有 provider 名称及默认 provider。"""
    llm_cfg = get_config().get("llm") or {}
    providers: dict[str, Any] = llm_cfg.get("providers") or {}
    default_provider: str = llm_cfg.get("default_provider") or ""
    return {
        "providers": sorted(providers.keys()),
        "default_provider": default_provider,
    }


# ---------------------------------------------------------------------------
# PersonalSpace CRUD
# ---------------------------------------------------------------------------


@router.get("/personal_spaces")
def list_personal_spaces() -> dict[str, Any]:
    psm = _get_psm()
    return {"personal_spaces": psm.list_disk_ps_ids(exclude_kinds={"feishu_chat"})}


@router.post("/personal_spaces/{ps_id}")
async def create_or_get_personal_space(
    ps_id: str, body: CreatePSRequest | None = None
) -> dict[str, Any]:
    """幂等 — 若 PS 不存在则 bootstrap on disk，再返回 manifest 概要。"""
    if ps_id in _RESERVED_PS_IDS:
        raise HTTPException(
            status_code=400,
            detail=f"PS id '{ps_id}' is reserved and cannot be used.",
        )
    psm = _get_psm()
    try:
        ps = await psm.get(ps_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # 可选：写入 title/description 到 personal_space.json
    if body is not None and (body.title or body.description):
        manifest_path = ps.ps_root / "personal_space.json"
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            data = {"ps_id": ps_id}
        if body.title is not None:
            data["title"] = body.title
        if body.description is not None:
            data["description"] = body.description
        manifest_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        ps.config.reload_file(manifest_path)
    return _ps_summary(ps_id, psm)


@router.get("/personal_spaces/{ps_id}")
async def get_personal_space(ps_id: str) -> dict[str, Any]:
    psm = _get_psm()
    if not (psm.root_path / ps_id).exists():
        raise HTTPException(status_code=404, detail=f"PS {ps_id} not found")
    return _ps_summary(ps_id, psm)


def _ps_summary(ps_id: str, psm: PersonalSpaceManager) -> dict[str, Any]:
    ps_root = psm.root_path / ps_id
    manifest_path = ps_root / "personal_space.json"
    manifest: dict[str, Any] = {"ps_id": ps_id}
    if manifest_path.exists():
        try:
            manifest.update(json.loads(manifest_path.read_text(encoding="utf-8")))
        except Exception:
            pass
    widgets_dir = ps_root / "widgets"
    widgets: list[dict[str, Any]] = []
    if widgets_dir.is_dir():
        for entry in sorted(widgets_dir.iterdir()):
            if not entry.is_dir():
                continue
            wm_path = entry / "widget.json"
            if not wm_path.exists():
                continue
            try:
                wm = json.loads(wm_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            widgets.append({
                "widget_id": wm.get("widget_id", entry.name),
                "widget_class": wm.get("widget_class"),
                "title": wm.get("title", entry.name),
            })
    return {
        "ps_id": ps_id,
        "manifest": manifest,
        "widgets": widgets,
        "loaded": psm.is_loaded(ps_id),
    }


# ---------------------------------------------------------------------------
# Widget CRUD
# ---------------------------------------------------------------------------


@router.post("/personal_spaces/{ps_id}/widgets")
async def create_widget(ps_id: str, body: CreateWidgetRequest) -> dict[str, Any]:
    psm = _get_psm()
    reg = _get_registry()
    reg.load()
    if not reg.has(body.widget_class):
        raise HTTPException(
            status_code=400, detail=f"Unknown widget_class: {body.widget_class}"
        )
    try:
        ps = await psm.get(ps_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    widget_dir = ps.ps_root / "widgets" / body.widget_id
    if widget_dir.exists():
        raise HTTPException(status_code=409, detail=f"Widget {body.widget_id} already exists")
    widget_dir.mkdir(parents=True, exist_ok=True)

    manifest = WidgetManifest(
        widget_id=body.widget_id,
        widget_class=body.widget_class,
        title=body.title or body.widget_id,
    )
    (widget_dir / "widget.json").write_text(
        json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (widget_dir / "widget.config.json").write_text(
        json.dumps(body.config or {}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # 让 PSConfigManager 立刻 pick up
    ps.config.load()

    return {"ok": True, "widget_id": body.widget_id, "widget_class": body.widget_class}


@router.get("/personal_spaces/{ps_id}/widgets/{widget_id}")
async def get_widget_info(ps_id: str, widget_id: str) -> dict[str, Any]:
    psm = _get_psm()
    try:
        ps = await psm.get(ps_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    manifest_raw = ps.config.get_widget_manifest(widget_id)
    if not manifest_raw:
        raise HTTPException(status_code=404, detail=f"Widget {widget_id} not found")

    reg = _get_registry()
    reg.load()
    class_id = manifest_raw.get("widget_class")
    class_defaults = reg.get_defaults(class_id) if class_id and reg.has(class_id) else {}
    resolved = ps.config.resolve_widget(widget_id, widget_class_defaults=class_defaults)

    class_info: dict[str, Any] | None = None
    if class_id and reg.has(class_id):
        spec = reg.get(class_id)
        class_info = {
            "class_id": spec.class_id,
            "title": spec.title,
            "config_schema": spec.config_schema,
            "has_hook": spec.hook_class is not None,
        }
    return {
        "ps_id": ps_id,
        "widget_id": widget_id,
        "manifest": manifest_raw,
        "widget_config": ps.config.get_widget_config_raw(widget_id),
        "resolved_config": resolved,
        "widget_class": class_info,
    }


@router.patch("/personal_spaces/{ps_id}/widgets/{widget_id}")
async def update_widget_manifest(
    ps_id: str, widget_id: str, body: UpdateWidgetManifestRequest
) -> dict[str, Any]:
    """更新 widget.json manifest 字段（目前仅支持 title）。"""
    psm = _get_psm()
    try:
        ps = await psm.get(ps_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    wjson = ps.ps_root / "widgets" / widget_id / "widget.json"
    if not wjson.exists():
        raise HTTPException(status_code=404, detail=f"Widget {widget_id} not found")
    try:
        raw = json.loads(wjson.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read widget.json: {e}")
    if body.title is not None:
        raw["title"] = body.title
    tmp = wjson.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(wjson)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write widget.json: {e}")
    ps.config.reload_file(wjson)
    return {"ok": True}


@router.patch("/personal_spaces/{ps_id}/widgets/{widget_id}/config")
async def update_widget_config(
    ps_id: str, widget_id: str, body: UpdateWidgetConfigRequest
) -> dict[str, Any]:
    psm = _get_psm()
    try:
        ps = await psm.get(ps_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ps.config.get_widget_manifest(widget_id):
        raise HTTPException(status_code=404, detail=f"Widget {widget_id} not found")
    ps.config.write_widget_config(widget_id, body.config)
    return {"ok": True}


@router.delete("/personal_spaces/{ps_id}/widgets/{widget_id}")
async def delete_widget(ps_id: str, widget_id: str) -> dict[str, Any]:
    psm = _get_psm()
    try:
        ps = await psm.get(ps_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    widget_dir = ps.ps_root / "widgets" / widget_id
    if not widget_dir.exists():
        raise HTTPException(status_code=404, detail=f"Widget {widget_id} not found")
    # 卸载运行时实例（若已加载）
    runtime_widget = ps._widgets.pop(widget_id, None)
    if runtime_widget is not None:
        try:
            await runtime_widget.unload()
        except Exception:
            _rlog.warning("web_api", f"[psapi] unload {widget_id} 失败")
    # 物理删除目录
    import shutil
    shutil.rmtree(widget_dir, ignore_errors=True)
    ps.config.load()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Highlight
# ---------------------------------------------------------------------------


@router.get("/personal_spaces/{ps_id}/widgets/{widget_id}/highlight")
async def get_widget_highlight(ps_id: str, widget_id: str) -> dict[str, Any]:
    """返回 widget hook.compute_highlight() 结果；无 hook 则返回 ``{}``。"""
    psm = _get_psm()
    try:
        ps = await psm.get(ps_id)
        widget = await ps.get_widget(widget_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Widget {widget_id} not found")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Baseline: config-level fields always available (even before widget is loaded)
    resolved = widget.resolved_config or {}
    agent_cfg = resolved.get("agent") or {}
    context_cfg = resolved.get("context") or {}
    highlight: dict[str, Any] = {}
    if agent_cfg.get("llm"):
        highlight["llm"] = str(agent_cfg["llm"])
    if agent_cfg.get("type"):
        highlight["agent_type"] = str(agent_cfg["type"])
    if context_cfg.get("type"):
        highlight["context_type"] = str(context_cfg["type"])
    # Hook data merges on top (runtime state overrides static config)
    if widget.hook is not None:
        try:
            res = widget.hook.compute_highlight()
            if isinstance(res, dict):
                highlight.update(res)
        except Exception as e:
            _rlog.warning("web_api", f"[psapi] compute_highlight failed: {e}")
    return {"ps_id": ps_id, "widget_id": widget_id, "highlight": highlight}


# ---------------------------------------------------------------------------
# Messages (recent chat history)
# ---------------------------------------------------------------------------


@router.get("/personal_spaces/{ps_id}/widgets/{widget_id}/messages")
async def get_widget_messages(
    ps_id: str,
    widget_id: str,
    limit: int = Query(10, ge=1, le=100),
) -> dict[str, Any]:
    """返回最近 limit 条 user/assistant 消息（过滤工具调用等中间消息）。"""
    psm = _get_psm()
    try:
        ps = await psm.get(ps_id)
        widget = await ps.get_widget(widget_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Widget {widget_id} not found")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    if widget.context_handler is None:
        return {"ps_id": ps_id, "widget_id": widget_id, "messages": []}
    try:
        raw = await widget.context_handler.get_recent_chat_messages(limit)
    except Exception as e:
        _rlog.warning("web_api", f"[psapi] get_recent_chat_messages failed: {e}")
        return {"ps_id": ps_id, "widget_id": widget_id, "messages": []}
    messages = [
        {
            "id": m.get("request_id") or m.get("id") or str(i),
            "role": m["role"],
            "text": m.get("content") or "",
            "timestamp": m.get("timestamp") or "",
        }
        for i, m in enumerate(raw)
    ]
    return {"ps_id": ps_id, "widget_id": widget_id, "messages": messages}


# ---------------------------------------------------------------------------
# History (full chat history from history_manager)
# ---------------------------------------------------------------------------


@router.get("/personal_spaces/{ps_id}/widgets/{widget_id}/history")
async def get_widget_history(
    ps_id: str,
    widget_id: str,
) -> dict[str, Any]:
    """返回 widget 的完整 history（从 history_manager.load_all() 读取）。"""
    psm = _get_psm()
    try:
        ps = await psm.get(ps_id)
        widget = await ps.get_widget(widget_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Widget {widget_id} not found")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    if widget.context_handler is None:
        return {"ps_id": ps_id, "widget_id": widget_id, "history": []}
    try:
        history = widget.context_handler.history_manager.load_all()
    except Exception as e:
        _rlog.warning("web_api", f"[psapi] history_manager.load_all failed: {e}")
        return {"ps_id": ps_id, "widget_id": widget_id, "history": []}
    return {"ps_id": ps_id, "widget_id": widget_id, "history": history}


# ---------------------------------------------------------------------------
# Streams (流式消息历史——从 JSONL 磁盘文件读取)
# ---------------------------------------------------------------------------


@router.get("/personal_spaces/{ps_id}/widgets/{widget_id}/streams")
async def get_widget_streams(
    ps_id: str,
    widget_id: str,
    request_id: str | None = Query(None, description="Filter by request_id"),
) -> dict[str, Any]:
    """返回 widget 下所有已完成流的 chunk 历史（从磁盘 JSONL 读取）。

    可选 ``?request_id=xxx`` 过滤单个流。
    每个 chunk 包含完整的 PSReply 字段（type, chunk_type, content, seq 等）。
    """
    psm = _get_psm()
    if not (psm.root_path / ps_id).exists():
        raise HTTPException(status_code=404, detail=f"PS {ps_id} not found")

    streams_dir = Path(PYCLAEGO_DEFAULT_LOGS_ROOT) / "streams" / ps_id / widget_id
    streams: list[dict[str, Any]] = []

    if streams_dir.is_dir():
        for fname in sorted(streams_dir.iterdir()):
            if not fname.suffix == ".jsonl":
                continue
            r_id = fname.stem
            if request_id and r_id != request_id:
                continue
            chunks: list[dict[str, Any]] = []
            try:
                for line in fname.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        # _seq 是内部序列号，暴露为 seq
                        if "_seq" in rec and "seq" not in rec:
                            rec["seq"] = rec["_seq"]
                        chunks.append(rec)
                    except json.JSONDecodeError:
                        pass
            except Exception:
                _rlog.warning("web_api", f"[psapi] 读取 stream 文件失败: {fname}")
                continue
            streams.append({
                "request_id": r_id,
                "status": "finished",
                "chunks": chunks,
            })

    return {"ps_id": ps_id, "widget_id": widget_id, "streams": streams}


# ---------------------------------------------------------------------------
# Agent Streams (按 Agent 维度的流式历史)
# ---------------------------------------------------------------------------


@router.get("/personal_spaces/{ps_id}/widgets/{widget_id}/agent_streams")
async def get_widget_agent_streams(
    ps_id: str,
    widget_id: str,
) -> dict[str, Any]:
    """返回 widget 下所有 Agent 流的摘要信息（磁盘扫描）。

    用于 AgentTreePanel 发现有哪些子 Agent 流存在。

    Returns:
        {ps_id, widget_id, agents: [{request_id, subagent_id?, finished, chunk_count}]}
        主 Agent 条目不包含 subagent_id 字段。
    """
    psm = _get_psm()
    if not (psm.root_path / ps_id).exists():
        raise HTTPException(status_code=404, detail=f"PS {ps_id} not found")

    result: list[dict[str, Any]] = []
    streams_dir = Path(PYCLAEGO_DEFAULT_LOGS_ROOT) / "streams" / ps_id / widget_id
    if streams_dir.is_dir():
        for d in sorted(streams_dir.iterdir()):
            if d.is_dir():
                # 新格式：子目录 = request_id，内含各 subagent_id.jsonl
                r_id = d.name
                for sf in sorted(d.iterdir()):
                    if sf.suffix == ".jsonl":
                        s_id = sf.stem
                        chunk_count = _count_jsonl_lines(sf)
                        result.append({
                            "request_id": r_id,
                            "subagent_id": s_id,
                            "finished": True,
                            "chunk_count": chunk_count,
                        })
                # 若该 request 也有旧扁平文件（主 Agent），一并列出
                flat = streams_dir / f"{d.name}.jsonl"
                if flat.exists():
                    result.append({
                        "request_id": d.name,
                        "finished": True,
                        "chunk_count": _count_jsonl_lines(flat),
                    })
            elif d.suffix == ".jsonl":
                # 旧格式：扁平文件 = 主 Agent
                r_id = d.stem
                # 跳过已从子目录识别出的主 Agent（避免重复）
                sub_dir = streams_dir / r_id
                if sub_dir.is_dir():
                    continue
                result.append({
                    "request_id": r_id,
                    "finished": True,
                    "chunk_count": _count_jsonl_lines(d),
                })
    return {"ps_id": ps_id, "widget_id": widget_id, "agents": result}


@router.get(
    "/personal_spaces/{ps_id}/widgets/{widget_id}/agent_streams/{request_id}/{subagent_id}"
)
async def get_agent_stream_chunks(
    ps_id: str,
    widget_id: str,
    request_id: str,
    subagent_id: str,
) -> dict[str, Any]:
    """返回指定 Agent 流的完整 chunk 历史。

    子 Agent JSONL 路径: streams/{ps}/{wid}/{req}/{subagent_id}.jsonl
    主 Agent 用 subagent_id="_main" 查询旧扁平路径。
    """
    from ..personal_space.stream_state import MAIN_AGENT

    psm = _get_psm()
    if not (psm.root_path / ps_id).exists():
        raise HTTPException(status_code=404, detail=f"PS {ps_id} not found")

    streams_dir = Path(PYCLAEGO_DEFAULT_LOGS_ROOT) / "streams" / ps_id / widget_id
    if subagent_id == MAIN_AGENT:
        filepath = streams_dir / f"{request_id}.jsonl"
    else:
        filepath = streams_dir / request_id / f"{subagent_id}.jsonl"

    if not filepath.exists():
        return {"ps_id": ps_id, "widget_id": widget_id, "request_id": request_id,
                "subagent_id": subagent_id, "chunks": [], "found": False}

    chunks: list[dict[str, Any]] = []
    try:
        for line in filepath.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if "_seq" in rec and "seq" not in rec:
                    rec["seq"] = rec["_seq"]
                chunks.append(rec)
            except json.JSONDecodeError:
                pass
    except Exception:
        pass

    return {
        "ps_id": ps_id, "widget_id": widget_id,
        "request_id": request_id, "subagent_id": subagent_id,
        "chunks": chunks, "found": True,
    }


# ---------------------------------------------------------------------------
# View (schema-driven UI)
# ---------------------------------------------------------------------------


@router.get("/personal_spaces/{ps_id}/widgets/{widget_id}/view")
async def get_widget_view(ps_id: str, widget_id: str) -> dict[str, Any]:
    """Return ViewSchema describing how the widget detail panel should render."""
    psm = _get_psm()
    try:
        ps = await psm.get(ps_id)
        widget = await ps.get_widget(widget_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Widget {widget_id} not found")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        schema = widget.compute_view()
        # Pydantic model → dict; plain dict passes through
        if hasattr(schema, "model_dump"):
            schema_dict = schema.model_dump()
        else:
            schema_dict = dict(schema) if isinstance(schema, dict) else {"type": "kv_table", "rows": []}
    except Exception as e:
        _rlog.warning("web_api", f"[psapi] compute_view failed: {e}")
        schema_dict = {"type": "kv_table", "rows": [["error", str(e)]]}
    return {"ps_id": ps_id, "widget_id": widget_id, "view": schema_dict}


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@router.post("/personal_spaces/{ps_id}/widgets/{widget_id}/commands")
async def send_widget_command(
    ps_id: str, widget_id: str, body: WidgetCommand
) -> dict[str, Any]:
    """Dispatch a frontend command (send, stop, …) to the widget runtime."""
    psm = _get_psm()
    try:
        ps = await psm.get(ps_id)
        widget = await ps.get_widget(widget_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Widget {widget_id} not found")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        result = await widget.handle_command(body.command, body.args)
    except Exception as e:
        _rlog.error("web_api", f"[psapi] handle_command error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    return result


# ---------------------------------------------------------------------------
# Cron CRUD
# ---------------------------------------------------------------------------


@router.get("/personal_spaces/{ps_id}/widgets/{widget_id}/cron")
async def get_widget_cron(ps_id: str, widget_id: str) -> dict[str, Any]:
    """返回 widget.json 中的 cron 数组。"""
    psm = _get_psm()
    try:
        ps = await psm.get(ps_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    wjson = ps.ps_root / "widgets" / widget_id / "widget.json"
    if not wjson.exists():
        raise HTTPException(status_code=404, detail=f"Widget {widget_id} not found")
    try:
        raw = json.loads(wjson.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read widget.json: {e}")
    return {"ps_id": ps_id, "widget_id": widget_id, "cron": raw.get("cron") or []}


@router.put("/personal_spaces/{ps_id}/widgets/{widget_id}/cron")
async def update_widget_cron(
    ps_id: str, widget_id: str, body: UpdateWidgetCronRequest
) -> dict[str, Any]:
    """替换 widget.json 中的完整 cron 数组，并通知 WidgetCronScheduler 热更新。"""
    from ..personal_space.cron import WidgetCronTrigger
    from ..personal_space.cron.scheduler import WidgetCronScheduler

    psm = _get_psm()
    try:
        ps = await psm.get(ps_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    wjson = ps.ps_root / "widgets" / widget_id / "widget.json"
    if not wjson.exists():
        raise HTTPException(status_code=404, detail=f"Widget {widget_id} not found")

    # Validate each entry before writing
    errors: list[str] = []
    for idx, item in enumerate(body.cron):
        fb_id = f"cr_{idx:02d}"
        try:
            WidgetCronTrigger.from_dict(item, fallback_id=fb_id)
        except Exception as exc:
            errors.append(f"cron[{idx}]: {exc}")
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))

    # Write updated widget.json atomically
    try:
        raw = json.loads(wjson.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read widget.json: {e}")
    raw["cron"] = body.cron
    tmp = wjson.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(wjson)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write widget.json: {e}")

    # Hot-reload APScheduler jobs if scheduler is running
    scheduler = WidgetCronScheduler.get_instance()
    count = 0
    if scheduler is not None:
        try:
            count = scheduler.reload_widget_crons(ps_id, widget_id)
        except Exception as e:
            _rlog.warning("web_api", f"[psapi] reload_widget_crons failed: {e}")

    return {"ok": True, "count": count}
