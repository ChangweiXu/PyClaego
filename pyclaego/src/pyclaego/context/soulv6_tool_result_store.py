"""SoulV6ToolResultStore — 磁盘支持的工具结果存储

问题背景（V5）：
    当一个工具返回 100KB 文本时，整段文本驻留在上下文消息列表里直到对话结束/压缩，
    吃掉上下文窗口，后续 LLM 调用都要付费重新处理。

V6 方案：
    1. 在工具结果进入消息之前，若其 token 数超过阈值，把完整原文写到磁盘：
       ``.memory/soul_v6/turn_artifacts/{group_id}/{tool_call_id}.txt``
    2. 消息里只放一个"提要 + 引用指针"（head/tail/token 统计 + ``tool_call_id``）。
    3. LLM 若需要完整内容，调用 ``tool_result_read(tool_call_id, range=...)`` 工具
       按需从磁盘取回（那是 Phase 2 的下一个模块）。

关键特性：
    - 并发安全：每个 session 一把写锁
    - 幂等：同一 ``tool_call_id`` 写入会覆盖（最新优先）
    - 审计：所有写入通过 SessionTaskHandlerV2.log_info 记录
    - 不污染 V5：V6 文件独立存放在 ``turn_artifacts/`` 子目录
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from ..logging import get_running_log
from .soulv6_memory_manager import SoulV6MemoryManager

_rlog = get_running_log()


@dataclass
class SoulV6StoredArtifact:
    """磁盘上一个工具结果的元数据"""
    tool_call_id: str
    tool_name: str
    group_id: str
    path: Path
    total_tokens: int
    total_chars: int
    head_text: str          # 缩略消息中展示的头部
    tail_text: str          # 缩略消息中展示的尾部（可为空）
    stored_at: str          # ISO timestamp


@dataclass
class SoulV6ReadSlice:
    """按 range 读取的片段"""
    tool_call_id: str
    start_char: int
    end_char: int
    total_chars: int
    total_tokens: int
    text: str
    truncated: bool         # 返回值是否被二次截断


class SoulV6ToolResultStore:
    """磁盘支持的工具结果存储

    单例（通过 `get_instance()`），写锁按 session_id 分桶。
    """

    _instance: SoulV6ToolResultStore | None = None

    def __init__(
        self,
        memory_manager: SoulV6MemoryManager | None = None,
        spill_token_threshold: int = 2_000,
        head_chars: int = 1_000,
        tail_chars: int = 500,
    ) -> None:
        self._memory_manager = memory_manager or SoulV6MemoryManager.get_instance()
        self.spill_token_threshold = spill_token_threshold
        self.head_chars = head_chars
        self.tail_chars = tail_chars

        self._session_locks: dict[str, asyncio.Lock] = {}
        # 进程内元数据缓存：tool_call_id → artifact
        self._artifacts: dict[str, SoulV6StoredArtifact] = {}

    @classmethod
    def get_instance(cls) -> SoulV6ToolResultStore:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------------
    # 判断是否需要落盘
    # ------------------------------------------------------------------

    def should_spill(self, content: str) -> bool:
        """给定工具结果文本，判断是否应落盘"""
        if not content:
            return False
        tokens = self._memory_manager.token_counter.count_tokens(content)
        return tokens > self.spill_token_threshold

    def count_tokens(self, content: str) -> int:
        return self._memory_manager.token_counter.count_tokens(content) if content else 0

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    async def spill(
        self,
        session_id: str,
        group_id: str,
        tool_call_id: str,
        tool_name: str,
        content: str,
    ) -> SoulV6StoredArtifact:
        """把一个工具结果写到磁盘，返回元数据

        注意：本方法不自行构造"缩略消息"，调用方应根据返回的 artifact
        自行构造要放进 UnifiedMessage 的替代文本。
        """
        from datetime import datetime, timezone

        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            self._memory_manager.turn_artifacts_dir(group_id).mkdir(
                parents=True, exist_ok=True
            )
            path = self._memory_manager.tool_artifact_path(group_id, tool_call_id)

            # 写入磁盘
            await asyncio.to_thread(path.write_text, content, "utf-8")

            total_chars = len(content)
            total_tokens = self.count_tokens(content)
            head_text = content[: self.head_chars]
            tail_text = (
                content[-self.tail_chars :]
                if total_chars > self.head_chars + self.tail_chars and self.tail_chars > 0
                else ""
            )

            artifact = SoulV6StoredArtifact(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                group_id=group_id,
                path=path,
                total_tokens=total_tokens,
                total_chars=total_chars,
                head_text=head_text,
                tail_text=tail_text,
                stored_at=datetime.now(timezone.utc).isoformat(),
            )
            self._artifacts[tool_call_id] = artifact

            _rlog.info(
                f"session_{session_id}",
                f"[SoulV6ToolResultStore] spill tool_call_id={tool_call_id} "
                f"tool={tool_name} tokens={total_tokens} chars={total_chars} "
                f"→ {path}",
            )
            return artifact

    # ------------------------------------------------------------------
    # 构建"缩略消息"内容
    # ------------------------------------------------------------------

    def render_placeholder(self, artifact: SoulV6StoredArtifact) -> str:
        """返回要塞进 ToolCallResult.content 的缩略文本"""
        lines = [
            f"[SoulV6 tool_result spilled — tool={artifact.tool_name}, "
            f"tool_call_id={artifact.tool_call_id}, "
            f"tokens={artifact.total_tokens}, chars={artifact.total_chars}]",
            "",
            f"---- HEAD (first {len(artifact.head_text)} chars) ----",
            artifact.head_text,
        ]
        if artifact.tail_text:
            lines.extend([
                "",
                f"---- TAIL (last {len(artifact.tail_text)} chars) ----",
                artifact.tail_text,
            ])
        lines.extend([
            "",
            f"[Full content available via `tool_result_read("
            f"tool_call_id=\"{artifact.tool_call_id}\", range=[start,end])`]",
        ])
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    async def read(
        self,
        tool_call_id: str,
        char_range: tuple[int, int] | None = None,
    ) -> SoulV6ReadSlice | None:
        """按 char range 读取磁盘上的内容。

        读取量以 token 数为上限（spill_token_threshold），确保读回的内容
        不会在 handle_after_tool_calls 中再次触发落盘。

        Args:
            tool_call_id: 工具调用 ID
            char_range: (start, end) 字符索引；None 表示从 start=0 开始读取

        Returns:
            None 如果找不到 artifact；否则 SoulV6ReadSlice
        """
        artifact = self._artifacts.get(tool_call_id)
        if artifact is None or not artifact.path.exists():
            return None

        content = await asyncio.to_thread(artifact.path.read_text, "utf-8")
        total_chars = len(content)

        if char_range:
            start = max(0, min(char_range[0], total_chars))
            candidate_end = max(start, min(char_range[1], total_chars))
        else:
            start = 0
            candidate_end = total_chars

        # 用 token 预算截断，保证读回内容不超过 spill_token_threshold
        raw_text = content[start:candidate_end]
        snippet = self._memory_manager.token_counter.truncate_text_to_tokens(
            raw_text, self.spill_token_threshold
        )
        end = start + len(snippet)
        truncated = end < total_chars

        return SoulV6ReadSlice(
            tool_call_id=tool_call_id,
            start_char=start,
            end_char=end,
            total_chars=total_chars,
            total_tokens=artifact.total_tokens,
            text=snippet,
            truncated=truncated,
        )

    # ------------------------------------------------------------------
    # 元数据访问（供 StaleEvictor / 调试使用）
    # ------------------------------------------------------------------

    def get_artifact(self, tool_call_id: str) -> SoulV6StoredArtifact | None:
        return self._artifacts.get(tool_call_id)

    def list_by_group(self, group_id: str) -> dict[str, SoulV6StoredArtifact]:
        return {
            tcid: art
            for tcid, art in self._artifacts.items()
            if art.group_id == group_id
        }

    def forget(self, tool_call_id: str) -> bool:
        """从内存缓存中移除（不删磁盘文件；保留以备审计）"""
        return self._artifacts.pop(tool_call_id, None) is not None
