"""``StreamStateManager`` —— 流式消息状态管理器。

用于管理每个会话轮次的流式消息缓冲区，支持：
- 实时追加 chunk（add_chunk）
- 重连时回放历史 chunk（get_chunks_since）
- 流结束时持久化到 JSONL 文件
- 获取历史流数据（get_stream_history）
- 定期清理过期流（cleanup_old_streams）
- 按 Agent（主/子）隔离流通道

并发安全：每个 stream 独立使用 asyncio.Lock。
持久化：
  - 主 Agent：``{data_dir}/streams/{ps_id}/{widget_id}/{request_id}.jsonl``
  - 子 Agent：``{data_dir}/streams/{ps_id}/{widget_id}/{request_id}/{subagent_id}.jsonl``
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

from ..logging import get_running_log

_rlog = get_running_log()

# 内部保留字 sentinel：主 Agent 始终使用此标识
MAIN_AGENT = "_main"

# key = (ps_id, widget_id, request_id, subagent_id)
StreamKey = tuple[str, str, str, str]

# 兼容旧版 3 元组 key（仅用于 get_stream_history 遍历磁盘文件时查找已有内存 key）
_LegacyKey = tuple[str, str, str]


@dataclass
class _StreamSession:
    """单个流式对话轮次的内部状态。"""

    # 按序列号存储的 chunk 列表：[(seq, chunk_dict), ...]
    chunks: list[tuple[int, dict[str, Any]]] = field(default_factory=list)
    # 当前序列号（自增）
    seq: int = 0
    # 流是否已结束
    finished: bool = False
    # 流开始时间（Unix timestamp）
    started_at: float = field(default_factory=time.monotonic)
    # 并发锁
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class StreamStateManager:
    """流式消息状态管理器。

    生命周期由 PSGateway 持有（与 PSGateway 同进程、同生命周期）。
    每个会话轮次（ps_id + widget_id + request_id）对应一个 StreamSession。

    Usage::

        ssm = StreamStateManager(data_dir="/path/to/data")

        # 流开始
        await ssm.start_stream(ps_id, widget_id, request_id)

        # 每收到一个 chunk 追加
        await ssm.add_chunk(ps_id, widget_id, request_id, chunk_dict)

        # 重连：获取 last_seq 之后的全部 chunk
        chunks, current_seq = await ssm.get_chunks_since(
            ps_id, widget_id, request_id, last_seq=3
        )

        # 流结束（自动持久化）
        await ssm.end_stream(ps_id, widget_id, request_id)

        # 获取历史流数据（用于刷新/新连接）
        streams = await ssm.get_stream_history(ps_id, widget_id)

        # 定期清理
        await ssm.cleanup_old_streams(max_age_seconds=300)
    """

    def __init__(self, data_dir: str | None = None) -> None:
        self._streams: dict[StreamKey, _StreamSession] = {}
        self._data_dir = data_dir

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    async def start_stream(
        self,
        ps_id: str,
        widget_id: str,
        request_id: str,
        subagent_id: str = MAIN_AGENT,
    ) -> None:
        """初始化一个新的流缓冲区。

        幂等：同一 key 重复调用会清空已有缓冲区重新开始。
        """
        key = self._make_key(ps_id, widget_id, request_id, subagent_id)
        session = _StreamSession()
        async with session.lock:
            self._streams[key] = session
        _rlog.debug("core_service", f"[StreamState] start stream: {key}")

    async def add_chunk(
        self,
        ps_id: str,
        widget_id: str,
        request_id: str,
        chunk: dict[str, Any],
        subagent_id: str = MAIN_AGENT,
    ) -> int:
        """追加一个 chunk 到流缓冲区，返回新序列号。

        如果该 key 的流不存在，自动创建（兼容未显式调用 start_stream 的场景）。
        序列号从 1 开始自增。

        Returns:
            该 chunk 的序列号（从 1 开始）。
        """
        key = self._make_key(ps_id, widget_id, request_id, subagent_id)
        session = self._streams.get(key)
        if session is None:
            session = _StreamSession()
            self._streams[key] = session

        async with session.lock:
            session.seq += 1
            session.chunks.append((session.seq, chunk))
            return session.seq

    async def get_chunks_since(
        self,
        ps_id: str,
        widget_id: str,
        request_id: str,
        last_seq: int = 0,
        subagent_id: str = MAIN_AGENT,
    ) -> tuple[list[dict[str, Any]], int, bool]:
        """获取指定序列号之后的所有 chunk。

        Args:
            ps_id: PersonalSpace ID
            widget_id: Widget ID
            request_id: 请求 ID（唯一标识一次用户对话轮次）
            last_seq: 客户端上次收到的最后序列号（0 表示从头开始）
            subagent_id: Agent 标识（MAIN_AGENT=主 Agent）

        Returns:
            (chunks, current_seq, finished)：
            - chunks: last_seq 之后的所有 chunk dict 列表
            - current_seq: 当前最新序列号（客户端可用于下次请求）
            - finished: 流是否已结束
        """
        key = self._make_key(ps_id, widget_id, request_id, subagent_id)
        session = self._streams.get(key)
        if session is None:
            # 尝试从持久化文件中读取
            return await self._load_chunks_from_file(ps_id, widget_id, request_id, last_seq, subagent_id)

        async with session.lock:
            result = [
                chunk_dict
                for seq, chunk_dict in session.chunks
                if seq > last_seq
            ]
            return (result, session.seq, session.finished)

    async def get_stream_state(
        self,
        ps_id: str,
        widget_id: str,
        request_id: str,
        subagent_id: str = MAIN_AGENT,
    ) -> tuple[int, bool]:
        """获取流的当前状态（不返回 chunk 数据）。

        Returns:
            (current_seq, finished)
        """
        key = self._make_key(ps_id, widget_id, request_id, subagent_id)
        session = self._streams.get(key)
        if session is None:
            # 检查持久化文件是否存在
            if self._data_dir:
                filepath = self._stream_filepath(ps_id, widget_id, request_id, subagent_id)
                if os.path.exists(filepath):
                    # 已持久化的流视为已完成
                    return (0, True)
            return (0, True)
        return (session.seq, session.finished)

    async def end_stream(
        self,
        ps_id: str,
        widget_id: str,
        request_id: str,
        subagent_id: str = MAIN_AGENT,
    ) -> None:
        """标记流已结束，并将数据持久化到 JSONL 文件。

        缓冲数据保留，供后续 get_chunks_since 查询。
        实际内存清理由 cleanup_old_streams 定期执行。
        """
        key = self._make_key(ps_id, widget_id, request_id, subagent_id)
        session = self._streams.get(key)
        if session is None:
            return

        async with session.lock:
            session.finished = True
            # 持久化到文件
            if self._data_dir:
                await self._persist_to_file(ps_id, widget_id, request_id, session, subagent_id)

        _rlog.debug("core_service", f"[StreamState] end stream: {key} (seq={session.seq})")

    async def get_stream_history(
        self,
        ps_id: str,
        widget_id: str,
    ) -> list[dict[str, Any]]:
        """获取指定 widget 下所有流的历史数据。

        包括内存中未结束的流和已持久化到文件的流。
        用于前端刷新时重建流状态。

        Returns:
            [{request_id, subagent_id, status,
              chunks: [{seq, chunk_type, content, ...}]}, ...]
        """
        result: list[dict[str, Any]] = []

        # 1. 内存中活跃的流
        for (p_id, w_id, r_id, s_id), session in self._streams.items():
            if p_id != ps_id or w_id != widget_id:
                continue
            async with session.lock:
                entry: dict[str, Any] = {
                    "request_id": r_id,
                    "status": "active" if not session.finished else "finished",
                    "chunks": [
                        {**chunk_dict, "seq": seq}
                        for seq, chunk_dict in session.chunks
                    ],
                }
                if s_id != MAIN_AGENT:
                    entry["subagent_id"] = s_id
                result.append(entry)

        # 2. 磁盘上已持久化的流
        if self._data_dir:
            streams_dir = os.path.join(
                self._data_dir, "streams", ps_id, widget_id
            )
            if os.path.isdir(streams_dir):
                # 2a. 旧格式：扁平 .jsonl 文件（主 Agent）
                for fname in os.listdir(streams_dir):
                    if fname.endswith(".jsonl"):
                        r_id = fname[:-6]  # strip .jsonl
                        # 跳过已在内存中的（按 request_id 去重）
                        if any(s["request_id"] == r_id for s in result):
                            continue
                        try:
                            chunks = self._read_jsonl_file(
                                os.path.join(streams_dir, fname)
                            )
                            result.append({
                                "request_id": r_id,
                                "status": "finished",
                                "chunks": chunks,
                            })
                        except Exception:
                            pass

                # 2b. 新格式：子目录（子 Agent）
                for fname in os.listdir(streams_dir):
                    sub_dir = os.path.join(streams_dir, fname)
                    if not os.path.isdir(sub_dir):
                        continue
                    # fname = request_id (directory)
                    r_id = fname
                    for sf in os.listdir(sub_dir):
                        if not sf.endswith(".jsonl"):
                            continue
                        s_id = sf[:-6]  # strip .jsonl
                        full_key = (ps_id, widget_id, r_id, s_id)
                        if any(
                            s.get("request_id") == r_id and s.get("subagent_id") == s_id
                            for s in result
                        ):
                            continue
                        try:
                            chunks = self._read_jsonl_file(
                                os.path.join(sub_dir, sf)
                            )
                            result.append({
                                "request_id": r_id,
                                "subagent_id": s_id,
                                "status": "finished",
                                "chunks": chunks,
                            })
                        except Exception:
                            pass

        return result

    async def list_agents_for_request(
        self,
        ps_id: str,
        widget_id: str,
        request_id: str,
    ) -> list[dict[str, Any]]:
        """列出指定 request 下所有 Agent 的流信息。

        用于前端 AgentTreePanel 发现有哪些子 Agent 流。

        Returns:
            [{subagent_id, finished, seq}, ...]
            主 Agent 永远排在第一项（subagent_id=None 或省略字段）。
        """
        result: list[dict[str, Any]] = []
        seen: set[str] = set()

        # 1. 内存
        for (p_id, w_id, r_id, s_id), session in self._streams.items():
            if p_id != ps_id or w_id != widget_id or r_id != request_id:
                continue
            async with session.lock:
                entry: dict[str, Any] = {
                    "finished": session.finished,
                    "seq": session.seq,
                }
                if s_id != MAIN_AGENT:
                    entry["subagent_id"] = s_id
                result.append(entry)
                seen.add(s_id)

        # 2. 磁盘
        if self._data_dir and MAIN_AGENT not in seen:
            # 检查主 Agent 旧文件
            filepath = self._stream_filepath(ps_id, widget_id, request_id, MAIN_AGENT)
            dir_path = os.path.dirname(filepath)
            # 旧扁平路径
            flat_path = os.path.join(dir_path, f"{request_id}.jsonl")
            if os.path.exists(flat_path):
                result.append({"finished": True, "seq": 0})
                seen.add(MAIN_AGENT)

        # 2b. 子 Agent 子目录
        if self._data_dir:
            sub_dir = os.path.join(
                self._data_dir, "streams", ps_id, widget_id, request_id
            )
            if os.path.isdir(sub_dir):
                for sf in os.listdir(sub_dir):
                    if sf.endswith(".jsonl"):
                        s_id = sf[:-6]
                        if s_id not in seen:
                            result.append({
                                "subagent_id": s_id,
                                "finished": True,
                                "seq": 0,
                            })

        return result

    async def get_active_streams(
        self,
        ps_id: str,
    ) -> list[dict[str, Any]]:
        """获取指定 PS 下所有活跃（未结束）的流信息。

        用于客户端重连时发现需要恢复哪些流。

        Returns:
            [{widget_id, request_id, subagent_id?, seq, finished, started_at}, ...]
        """
        result: list[dict[str, Any]] = []
        for (p_id, w_id, r_id, s_id), session in self._streams.items():
            if p_id != ps_id:
                continue
            async with session.lock:
                entry: dict[str, Any] = {
                    "widget_id": w_id,
                    "request_id": r_id,
                    "seq": session.seq,
                    "finished": session.finished,
                    "started_at": session.started_at,
                }
                if s_id != MAIN_AGENT:
                    entry["subagent_id"] = s_id
                result.append(entry)
        return result

    async def cleanup_old_streams(self, max_age_seconds: float = 300.0) -> int:
        """清理已结束且超过 max_age_seconds 的流缓冲区。

        Args:
            max_age_seconds: 最大保留时间（秒），默认 300 秒（5 分钟）

        Returns:
            被清理的流数量
        """
        now = time.monotonic()
        to_remove: list[StreamKey] = []

        for key, session in self._streams.items():
            async with session.lock:
                if session.finished and (now - session.started_at) > max_age_seconds:
                    to_remove.append(key)

        for key in to_remove:
            del self._streams[key]

        if to_remove:
            _rlog.debug(
                "core_service",
                f"[StreamState] cleanup: removed {len(to_remove)} streams"
            )
        return len(to_remove)

    async def delete_stream(
        self,
        ps_id: str,
        widget_id: str,
        request_id: str,
        subagent_id: str | None = None,
    ) -> bool:
        """删除指定流的缓冲区（内存）及对应的 JSONL 持久化文件。

        Args:
            subagent_id: None 表示删除该 request 下所有 Agent 流（级联删除）。
                        指定值则仅删除对应 Agent 的流。

        幂等：若流不存在，仅尝试删除磁盘文件后返回 False。

        Returns:
            True 表示内存中存在该流并已删除；False 表示不存在。
        """
        if subagent_id is None:
            # ── 级联：删除该 request 下所有 Agent ──
            to_delete: list[tuple[StreamKey, _StreamSession]] = []
            for key, session in list(self._streams.items()):
                p_id, w_id, r_id, _s_id = key
                if p_id == ps_id and w_id == widget_id and r_id == request_id:
                    to_delete.append((key, session))

            found = False
            for key, session in to_delete:
                async with session.lock:
                    del self._streams[key]
                found = True
                _s_id = key[3]
                _rlog.debug("core_service", f"[StreamState] delete stream (cascade): {key}")
                # 逐个删除磁盘文件
                if self._data_dir:
                    filepath = self._stream_filepath(ps_id, widget_id, request_id, _s_id)
                    self._remove_file_if_exists(filepath)

            # 尝试删除空子目录 + 旧扁平文件
            if self._data_dir:
                flat_path = os.path.join(
                    self._data_dir or "", "streams", ps_id, widget_id,
                    f"{request_id}.jsonl",
                )
                self._remove_file_if_exists(flat_path)
                # 子目录 cleanup
                sub_dir = os.path.join(
                    self._data_dir or "", "streams", ps_id, widget_id, request_id
                )
                if os.path.isdir(sub_dir):
                    try:
                        os.rmdir(sub_dir)
                    except OSError:
                        pass
            return found

        # ── 单个 Agent 删除 ──
        key = self._make_key(ps_id, widget_id, request_id, subagent_id)
        session = self._streams.get(key)
        if session is not None:
            async with session.lock:
                del self._streams[key]
            _rlog.debug("core_service", f"[StreamState] delete stream: {key}")
        # 无论内存是否存在，都尝试删除磁盘 JSONL 文件
        if self._data_dir:
            filepath = self._stream_filepath(ps_id, widget_id, request_id, subagent_id)
            self._remove_file_if_exists(filepath)
        return session is not None

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _stream_filepath(
        self, ps_id: str, widget_id: str, request_id: str,
        subagent_id: str = MAIN_AGENT,
    ) -> str:
        """获取 stream JSONL 文件的路径。

        - 主 Agent: streams/{ps_id}/{widget_id}/{request_id}.jsonl（旧格式兼容）
        - 子 Agent: streams/{ps_id}/{widget_id}/{request_id}/{subagent_id}.jsonl
        """
        base = os.path.join(self._data_dir or "", "streams", ps_id, widget_id)
        if subagent_id == MAIN_AGENT:
            return os.path.join(base, f"{request_id}.jsonl")
        else:
            return os.path.join(base, request_id, f"{subagent_id}.jsonl")

    async def _persist_to_file(
        self,
        ps_id: str,
        widget_id: str,
        request_id: str,
        session: _StreamSession,
        subagent_id: str = MAIN_AGENT,
    ) -> None:
        """将 stream session 的 chunks 写入 JSONL 文件。"""
        try:
            filepath = self._stream_filepath(ps_id, widget_id, request_id, subagent_id)
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            with open(filepath, "w", encoding="utf-8") as f:
                for seq, chunk_dict in session.chunks:
                    record = {**chunk_dict, "_seq": seq}
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
            _rlog.debug(
                "core_service",
                f"[StreamState] persisted stream: {filepath} ({len(session.chunks)} chunks)"
            )
        except Exception:
            _rlog.exception("core_service", "[StreamState] persist 失败")

    def _read_jsonl_file(self, filepath: str) -> list[dict[str, Any]]:
        """从 JSONL 文件读取所有记录。"""
        records = []
        with open(filepath, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return records

    async def _load_chunks_from_file(
        self,
        ps_id: str,
        widget_id: str,
        request_id: str,
        last_seq: int = 0,
        subagent_id: str = MAIN_AGENT,
    ) -> tuple[list[dict[str, Any]], int, bool]:
        """从持久化 JSONL 文件加载 chunk 数据。"""
        if not self._data_dir:
            return ([], 0, True)

        filepath = self._stream_filepath(ps_id, widget_id, request_id, subagent_id)
        # 向后兼容：子 Agent 文件也可能以旧扁平路径存在（迁移前数据）
        if subagent_id != MAIN_AGENT and not os.path.exists(filepath):
            # 尝试旧路径作为 fallback（极少情况）
            fallback = self._stream_filepath(ps_id, widget_id, request_id, MAIN_AGENT)
            if os.path.exists(fallback):
                filepath = fallback

        if not os.path.exists(filepath):
            return ([], 0, True)

        try:
            records = self._read_jsonl_file(filepath)
            max_seq = 0
            result = []
            for rec in records:
                seq = rec.pop("_seq", 0)
                if seq > last_seq:
                    result.append(rec)
                if seq > max_seq:
                    max_seq = seq
            return (result, max_seq, True)  # 磁盘上的都是已完成的
        except Exception:
            return ([], 0, True)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _make_key(
        ps_id: str, widget_id: str, request_id: str,
        subagent_id: str = MAIN_AGENT,
    ) -> StreamKey:
        return (ps_id, widget_id, request_id, subagent_id)

    @staticmethod
    def _remove_file_if_exists(filepath: str) -> None:
        """尝试删除文件，不存在或失败均静默。"""
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
                _rlog.debug(
                    "core_service",
                    f"[StreamState] deleted persisted file: {filepath}",
                )
        except OSError:
            _rlog.exception("core_service", "[StreamState] delete file 失败")
