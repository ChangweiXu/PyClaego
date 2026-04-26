"""TaskArtifactStore - 任务工件存储

为每个 task_id 持久化关联的工件（LLM 响应、工具参数/结果、错误堆栈等），
供任务图谱仪表盘按需懒加载。

设计要点:
- 单例。CoreScheduler 进程写入；Web 进程通过磁盘读取（跨进程）。
- 磁盘布局（扁平）：
    {cache_root}/{task_id}/index.json
    {cache_root}/{task_id}/{artifact_id}.{ext}
- 每个工件由 index.json 里的一行描述 (ArtifactRef)，再加一个独立文件存 payload。
- 默认存储路径：~/.pyclaego/.cache/task_artifact/

文件不大、列表小，故每次 attach 重写 index.json（小文件原子写）。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

# ============================================================
# Data classes
# ============================================================

# 工件类型枚举
KIND_LLM_RESPONSE = "llm_response"
KIND_TOOL_ARGS = "tool_args"
KIND_TOOL_RESULT = "tool_result"
KIND_ERROR_TRACE = "error_trace"
KIND_META = "meta"
KIND_FILE_EDIT = "file_edit"

_TEXT_KINDS = {KIND_LLM_RESPONSE, KIND_TOOL_ARGS, KIND_TOOL_RESULT,
               KIND_ERROR_TRACE, KIND_META, KIND_FILE_EDIT}


@dataclass
class ArtifactRef:
    """单个工件引用"""
    artifact_id: str
    task_id: str
    kind: str
    name: str           # 显示名 (例如 "tool_args[read_file]")
    mime: str           # mime 类型
    size: int           # payload bytes
    ext: str            # 存盘扩展名
    created_at: float   # epoch seconds
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ============================================================
# Store
# ============================================================

class TaskArtifactStore:
    """任务工件存储（单例）"""

    _instance: Optional["TaskArtifactStore"] = None
    _lock = asyncio.Lock()

    def __init__(self, cache_root: Optional[Path] = None) -> None:
        if cache_root is None:
            cache_root = Path.home() / "pyclaego" / ".cache" / "task_artifact"
        self.cache_root: Path = Path(cache_root)
        self.cache_root.mkdir(parents=True, exist_ok=True)
        # 进程内简单缓存 (task_id -> [ArtifactRef])，避免重复读盘
        self._index_cache: Dict[str, List[ArtifactRef]] = {}

    # -- singleton --
    @classmethod
    def get_instance(cls) -> "TaskArtifactStore":
        if cls._instance is None:
            cls._instance = TaskArtifactStore()
        return cls._instance

    # -- paths --
    def _task_dir(self, task_id: str) -> Path:
        # task_id 已经是 "{session_id}-{ts}-{rand}"，不会含路径分隔符
        safe = task_id.replace("/", "_").replace("..", "_")
        return self.cache_root / safe

    def _index_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "index.json"

    def _artifact_path(self, task_id: str, artifact_id: str, ext: str) -> Path:
        return self._task_dir(task_id) / f"{artifact_id}.{ext}"

    # -- internal: index io --
    def _load_index(self, task_id: str) -> List[ArtifactRef]:
        if task_id in self._index_cache:
            return self._index_cache[task_id]
        idx = self._index_path(task_id)
        if not idx.exists():
            self._index_cache[task_id] = []
            return []
        try:
            data = json.loads(idx.read_text(encoding="utf-8"))
            refs = [ArtifactRef(**d) for d in data]
        except Exception:
            refs = []
        self._index_cache[task_id] = refs
        return refs

    def _save_index(self, task_id: str, refs: List[ArtifactRef]) -> None:
        d = self._task_dir(task_id)
        d.mkdir(parents=True, exist_ok=True)
        idx = self._index_path(task_id)
        tmp = idx.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps([r.to_dict() for r in refs], ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        os.replace(tmp, idx)
        self._index_cache[task_id] = refs

    # -- public: attach --
    def attach(
        self,
        task_id: str,
        kind: str,
        payload: Union[str, bytes, Dict[str, Any], List[Any]],
        name: Optional[str] = None,
        mime: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> ArtifactRef:
        """挂载一个工件。同步落盘。

        - payload 接受 str/bytes/dict/list；dict/list 会被 json 序列化。
        - mime/ext 会按 kind 默认推导，也可以显式覆盖。
        """
        # 序列化
        if isinstance(payload, (dict, list)):
            data_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            default_mime = "application/json"
            default_ext = "json"
        elif isinstance(payload, bytes):
            data_bytes = payload
            default_mime = "application/octet-stream"
            default_ext = "bin"
        else:
            text = str(payload)
            data_bytes = text.encode("utf-8")
            default_mime = "text/plain; charset=utf-8"
            default_ext = "txt"

        eff_mime = mime or default_mime
        eff_ext = default_ext
        if "json" in eff_mime:
            eff_ext = "json"
        elif "html" in eff_mime:
            eff_ext = "html"

        artifact_id = uuid.uuid4().hex[:12]
        ref = ArtifactRef(
            artifact_id=artifact_id,
            task_id=task_id,
            kind=kind,
            name=name or kind,
            mime=eff_mime,
            size=len(data_bytes),
            ext=eff_ext,
            created_at=time.time(),
            extra=extra or {},
        )

        # 写 payload
        path = self._artifact_path(task_id, artifact_id, eff_ext)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data_bytes)

        # 更新 index
        refs = self._load_index(task_id)
        refs.append(ref)
        self._save_index(task_id, refs)
        return ref

    # -- public: list / fetch --
    def list_for_task(self, task_id: str) -> List[Dict[str, Any]]:
        """列出某 task_id 下所有 artifact 的元数据。"""
        # 直接读盘（跨进程场景），不走缓存
        refs: List[ArtifactRef] = []
        idx = self._index_path(task_id)
        if idx.exists():
            try:
                data = json.loads(idx.read_text(encoding="utf-8"))
                refs = [ArtifactRef(**d) for d in data]
            except Exception:
                refs = []
        return [r.to_dict() for r in refs]

    def fetch(self, task_id: str, artifact_id: str) -> Optional[Tuple[bytes, str, str]]:
        """取出一个 artifact 的 (bytes, mime, name)。"""
        for d in self.list_for_task(task_id):
            if d["artifact_id"] == artifact_id:
                p = self._artifact_path(task_id, artifact_id, d["ext"])
                if p.exists():
                    return p.read_bytes(), d["mime"], d["name"]
        return None

    # -- public: digest --
    def digest(self, task_id: str) -> Dict[str, Any]:
        """聚合摘要：按 kind 计数，便于前端徽标显示。"""
        counts: Dict[str, int] = {}
        total_size = 0
        for d in self.list_for_task(task_id):
            counts[d["kind"]] = counts.get(d["kind"], 0) + 1
            total_size += int(d.get("size", 0))
        return {"by_kind": counts, "total_size": total_size}


# ============================================================
# Reporter (convenience helper bound to a task_id)
# ============================================================

class ArtifactReporter:
    """绑定到单个 task_id 的便捷工件上报器。

    用法：
        reporter = ArtifactReporter.for_task(task_id)
        reporter.tool_args("read_file", {"path": "/x"})
        reporter.tool_result("read_file", "file contents...")
        reporter.llm_response({...})
        reporter.error_trace(traceback_str)
    """

    def __init__(self, task_id: str, store: Optional[TaskArtifactStore] = None) -> None:
        self.task_id = task_id
        self._store = store or TaskArtifactStore.get_instance()

    @classmethod
    def for_task(cls, task_id: str) -> "ArtifactReporter":
        return cls(task_id)

    # — kind-specific helpers —

    def llm_response(self, payload: Union[str, Dict[str, Any]], name: str = "llm_response") -> ArtifactRef:
        return self._store.attach(self.task_id, KIND_LLM_RESPONSE, payload, name=name)

    def tool_args(self, tool_name: str, args: Dict[str, Any]) -> ArtifactRef:
        return self._store.attach(
            self.task_id, KIND_TOOL_ARGS, args,
            name=f"args[{tool_name}]",
            extra={"tool_name": tool_name},
        )

    def tool_result(
        self,
        tool_name: str,
        content: Union[str, Dict[str, Any]],
        success: bool = True,
    ) -> ArtifactRef:
        return self._store.attach(
            self.task_id, KIND_TOOL_RESULT, content,
            name=f"result[{tool_name}]",
            extra={"tool_name": tool_name, "success": success},
        )

    def error_trace(self, trace: str, name: str = "error_trace") -> ArtifactRef:
        return self._store.attach(self.task_id, KIND_ERROR_TRACE, trace, name=name)

    def meta(self, name: str, payload: Union[str, Dict[str, Any]]) -> ArtifactRef:
        return self._store.attach(self.task_id, KIND_META, payload, name=name)

    def file_edit(self, file_path: str, summary: Union[str, Dict[str, Any]]) -> ArtifactRef:
        return self._store.attach(
            self.task_id, KIND_FILE_EDIT, summary,
            name=f"file_edit[{file_path}]",
            extra={"file_path": file_path},
        )

    # — passthrough helpers —

    def list(self) -> List[Dict[str, Any]]:
        return self._store.list_for_task(self.task_id)

    def digest(self) -> Dict[str, Any]:
        return self._store.digest(self.task_id)
