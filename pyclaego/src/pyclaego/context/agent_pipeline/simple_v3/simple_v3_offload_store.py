"""SimpleV3OffloadStore — 公共内容卸载存储（银行保险柜）

独立于任何上下文策略，session 级隔离。提供按 key 存储/检索完整消息内容的能力。

Key 格式：``{group_id}/{message_index}``（如 ``g_20260506_001/4``）
磁盘路径：``{workspace}/.offload/{session_id}/{key}.json``
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ....context.token_counter import TokenCounter
from ....logging import get_running_log

_rlog = get_running_log()

# 模块级 TokenCounter（lazy init，默认 gpt-4 编码）
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


@dataclass
class SimpleV3StoredContent:
    """磁盘上一条卸载内容的元数据"""
    key: str
    content_type: str           # "text" | "tool_result" | "binary"
    content: str                # 完整原始内容
    summary: str = ""           # llm_mini 生成的摘要
    original_tokens: int = 0
    stored_at: str = ""
    session_id: str = ""
    group_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "content_type": self.content_type,
            "content": self.content,
            "summary": self.summary,
            "original_tokens": self.original_tokens,
            "stored_at": self.stored_at,
            "session_id": self.session_id,
            "group_id": self.group_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SimpleV3StoredContent:
        return cls(
            key=d.get("key", ""),
            content_type=d.get("content_type", "text"),
            content=d.get("content", ""),
            summary=d.get("summary", ""),
            original_tokens=int(d.get("original_tokens", 0)),
            stored_at=d.get("stored_at", ""),
            session_id=d.get("session_id", ""),
            group_id=d.get("group_id", ""),
        )


class SimpleV3OffloadStore:
    """公共内容卸载存储。

    线程安全：通过 asyncio.Lock 按 key 分桶保护写操作。
    内存缓存：key → SimpleV3StoredContent，避免重复磁盘读取。
    """

    _DEFAULT_SUBDIR = ".offload"

    def __init__(
        self,
        workspace_path: Path,
        subdir: str = "",
    ) -> None:
        self._workspace = workspace_path.expanduser().resolve()
        self._subdir = subdir or self._DEFAULT_SUBDIR

        # 内存缓存：key → SimpleV3StoredContent
        self._cache: dict[str, SimpleV3StoredContent] = {}
        # 写锁：按 key 分桶
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # 路径工具
    # ------------------------------------------------------------------

    def _offload_root(self) -> Path:
        return self._workspace / self._subdir

    def _session_dir(self, session_id: str) -> Path:
        return self._offload_root() / session_id

    def _key_path(self, session_id: str, key: str) -> Path:
        # 清理 key 中的路径分隔符，防止目录穿越
        safe_key = key.replace("/", "_").replace("\\", "_")
        return self._session_dir(session_id) / f"{safe_key}.json"

    # ------------------------------------------------------------------
    # Token 估算
    # ------------------------------------------------------------------

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Token 估算（委托给模块级 TokenCounter）"""
        return _estimate_tokens(text)

    # ------------------------------------------------------------------
    # 核心接口
    # ------------------------------------------------------------------

    async def store(
        self,
        key: str,
        content: str,
        content_type: str = "text",
        summary: str = "",
        session_id: str = "",
        group_id: str = "",
    ) -> str:
        """将内容写入磁盘并缓存，返回 key。

        幂等：同 key 覆盖。
        """
        if not key:
            raise ValueError("key 不能为空")

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            stored = SimpleV3StoredContent(
                key=key,
                content_type=content_type,
                content=content,
                summary=summary,
                original_tokens=self.estimate_tokens(content),
                stored_at=datetime.now(timezone.utc).isoformat(),
                session_id=session_id,
                group_id=group_id,
            )

            path = self._key_path(session_id, key)
            path.parent.mkdir(parents=True, exist_ok=True)

            await asyncio.to_thread(
                path.write_text,
                json.dumps(stored.to_dict(), ensure_ascii=False, indent=2),
                "utf-8",
            )

            self._cache[key] = stored

            _rlog.info(
                f"session_{session_id}",
                f"[SimpleV3OffloadStore] store key={key} "
                f"type={content_type} tokens={stored.original_tokens}",
            )
            return key

    async def retrieve(self, key: str) -> SimpleV3StoredContent | None:
        """按 key 检索完整内容。"""
        if not key:
            return None

        # 先查缓存
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        # 从磁盘加载（需要遍历可能的 session 目录）
        return await self._retrieve_from_disk(key)

    async def _retrieve_from_disk(self, key: str) -> SimpleV3StoredContent | None:
        """从磁盘搜索并加载指定 key 的内容。"""
        root = self._offload_root()
        if not root.exists():
            return None

        safe_key = key.replace("/", "_").replace("\\", "_")
        filename = f"{safe_key}.json"

        # 遍历所有 session 目录
        for session_dir in root.iterdir():
            if not session_dir.is_dir():
                continue
            candidate = session_dir / filename
            if candidate.exists():
                try:
                    raw = await asyncio.to_thread(
                        candidate.read_text, "utf-8"
                    )
                    data = json.loads(raw)
                    stored = SimpleV3StoredContent.from_dict(data)
                    self._cache[key] = stored
                    return stored
                except Exception as e:
                    _rlog.error(
                        "simple_v3_offload",
                        f"[SimpleV3OffloadStore] 读取 {candidate} 失败: {e}",
                    )
        return None

    def render_placeholder(
        self,
        key: str,
        summary: str = "",
        content_type: str = "text",
        original_tokens: int = 0,
    ) -> str:
        """生成可注入 messages 的占位文本。"""
        extra = ""
        if original_tokens > 0:
            extra += f" (~{original_tokens} tokens)"
        if content_type and content_type != "text":
            extra += f" [{content_type}]"

        if summary:
            return (
                f"[内容已卸载: {key}{extra}]\n" f"摘要: {summary}"
            )
        return f"[内容已卸载: {key}{extra}]"

    async def list_keys(self, session_id: str) -> list[str]:
        """列出某 session 的所有卸载 key。"""
        session_dir = self._session_dir(session_id)
        if not session_dir.exists():
            return []

        keys: list[str] = []
        for f in session_dir.iterdir():
            if f.suffix == ".json":
                try:
                    raw = await asyncio.to_thread(f.read_text, "utf-8")
                    data = json.loads(raw)
                    k = data.get("key", "")
                    if k:
                        keys.append(k)
                except Exception:
                    pass
        return sorted(keys)

    def exists(self, key: str) -> bool:
        """检查 key 是否已在缓存中存在。"""
        return key in self._cache
