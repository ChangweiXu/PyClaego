"""SoulV6TurnBriefSynthesizer — 轮末摘要生成（fire-and-forget）

在每一轮对话结束后（handle_after_loop），基于该 group 的完整消息用 LLM 生成一份结构化的 "turn brief"：
    {
        "summary": "这一轮做了什么（1-2 句）",
        "decisions": ["做出的决定"],
        "facts_established": ["被确认的事实"],
        "entities_touched": ["涉及的实体名"],
        "open_loops": ["未闭合的问题/承诺"],
        "artifacts": ["产出的文件/工具结果 tool_call_id"],
        "outcome": "success|partial|failed",
        "turn_tokens": <int>
    }

磁盘布局：
    .memory/soul_v6/briefs/{session_id}/{group_id}.json

**fire-and-forget**：`synthesize_async(...)` 立即返回，后台 task 执行。
**等待**：`wait_brief_done(session_id, timeout)` 在下一轮开始时调用（短超时 0.5s）。
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
import traceback
from typing import Any, Dict, List, Optional

from ..task_manager import SessionTaskHandlerV2, TaskType
from .soulv6_memory_manager import SoulV6MemoryManager
from ..logging import get_running_log

_rlog = get_running_log()

_BRIEF_SYSTEM = """你是会话摘要助手。用户会给你一段对话转录（单轮 = user→assistant，可能含多次工具调用）。\
你需要输出一份严格的 JSON 对象（不要 markdown 代码块，直接输出 `{ ... }`），字段如下：

{
  "summary": "这一轮做了什么，1-2 句中文",
  "decisions": ["做出的决定（可为空数组）"],
  "facts_established": ["在这一轮确认的事实（可为空）"],
  "entities_touched": ["涉及的人/项目/专有名词（可为空）"],
  "open_loops": ["用户提出但未完全解决的问题/承诺（可为空）"],
  "artifacts": ["引用的 tool_call_id 或文件路径（可为空）"],
  "outcome": "success | partial | failed"
}

只输出 JSON，不要任何其他文字。"""


@dataclass
class SoulV6TurnBrief:
    session_id: str
    group_id: str
    created_at: str
    summary: str = ""
    decisions: List[str] = field(default_factory=list)
    facts_established: List[str] = field(default_factory=list)
    entities_touched: List[str] = field(default_factory=list)
    open_loops: List[str] = field(default_factory=list)
    artifacts: List[str] = field(default_factory=list)
    outcome: str = "success"
    turn_tokens: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def render_compact(self) -> str:
        """渲染为简洁的文本块，用于作为历史层注入上下文"""
        parts = [f"[{self.group_id}] {self.summary}".strip()]
        if self.decisions:
            parts.append("  决定: " + "; ".join(self.decisions))
        if self.facts_established:
            parts.append("  事实: " + "; ".join(self.facts_established))
        if self.entities_touched:
            parts.append("  实体: " + ", ".join(self.entities_touched))
        if self.open_loops:
            parts.append("  未闭合: " + "; ".join(self.open_loops))
        if self.artifacts:
            parts.append("  产物: " + ", ".join(self.artifacts))
        if self.outcome and self.outcome != "success":
            parts.append(f"  结局: {self.outcome}")
        return "\n".join(parts)


class SoulV6TurnBriefSynthesizer:
    """轮末摘要生成器（异步 fire-and-forget）"""

    _instance: Optional["SoulV6TurnBriefSynthesizer"] = None

    def __init__(self, memory_manager: Optional[SoulV6MemoryManager] = None) -> None:
        self._memory_manager = memory_manager or SoulV6MemoryManager.get_instance()
        # session_id → group_id → asyncio.Task
        self._pending: Dict[str, Dict[str, asyncio.Task]] = {}
        # session_id → {group_id: SoulV6TurnBrief} 进程内缓存
        self._cache: Dict[str, Dict[str, SoulV6TurnBrief]] = {}

    @classmethod
    def get_instance(cls) -> "SoulV6TurnBriefSynthesizer":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------------
    # fire-and-forget 入口
    # ------------------------------------------------------------------

    def synthesize_async(
        self,
        session_id: str,
        group_id: str,
        messages: List[Dict[str, Any]],
        session_task_handler: "SessionTaskHandlerV2",
    ) -> asyncio.Task:
        """立即返回，后台生成并持久化 brief"""
        pending = self._pending.setdefault(session_id, {})
        # 若已在进行中，返回现有任务（避免重复）
        if group_id in pending and not pending[group_id].done():
            return pending[group_id]

        task = asyncio.create_task(
            self._synthesize_and_persist(session_id, group_id, messages, session_task_handler)
        )
        pending[group_id] = task
        task.add_done_callback(lambda _t: pending.pop(group_id, None))
        return task

    async def wait_brief_done(
        self, session_id: str, timeout: float = 0.5
    ) -> int:
        """等待当前 session 所有 pending brief 完成，最多 `timeout` 秒。
        返回已完成（包括超时前已完成）的任务数量。"""
        pending = self._pending.get(session_id)
        if not pending:
            return 0
        tasks = [t for t in pending.values() if not t.done()]
        if not tasks:
            return len(pending)
        done, _ = await asyncio.wait(tasks, timeout=timeout)
        return len(done)

    # ------------------------------------------------------------------
    # 读取（用于 history_briefs 层）
    # ------------------------------------------------------------------

    async def load_brief(
        self, session_id: str, group_id: str
    ) -> Optional[SoulV6TurnBrief]:
        cache = self._cache.setdefault(session_id, {})
        if group_id in cache:
            return cache[group_id]
        path = self._memory_manager.brief_path(session_id, group_id)
        if not path.exists():
            return None
        try:
            raw = await asyncio.to_thread(path.read_text, "utf-8")
            data = json.loads(raw)
            brief = SoulV6TurnBrief(**data)
            cache[group_id] = brief
            return brief
        except Exception as e:
            _rlog.error(
                f"session_{session_id}",
                f"[SoulV6TurnBriefSynthesizer] 读取 brief 失败 {path}: {e}",
            )
            return None

    async def load_briefs_for_groups(
        self, session_id: str, group_ids: List[str]
    ) -> List[SoulV6TurnBrief]:
        briefs: List[SoulV6TurnBrief] = []
        for gid in group_ids:
            b = await self.load_brief(session_id, gid)
            if b is not None:
                briefs.append(b)
        return briefs

    # ------------------------------------------------------------------
    # 核心：LLM 调用 + 持久化
    # ------------------------------------------------------------------

    async def _synthesize_and_persist(
        self,
        session_id: str,
        group_id: str,
        messages: List[Dict[str, Any]],
        session_task_handler: SessionTaskHandlerV2,
    ) -> None:
        if not session_task_handler:
            raise ValueError("SessionTaskHandlerV2 未设置")
        mem_brf_handler = await session_task_handler.create_sibling_task(
            task_type=TaskType.MEMORY_BRIEF,
            name=f"SoulV6 synthesize brief (group={group_id})",
            metadata={
                "group_id": group_id,
                "messages": len(messages),
                "chars": sum(
                    len(str(m.get("content") or "")) for m in messages
                ),
            },
        )

        try:
            transcript = self._messages_to_transcript(messages)
            tc = self._memory_manager.token_counter
            turn_tokens = tc.count_tokens(transcript)
            # 极短轮直接产生 minimal brief
            if turn_tokens < 40:
                brief = SoulV6TurnBrief(
                    session_id=session_id,
                    group_id=group_id,
                    created_at=datetime.now().isoformat(),
                    summary=transcript[:120].replace("\n", " "),
                    turn_tokens=turn_tokens,
                )
                await self._persist(brief)
                return

            reply = await self._call_llm(mem_brf_handler, transcript)
            brief = self._parse_reply(reply, session_id, group_id, turn_tokens)
            await self._persist(brief)
            await mem_brf_handler.log_info(
                f"[SoulV6TurnBrief] synthesized group={group_id} "
                f"tokens={turn_tokens} decisions={len(brief.decisions)} "
                f"open_loops={len(brief.open_loops)}",
            )
            await mem_brf_handler.complete()
        except asyncio.CancelledError:
            await mem_brf_handler.cancel()
            raise
        except Exception as e:
            await mem_brf_handler.log_error(
                f"[SoulV6TurnBrief] 生成失败 group={group_id}: {e}\n{traceback.format_exc()}",
            )
            await mem_brf_handler.fail()

    async def _call_llm(self, session_task_handler: "SessionTaskHandlerV2", transcript: str) -> Optional[str]:
        if not session_task_handler:
            raise ValueError("SessionTaskHandlerV2 未设置")
        from ..security_executor.handler import SecurityHandler
        from ..llm import UnifiedMessage

        security = SecurityHandler.get_instance()
        result = await security.request_llm_call_v3(
            session_task_handler=session_task_handler,
            llm_id=self._memory_manager.llm_id,
            system=_BRIEF_SYSTEM,
            messages=[UnifiedMessage(role="user", text=transcript)],
            max_tokens=800,
        )
        if result.get("success") and result.get("v2_response"):
            return result["v2_response"].text
        return None

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _messages_to_transcript(self, messages: List[Dict[str, Any]]) -> str:
        """将一组 dict 消息拼成可读转录"""
        lines: List[str] = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "") or ""
            tool_calls = m.get("tool_calls") or []
            tool_results = m.get("tool_results") or []
            if isinstance(content, list):
                # content_parts 的原始形式，尽量抽出文本
                texts = []
                for p in content:
                    if isinstance(p, dict):
                        texts.append(str(p.get("text", "")))
                    else:
                        texts.append(str(p))
                content = "\n".join(t for t in texts if t)
            lines.append(f"[{role}] {content}".rstrip())
            for tc in tool_calls:
                nm = tc.get("name", "")
                args = tc.get("arguments", "")
                tid = tc.get("id", "")
                lines.append(f"  →tool_call {nm}(id={tid}) args={str(args)[:200]}")
            for tr in tool_results:
                nm = tr.get("tool_name", "")
                tid = tr.get("tool_call_id", "")
                cc = str(tr.get("content", ""))[:400]
                lines.append(f"  ←tool_result {nm}(id={tid}) {cc}")
        return "\n".join(lines)

    def _parse_reply(
        self,
        reply: Optional[str],
        session_id: str,
        group_id: str,
        turn_tokens: int,
    ) -> SoulV6TurnBrief:
        brief = SoulV6TurnBrief(
            session_id=session_id,
            group_id=group_id,
            created_at=datetime.now().isoformat(),
            turn_tokens=turn_tokens,
        )
        if not reply:
            brief.summary = "(brief 生成失败)"
            brief.outcome = "failed"
            return brief
        # 容错抽取 JSON 对象
        text = reply.strip()
        # strip possible ```json fences
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not m:
            brief.summary = text[:200]
            return brief
        try:
            data = json.loads(m.group(0))
        except Exception:
            brief.summary = text[:200]
            return brief

        def _s_list(v: Any) -> List[str]:
            if isinstance(v, list):
                return [str(x) for x in v if str(x).strip()]
            if isinstance(v, str) and v.strip():
                return [v.strip()]
            return []

        brief.summary = str(data.get("summary", "")).strip()
        brief.decisions = _s_list(data.get("decisions"))
        brief.facts_established = _s_list(data.get("facts_established"))
        brief.entities_touched = _s_list(data.get("entities_touched"))
        brief.open_loops = _s_list(data.get("open_loops"))
        brief.artifacts = _s_list(data.get("artifacts"))
        outcome = str(data.get("outcome", "success")).strip().lower()
        if outcome not in {"success", "partial", "failed"}:
            outcome = "success"
        brief.outcome = outcome
        return brief

    async def _persist(self, brief: SoulV6TurnBrief) -> None:
        path = self._memory_manager.brief_path(brief.session_id, brief.group_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(brief.to_dict(), ensure_ascii=False, indent=2)
        await asyncio.to_thread(path.write_text, raw, "utf-8")
        self._cache.setdefault(brief.session_id, {})[brief.group_id] = brief
