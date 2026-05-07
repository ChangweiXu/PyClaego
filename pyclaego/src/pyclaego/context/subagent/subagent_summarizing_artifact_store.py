"""SubAgentSummarizingArtifactStore — 子 Agent 工具结果磁盘存储（Summarizing 版）

存储策略（两档阈值）：
  - content < warn_tokens:  不落盘，不触发冻结循环，inline 原样保留。
  - content >= warn_tokens 且 <= truncate_tokens:
                            落盘（完整内容）；inline 原样保留；触发冻结循环。
  - content > truncate_tokens:
                            落盘（完整内容）；inline 截断至 truncate_tokens + 截断提示；触发冻结循环。

所有落盘结果均有对应 artifact，因此驱逐工具（tool_result_summarize_and_evict）
的 "Full content available via tool_result_read" 字段始终有效，无需分支处理。

存储路径：
    {workspace_path}/.subagent_summarizing_artifacts/{session_id}/{safe_tool_call_id}.txt
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ...logging import get_running_log

_rlog = get_running_log()

_SAFE_RE = re.compile(r"[^\w\-.]")


def _safe_filename(tool_call_id: str) -> str:
    return _SAFE_RE.sub("_", tool_call_id)[:120]


@dataclass
class SubAgentSummarizingArtifact:
    """单个工具结果的磁盘元数据"""
    tool_call_id: str
    tool_name: str
    session_id: str
    path: Path
    total_tokens: int
    total_chars: int
    stored_at: str  # ISO timestamp


class SubAgentSummarizingArtifactStore:
    """子 Agent 工具结果磁盘存储（Summarizing 策略版）

    每个 SubAgentSummarizingContextHandler 持有一个实例。
    不是单例——每次子 Agent 运行产生独立的 artifact 空间。

    Args:
        workspace_path:   工作区根目录
        warn_tokens:      内容 token 数 >= 此值时落盘并触发冻结循环（默认 5_000）
        truncate_tokens:  内容 token 数 > 此值时 inline 截断（默认 10_000）
    """

    def __init__(
        self,
        workspace_path: Path,
        warn_tokens: int = 5_000,
        truncate_tokens: int = 10_000,
    ) -> None:
        self._workspace_path = workspace_path
        self.warn_tokens = warn_tokens
        self.truncate_tokens = truncate_tokens

        from ..token_counter import TokenCounter
        self._token_counter = TokenCounter()

        self._session_locks: dict[str, asyncio.Lock] = {}
        self._artifacts: dict[str, SubAgentSummarizingArtifact] = {}

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _artifact_dir(self, session_id: str) -> Path:
        return self._workspace_path / ".subagent_summarizing_artifacts" / session_id

    def _artifact_path(self, session_id: str, tool_call_id: str) -> Path:
        return self._artifact_dir(session_id) / f"{_safe_filename(tool_call_id)}.txt"

    # ------------------------------------------------------------------
    # Token helpers
    # ------------------------------------------------------------------

    def count_tokens(self, content: str) -> int:
        return self._token_counter.count_tokens(content) if content else 0

    # ------------------------------------------------------------------
    # Spill decision
    # ------------------------------------------------------------------

    def should_spill(self, content: str) -> bool:
        """Return True when content should be spilled to disk (and trigger frozen loop)."""
        if not content:
            return False
        return self.count_tokens(content) >= self.warn_tokens

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def spill(
        self,
        session_id: str,
        tool_call_id: str,
        tool_name: str,
        content: str,
    ) -> SubAgentSummarizingArtifact:
        """Write full content to disk; return artifact metadata."""
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            artifact_dir = self._artifact_dir(session_id)
            artifact_dir.mkdir(parents=True, exist_ok=True)
            path = self._artifact_path(session_id, tool_call_id)

            await asyncio.to_thread(path.write_text, content, "utf-8")

            total_chars = len(content)
            total_tokens = self.count_tokens(content)

            artifact = SubAgentSummarizingArtifact(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                session_id=session_id,
                path=path,
                total_tokens=total_tokens,
                total_chars=total_chars,
                stored_at=datetime.now(timezone.utc).isoformat(),
            )
            self._artifacts[tool_call_id] = artifact

            _rlog.info(
                f"session_{session_id}",
                f"[SubAgentSummarizingArtifactStore] spill "
                f"tool_call_id={tool_call_id} tool={tool_name} "
                f"tokens={total_tokens} chars={total_chars} → {path}",
            )
            return artifact

    # ------------------------------------------------------------------
    # Inline rendering
    # ------------------------------------------------------------------

    def render_inline(self, artifact: SubAgentSummarizingArtifact, content: str) -> str:
        """Return the string to place inline in the context message.

        - total_tokens <= truncate_tokens → return content verbatim (full).
        - total_tokens > truncate_tokens  → first truncate_tokens tokens + truncation marker.
        """
        if artifact.total_tokens <= self.truncate_tokens:
            return content

        truncated = self._token_counter.truncate_text_to_tokens(content, self.truncate_tokens)
        marker = (
            f"\n\n[Tool result truncated: {artifact.total_tokens} tokens total, "
            f"showing first {self.truncate_tokens}. "
            f"Full content on disk — use "
            f"tool_result_read(tool_call_id=\"{artifact.tool_call_id}\") to read more.]"
        )
        return truncated + marker

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_artifact(self, tool_call_id: str) -> SubAgentSummarizingArtifact | None:
        return self._artifacts.get(tool_call_id)

    def list_artifacts(self, session_id: str) -> list[SubAgentSummarizingArtifact]:
        """Return all artifacts belonging to the given session."""
        return [a for a in self._artifacts.values() if a.session_id == session_id]
