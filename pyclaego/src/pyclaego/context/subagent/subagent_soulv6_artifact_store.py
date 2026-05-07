"""SubAgentSoulV6ArtifactStore — 子 Agent 工具结果磁盘存储

问题背景：
    子 Agent 在多轮工具调用中产生的大型工具输出（如大文件读取、网页抓取）会
    持续占用上下文窗口，导致后续轮次 token 超限或内容被截断。

解决方案：
    工具结果超过 token 阈值时，将完整内容写入磁盘，消息列表中只保留
    一个"提要 + 指针"占位符。子 Agent 若需完整内容，可调用
    ``subagent_soulv6_tool_result_read`` 工具按需读取。

存储路径：
    ``{workspace_path}/.subagent_artifacts/{session_id}/{safe_tool_call_id}.txt``

设计特性：
    - 独立（无 SoulV6MemoryManager 依赖），自带 TokenCounter
    - 并发安全：按 session_id 分桶写锁
    - 幂等：同一 tool_call_id 写入覆盖旧文件
    - **无 cleanup 方法**：artifacts 持久保留，由 TaskHandler 上报后供调试
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

from ...logging import get_running_log

_rlog = get_running_log()

# 工具调用 ID 中的非法文件名字符替换为 _
_SAFE_RE = re.compile(r"[^\w\-.]")


def _safe_filename(tool_call_id: str) -> str:
    return _SAFE_RE.sub("_", tool_call_id)[:120]


@dataclass
class SubAgentSoulV6Artifact:
    """单个工具结果的磁盘元数据"""
    tool_call_id: str
    tool_name: str
    session_id: str
    path: Path
    total_tokens: int
    total_chars: int
    head_text: str
    tail_text: str
    stored_at: str  # ISO timestamp


@dataclass
class SubAgentSoulV6ReadSlice:
    """按 char range 读取的内容片段"""
    tool_call_id: str
    start_char: int
    end_char: int
    total_chars: int
    total_tokens: int
    text: str
    truncated: bool


class SubAgentSoulV6ArtifactStore:
    """子 Agent 工具结果磁盘存储

    实例级对象（每个 SubAgentSoulV6ContextHandler 持有一个），
    不是单例——每个子 Agent 运行产生独立的 artifact 空间。
    """

    def __init__(
        self,
        workspace_path: Path,
        spill_token_threshold: int = 2_000,
        head_chars: int = 1_000,
        tail_chars: int = 500,
    ) -> None:
        self._workspace_path = workspace_path
        self.spill_token_threshold = spill_token_threshold
        self.head_chars = head_chars
        self.tail_chars = tail_chars

        # lazy import 避免循环依赖
        from ..token_counter import TokenCounter
        self._token_counter = TokenCounter()

        # 写锁按 session_id 分桶
        self._session_locks: dict[str, asyncio.Lock] = {}
        # 进程内元数据缓存
        self._artifacts: dict[str, SubAgentSoulV6Artifact] = {}

    # ------------------------------------------------------------------
    # 路径辅助
    # ------------------------------------------------------------------

    def _artifact_dir(self, session_id: str) -> Path:
        return self._workspace_path / ".subagent_artifacts" / session_id

    def _artifact_path(self, session_id: str, tool_call_id: str) -> Path:
        return self._artifact_dir(session_id) / f"{_safe_filename(tool_call_id)}.txt"

    # ------------------------------------------------------------------
    # 判断是否需要落盘
    # ------------------------------------------------------------------

    def should_spill(self, content: str) -> bool:
        if not content:
            return False
        return self.count_tokens(content) > self.spill_token_threshold

    def count_tokens(self, content: str) -> int:
        return self._token_counter.count_tokens(content) if content else 0

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    async def spill(
        self,
        session_id: str,
        tool_call_id: str,
        tool_name: str,
        content: str,
    ) -> SubAgentSoulV6Artifact:
        """将工具结果写入磁盘，返回 artifact 元数据"""
        from datetime import datetime, timezone

        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            artifact_dir = self._artifact_dir(session_id)
            artifact_dir.mkdir(parents=True, exist_ok=True)
            path = self._artifact_path(session_id, tool_call_id)

            await asyncio.to_thread(path.write_text, content, "utf-8")

            total_chars = len(content)
            total_tokens = self.count_tokens(content)
            head_text = content[: self.head_chars]
            tail_text = (
                content[-self.tail_chars:]
                if total_chars > self.head_chars + self.tail_chars and self.tail_chars > 0
                else ""
            )

            artifact = SubAgentSoulV6Artifact(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                session_id=session_id,
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
                f"[SubAgentSoulV6ArtifactStore] spill tool_call_id={tool_call_id} "
                f"tool={tool_name} tokens={total_tokens} chars={total_chars} → {path}",
            )
            return artifact

    # ------------------------------------------------------------------
    # 占位符渲染
    # ------------------------------------------------------------------

    def render_placeholder(self, artifact: SubAgentSoulV6Artifact) -> str:
        """生成放入 ToolCallResult.content 的缩略占位文本"""
        lines = [
            f"[SubAgentSoulV6 tool_result spilled — tool={artifact.tool_name}, "
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
            f'[Full content available via `subagent_soulv6_tool_result_read('
            f'tool_call_id="{artifact.tool_call_id}", char_start=0, char_end=...)`]',
        ])
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    async def read(
        self,
        tool_call_id: str,
        char_range: tuple[int, int] | None = None,
    ) -> SubAgentSoulV6ReadSlice | None:
        """从磁盘按 char range 读取工具结果。

        读取量以 token 数为上限（spill_token_threshold），确保读回的内容
        不会在 handle_after_tool_calls 中再次触发落盘。

        Args:
            tool_call_id: 工具调用 ID
            char_range: (start, end) 字符索引；None 表示从 start=0 开始读取

        Returns:
            None 若找不到 artifact；否则 SubAgentSoulV6ReadSlice
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
        text = self._token_counter.truncate_text_to_tokens(raw_text, self.spill_token_threshold)
        end = start + len(text)
        truncated = end < total_chars

        return SubAgentSoulV6ReadSlice(
            tool_call_id=tool_call_id,
            start_char=start,
            end_char=end,
            total_chars=total_chars,
            total_tokens=artifact.total_tokens,
            text=text,
            truncated=truncated,
        )

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_artifact(self, tool_call_id: str) -> SubAgentSoulV6Artifact | None:
        return self._artifacts.get(tool_call_id)

    def list_artifacts(self, session_id: str) -> list[SubAgentSoulV6Artifact]:
        """返回属于指定 session 的全部 artifact 元数据列表"""
        return [a for a in self._artifacts.values() if a.session_id == session_id]
