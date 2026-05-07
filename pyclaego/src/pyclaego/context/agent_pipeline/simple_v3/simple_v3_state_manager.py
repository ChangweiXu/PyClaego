"""SimpleV3StateManager — 状态持久化管理器

管理 ``.simple_v3/state.json``：scratch（工作草稿）和 summaries（历史摘要）的持久化与恢复。

关键设计：
- state.json 是**派生缓存**，来源是 history 文件，丢失后可全量重建
- 原子写入：先写 .tmp 再 rename
- 增量恢复：通过 last_summarized_group 标记，只重建新 groups
- 断点续传：每处理完一个 group 就落盘
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ....context.token_counter import TokenCounter
from ....logging import get_running_log

_rlog = get_running_log()

# 模块级 TokenCounter（lazy init）
_token_counter: TokenCounter | None = None


def _get_token_counter() -> TokenCounter:
    global _token_counter
    if _token_counter is None:
        _token_counter = TokenCounter()
    return _token_counter


def _estimate_tokens(text: str) -> int:
    """使用 tiktoken 精确估算 token 数"""
    if not text:
        return 0
    return _get_token_counter().count_tokens(text)


class SimpleV3StateManager:
    """状态持久化管理器。

    管理 scratch 和 summaries 的读写、增量更新和全量重建。
    """

    _DEFAULT_STATE_DIR = ".simple_v3"
    _DEFAULT_STATE_FILE = "state.json"

    def __init__(
        self,
        workspace_path: Path,
        state_dirname: str = "",
        state_filename: str = "",
    ) -> None:
        self._workspace = workspace_path.expanduser().resolve()
        self._state_dirname = state_dirname or self._DEFAULT_STATE_DIR
        self._state_filename = state_filename or self._DEFAULT_STATE_FILE

        # 进程内状态缓存（避免反复读磁盘）
        self._state: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------

    @property
    def state_path(self) -> Path:
        return self._workspace / self._state_dirname / self._state_filename

    def _ensure_dir(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 加载 / 保存
    # ------------------------------------------------------------------

    def load(self) -> dict[str, Any]:
        """加载 state.json，不存在则返回初始空状态。"""
        if self._state is not None:
            return self._state

        if self.state_path.exists():
            try:
                raw = self.state_path.read_text("utf-8")
                self._state = json.loads(raw)
                _rlog.info(
                    "simple_v3_state",
                    f"[StateManager] 已加载 {self.state_path} "
                    f"(last_summarized_group={self._state.get('last_summarized_group', 'N/A')})",
                )
                return self._state
            except Exception as e:
                _rlog.error(
                    "simple_v3_state",
                    f"[StateManager] 加载 {self.state_path} 失败: {e}，使用空状态",
                )

        self._state = self._empty_state()
        return self._state

    def save(self, state: dict[str, Any] | None = None) -> None:
        """原子写入 state.json（先 .tmp 再 rename）。"""
        if state is not None:
            self._state = state

        if self._state is None:
            return

        self._state["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._ensure_dir()

        tmp_path = self.state_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2),
                "utf-8",
            )
            os.replace(tmp_path, self.state_path)
        except Exception as e:
            _rlog.error(
                "simple_v3_state",
                f"[StateManager] 保存 state.json 失败: {e}",
            )
            # 清理临时文件
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    def invalidate_cache(self) -> None:
        """强制下次 load 重新从磁盘读取。"""
        self._state = None

    # ------------------------------------------------------------------
    # 初始状态
    # ------------------------------------------------------------------

    def _empty_state(self) -> dict[str, Any]:
        return {
            "version": 1,
            "session_id": "",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": "",
            "scratch": "",
            "scratch_tokens": 0,
            "summaries": {},
            "last_summarized_group": "",
            "total_groups_summarized": 0,
        }

    # ------------------------------------------------------------------
    # Scratch 接口
    # ------------------------------------------------------------------

    def get_scratch(self) -> str:
        """返回当前 scratch 文本。"""
        state = self.load()
        return state.get("scratch", "") or ""

    def set_scratch(self, text: str, tokens: int = 0) -> None:
        """设置 scratch 文本并持久化。"""
        state = self.load()
        state["scratch"] = text
        state["scratch_tokens"] = tokens if tokens > 0 else self._estimate_tokens(text)
        self.save()

    # ------------------------------------------------------------------
    # Summaries 接口
    # ------------------------------------------------------------------

    def get_summaries_for_context(self, token_budget: int) -> str:
        """渲染 summaries 为可注入 system prompt 的文本。

        按时间升序排列，累计 token 不超过预算。
        """
        state = self.load()
        summaries_dict: dict[str, dict[str, Any]] = state.get("summaries", {})
        if not summaries_dict:
            return ""

        # 按 group_id 排序（group_id 包含时间戳，天然可排序）
        ordered = sorted(summaries_dict.items())

        blocks: list[str] = []
        total_tokens = 0

        for group_id, info in ordered:
            summary = info.get("summary", "")
            if not summary:
                continue
            block = f"[{group_id}] {summary}"
            block_tokens = self._estimate_tokens(block)
            if total_tokens + block_tokens > token_budget:
                remaining = len(ordered) - len(blocks) - 1
                if remaining > 0:
                    blocks.append(
                        f"... (省略 {remaining} 条更早的摘要以控制 token budget)"
                    )
                break
            blocks.append(block)
            total_tokens += block_tokens

        if not blocks:
            return ""

        return "【更早的对话摘要】\n" + "\n".join(blocks)

    def add_summary(self, group_id: str, summary: str, tokens: int = 0) -> None:
        """添加一条 group 摘要。"""
        state = self.load()
        summaries = state.setdefault("summaries", {})
        summaries[group_id] = {
            "summary": summary,
            "tokens": tokens if tokens > 0 else self._estimate_tokens(summary),
            "summarized_at": datetime.now(timezone.utc).isoformat(),
        }
        state["last_summarized_group"] = group_id
        state["total_groups_summarized"] = len(summaries)
        self.save()

    # ------------------------------------------------------------------
    # 组合更新（after_loop 调用）
    # ------------------------------------------------------------------

    def update_after_turn(
        self,
        group_id: str,
        scratch: str,
        summary: str,
    ) -> None:
        """同时更新 scratch 和添加 summary，一次落盘。"""
        state = self.load()

        if scratch:
            state["scratch"] = scratch
            state["scratch_tokens"] = self._estimate_tokens(scratch)

        if summary and group_id:
            summaries = state.setdefault("summaries", {})
            summaries[group_id] = {
                "summary": summary,
                "tokens": self._estimate_tokens(summary),
                "summarized_at": datetime.now(timezone.utc).isoformat(),
            }
            state["last_summarized_group"] = group_id
            state["total_groups_summarized"] = len(summaries)

        self.save()

    # ------------------------------------------------------------------
    # 恢复 / 重建
    # ------------------------------------------------------------------

    def get_last_summarized_group(self) -> str:
        """返回最后被 summarize 的 group_id，用于增量判断。"""
        state = self.load()
        return state.get("last_summarized_group", "") or ""

    def ensure_state(
        self,
        session_id: str,
        groups_since_last: list[str],
        summarize_fn=None,
    ) -> dict[str, Any]:
        """确保 state 最新：增量处理 last_summarized_group 之后的 groups。

        Args:
            session_id: 当前 session ID
            groups_since_last: last_summarized_group 之后的新 group_id 列表
            summarize_fn: async (group_id) → summary_str 的摘要生成函数

        Returns:
            最新 state dict
        """
        state = self.load()

        # 确保 session_id 一致
        if state.get("session_id") and state["session_id"] != session_id:
            _rlog.warning(
                "simple_v3_state",
                f"[StateManager] session_id 不匹配，重置 state "
                f"(old={state['session_id']}, new={session_id})",
            )
            state = self._empty_state()
        state["session_id"] = session_id

        if not groups_since_last:
            self.save(state)
            return state

        # 有新的 groups 需要处理，但不在此处调用 LLM（由调用方同步处理）
        self.save(state)
        return state

    def rebuild_from_summaries(
        self,
        session_id: str,
        summaries: dict[str, str],
        scratch: str = "",
    ) -> None:
        """用外部生成的 summaries 全量重建 state（用于恢复）。"""
        state = self._empty_state()
        state["session_id"] = session_id

        summaries_dict: dict[str, dict[str, Any]] = {}
        for group_id, summary in summaries.items():
            summaries_dict[group_id] = {
                "summary": summary,
                "tokens": self._estimate_tokens(summary),
                "summarized_at": datetime.now(timezone.utc).isoformat(),
            }

        state["summaries"] = summaries_dict
        state["scratch"] = scratch
        state["scratch_tokens"] = self._estimate_tokens(scratch)
        state["last_summarized_group"] = (
            max(summaries.keys()) if summaries else ""
        )
        state["total_groups_summarized"] = len(summaries)

        self.save()
        _rlog.info(
            "simple_v3_state",
            f"[StateManager] 全量重建完成: "
            f"{len(summaries)} summaries, scratch={len(scratch)} chars",
        )

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Token 估算（委托给模块级 TokenCounter）"""
        return _estimate_tokens(text)

    def get_all_group_ids(self) -> list[str]:
        """返回所有已有 summary 的 group_id 列表。"""
        state = self.load()
        return sorted(state.get("summaries", {}).keys())
