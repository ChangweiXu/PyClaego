"""SoulV6MemoryManager — SoulV5MemoryManager 的 V6 兄弟版

继承 V5 的全部能力（MD 文件树 + SQLite 索引 + LLM 驱动的总结），
但：

1. **独立单例**（自己的 ``_instance``）。V5 和 V6 可以共存，互不干扰。
2. **独立配置**：读取 ``context_global.soul_v6_memory``（不是 soul_v5_memory）。
3. **独立磁盘路径**：默认 ``.memory/soul_v6/``。
4. **额外子目录**：V6 在 V5 基础上新增
   - ``briefs/{session_id}/``         TurnBrief JSON
   - ``entities/``                    实体卡片 JSON
   - ``open_loops/``                  未闭合问题 JSON
   - ``turn_artifacts/{group_id}/``   磁盘上的原始工具结果

V6 的新功能（budget allocator / tool result store / brief synthesizer 等）
由独立模块提供，本 manager 仅负责文件树 + 索引。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Dict, Optional

import aiosqlite

from .soulv5_memory_manager import SoulV5MemoryManager
from .token_counter import TokenCounter
from ..config import get_config
from ..logging import get_running_log

_rlog = get_running_log()

# V6 新增子目录
_DIR_V6_BRIEFS = "briefs"
_DIR_V6_ENTITIES = "entities"
_DIR_V6_OPEN_LOOPS = "open_loops"
_DIR_V6_TURN_ARTIFACTS = "turn_artifacts"


class SoulV6MemoryManager(SoulV5MemoryManager):
    """V6 记忆管理器（独立单例）"""

    # 覆盖父类的 _instance，让子类有独立的单例
    _instance: Optional["SoulV6MemoryManager"] = None

    def __new__(cls) -> "SoulV6MemoryManager":  # type: ignore[override]
        if cls._instance is None:
            cls._instance = object.__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    @classmethod
    def get_instance(cls) -> "SoulV6MemoryManager":  # type: ignore[override]
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:  # type: ignore[override]
        # 跳过 V5 的 __init__（不能复用，因为配置 key 不同）
        if getattr(self, "_initialized", False):
            return

        config = get_config()
        mem_config: Dict[str, Any] = config.get("context_global", {}).get(
            "soul_v6_memory", {}
        )

        # 路径
        default_root = os.path.join(
            config.get("pyclaego", {}).get("root_path", "~/.pyclaego"),
            ".memory", "soul_v6",
        )
        self.md_root = Path(mem_config.get("md_root", default_root)).expanduser()
        self.db_path = Path(
            mem_config.get("db_path", str(self.md_root / "_index.db"))
        ).expanduser()

        # LLM
        self.llm_id: str = mem_config.get("llm_id", "")

        # Token 预算（V5 兼容字段，供父类方法使用）
        budget = mem_config.get("token_budget", {})
        self.context_window_cap: int = budget.get("context_window_cap", 131_072)
        self.preference_cap: int = budget.get("preference_cap", 2_000)
        self.experience_inject_cap: int = budget.get("experience_inject_cap", 8_000)
        self.topic_index_cap: int = budget.get("topic_index_cap", 1_500)

        # 工具输出截断
        self.tool_content_truncation_threshold: int = budget.get(
            "tool_content_truncation_threshold", 2_000
        )
        self.tool_content_head_chars: int = budget.get(
            "tool_content_head_chars", 1_000
        )
        self.tool_content_tail_chars: int = budget.get(
            "tool_content_tail_chars", 500
        )

        # 压缩配置
        compress = mem_config.get("compression", {})
        self.auto_group_threshold: int = compress.get("auto_group_threshold", 8)
        self.group_compress_stop_at: int = compress.get(
            "group_compress_stop_at", max(1, self.auto_group_threshold // 2)
        )
        self.auto_case_threshold: int = compress.get("auto_case_threshold", 4)
        self.cluster_token_budget: int = compress.get("cluster_token_budget", 12_000)
        self.max_exps_per_topic: int = compress.get("max_exps_per_topic", 5)

        # 记忆召回配置（V6 特有 key，同时 fallback V5 的 key）
        self.recall_config: Dict[str, Any] = mem_config.get("memory_recall", {})

        # Budget allocator 配置（V6 新增）
        self.budget_config: Dict[str, Any] = mem_config.get("budget", {})

        # Token 计数器
        tiktoken_model = mem_config.get("tiktoken_model", "gpt-4")
        self.token_counter = TokenCounter(model=tiktoken_model)

        # 运行时状态
        self._db: Optional[aiosqlite.Connection] = None
        self._write_lock = asyncio.Lock()
        self._db_ready = False
        self._session_compress_locks: Dict[str, asyncio.Lock] = {}

        self._initialized = True
        _rlog.info(
            "soulv6_memory",
            f"[SoulV6MemoryManager] 单例初始化完成 "
            f"(md_root={self.md_root}, db_path={self.db_path})",
        )

    # ------------------------------------------------------------------
    # 目录结构
    # ------------------------------------------------------------------

    def ensure_directory_structure(self) -> None:  # type: ignore[override]
        """创建 V6 MD 文件树目录结构（在 V5 基础上新增 briefs/entities/open_loops/turn_artifacts）"""
        super().ensure_directory_structure()
        for subdir in (
            _DIR_V6_BRIEFS,
            _DIR_V6_ENTITIES,
            _DIR_V6_OPEN_LOOPS,
            _DIR_V6_TURN_ARTIFACTS,
        ):
            (self.md_root / subdir).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # V6 新增路径 helper
    # ------------------------------------------------------------------

    def briefs_dir(self, session_id: str) -> Path:
        """TurnBrief 目录：``.memory/soul_v6/briefs/{session_id}/``"""
        return self.md_root / _DIR_V6_BRIEFS / session_id

    def brief_path(self, session_id: str, group_id: str) -> Path:
        """单个 TurnBrief 文件路径"""
        return self.briefs_dir(session_id) / f"{group_id}.json"

    def entities_dir(self) -> Path:
        """实体卡片目录：``.memory/soul_v6/entities/``"""
        return self.md_root / _DIR_V6_ENTITIES

    def entity_path(self, slug: str) -> Path:
        return self.entities_dir() / f"{slug}.json"

    def open_loops_dir(self) -> Path:
        """未闭合问题目录：``.memory/soul_v6/open_loops/``"""
        return self.md_root / _DIR_V6_OPEN_LOOPS

    def open_loops_path(self, session_id: str) -> Path:
        return self.open_loops_dir() / f"{session_id}.json"

    def turn_artifacts_dir(self, group_id: str) -> Path:
        """磁盘工具结果目录：``.memory/soul_v6/turn_artifacts/{group_id}/``"""
        return self.md_root / _DIR_V6_TURN_ARTIFACTS / group_id

    def tool_artifact_path(self, group_id: str, tool_call_id: str) -> Path:
        """单个工具结果文件：``.memory/soul_v6/turn_artifacts/{group_id}/{tool_call_id}.txt``"""
        # 清理 tool_call_id 以避免路径注入
        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in tool_call_id)
        return self.turn_artifacts_dir(group_id) / f"{safe_id}.txt"
