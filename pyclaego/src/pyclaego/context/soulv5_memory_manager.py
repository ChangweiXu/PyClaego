"""SoulV5MemoryManager — 多层记忆系统的文件树 + SQLite 索引管理器（单例）

负责管理 MD 文件树（groups/cases/experiences/topics/preferences）以及
SQLite 索引数据库（nodes/edges/content_fts）。

所有写操作通过 asyncio.Lock 保护；读操作不加锁。
LLM 驱动的总结任务通过 SecurityHandler.request_llm_call_v3 执行。

典型配置（context_global.soul_v5_memory）：
    md_root: @{pyclaego.root_path}/.memory/soul_v5
    db_path: @{pyclaego.root_path}/.memory/soul_v5/_index.db
    llm_id: kimi_summary
    token_budget:
      context_window_cap: 65536
      preference_cap: 2000
      experience_inject_cap: 8000
      topic_index_cap: 1500
    compression:
      auto_group_threshold: 8
      auto_case_threshold: 4
      cluster_token_budget: 12000
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite
import yaml

from ..config import PYCLAEGO_DEFAULT_ROOT, get_config
from ..logging import get_running_log
from .token_counter import TokenCounter

_rlog = get_running_log()

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_DIR_GROUPS = "groups"
_DIR_CASES = "cases"
_DIR_EXPERIENCES = "experiences"
_DIR_TOPICS = "topics"
_DIR_PREFERENCES = "preferences"
_DIR_PROJECTS = "preferences/projects"
_DIR_ARCHIVE = "_archive"

_DOC_TYPE_GROUP = "group"
_DOC_TYPE_CASE = "case"
_DOC_TYPE_EXPERIENCE = "experience"
_DOC_TYPE_TOPIC = "topic"
_DOC_TYPE_PREFERENCE = "preference"

_EDGE_INDEXES_CASE = "indexes_case"
_EDGE_INDEXES_EXPERIENCE = "indexes_experience"
_EDGE_REFERENCES_CASE = "references_case"
_EDGE_REFERENCES_GROUP = "references_group"

_VALID_EDGE_TYPES = frozenset({
    _EDGE_INDEXES_CASE,
    _EDGE_INDEXES_EXPERIENCE,
    _EDGE_REFERENCES_CASE,
    _EDGE_REFERENCES_GROUP,
})

# Front matter YAML 分隔符
_FM_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# Slug 清理
_SLUG_RE = re.compile(r"[^\w\u4e00-\u9fff-]+")

# 每个 topic 最多保留的活跃 experience 数量（默认值，可由配置覆盖）
_MAX_EXPS_PER_TOPIC_DEFAULT = 5

# 压缩路径文本截断阈值（字符数）
# 超过此长度的纯文本内容将保留头尾片段，其余替换为截断标记
_COMPRESS_TEXT_TRUNC_CHARS: int = 2000
_COMPRESS_TEXT_HEAD_CHARS: int = 500
_COMPRESS_TEXT_TAIL_CHARS: int = 200

# SQL Schema
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS nodes (
    md_path      TEXT PRIMARY KEY,
    doc_type     TEXT NOT NULL,
    title        TEXT,
    tags         TEXT,
    status       TEXT DEFAULT 'current',
    created_at   TEXT,
    modified_at  TEXT,
    case_indexed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS edges (
    from_path   TEXT NOT NULL,
    to_path     TEXT NOT NULL,
    edge_type   TEXT NOT NULL,
    PRIMARY KEY (from_path, to_path, edge_type)
);

CREATE VIRTUAL TABLE IF NOT EXISTS content_fts USING fts5(
    md_path,
    content,
    tokenize='unicode61 remove_diacritics 2'
);
"""


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass
class SearchResult:
    """FTS 搜索结果"""
    md_path: str
    doc_type: str
    title: str
    tags: list[str]
    snippet: str
    rank: float


@dataclass
class TopicSummary:
    """话题概要"""
    topic_slug: str
    title: str
    case_count: int
    experience_count: int
    modified_at: str


# ---------------------------------------------------------------------------
# SoulV5MemoryManager
# ---------------------------------------------------------------------------

class SoulV5MemoryManager:
    """多层记忆文件树 + SQLite 索引管理器（单例）

    读操作不加锁；所有写操作（文件 + DB 同步）通过 ``_write_lock`` 串行化。
    """

    _instance: SoulV5MemoryManager | None = None

    def __new__(cls) -> SoulV5MemoryManager:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return

        config = get_config()
        mem_config: dict[str, Any] = config.get("context_global", {}).get(
            "soul_v5_memory", {}
        )

        # 路径
        default_root = os.path.join(
            config.get("pyclaego", {}).get("root_path", PYCLAEGO_DEFAULT_ROOT),
            ".memory", "soul_v5",
        )
        self.md_root = Path(mem_config.get("md_root", default_root)).expanduser()
        self.db_path = Path(
            mem_config.get("db_path", str(self.md_root / "_index.db"))
        ).expanduser()

        # LLM
        self.llm_id: str = mem_config.get("llm_id", "")

        # Token 预算
        budget = mem_config.get("token_budget", {})
        self.context_window_cap: int = budget.get("context_window_cap", 65536)
        self.preference_cap: int = budget.get("preference_cap", 2000)
        self.experience_inject_cap: int = budget.get("experience_inject_cap", 8000)
        self.topic_index_cap: int = budget.get("topic_index_cap", 1500)

        # 工具输出截断（加载历史时生效，不影响磁盘文件）
        self.tool_content_truncation_threshold: int = budget.get(
            "tool_content_truncation_threshold", 2000
        )  # token 上限
        self.tool_content_head_chars: int = budget.get(
            "tool_content_head_chars", 1000
        )  # 保留头部字符数
        self.tool_content_tail_chars: int = budget.get(
            "tool_content_tail_chars", 500
        )  # 保留尾部字符数

        # 压缩配置
        compress = mem_config.get("compression", {})
        self.auto_group_threshold: int = compress.get("auto_group_threshold", 8)
        self.group_compress_stop_at: int = compress.get(
            "group_compress_stop_at", max(1, self.auto_group_threshold // 2)
        )
        self.auto_case_threshold: int = compress.get("auto_case_threshold", 4)
        self.cluster_token_budget: int = compress.get("cluster_token_budget", 12_000)
        self.max_exps_per_topic: int = compress.get(
            "max_exps_per_topic", _MAX_EXPS_PER_TOPIC_DEFAULT
        )

        # 记忆召回配置
        self.recall_config: dict[str, Any] = mem_config.get("memory_recall", {})

        # Token 计数器
        tiktoken_model = mem_config.get("tiktoken_model", "gpt-4")
        self.token_counter = TokenCounter(model=tiktoken_model)

        # 运行时状态
        self._db: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()
        self._db_ready = False

        # 每会话压缩锁：compression 期间持有，handle_before_loop 等待
        self._session_compress_locks: dict[str, asyncio.Lock] = {}

        self._initialized = True
        _rlog.info(
            "soulv5_memory",
            f"[SoulV5MemoryManager] 单例初始化完成 "
            f"(md_root={self.md_root}, db_path={self.db_path})",
        )

    @classmethod
    def get_instance(cls) -> SoulV5MemoryManager:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    def ensure_directory_structure(self) -> None:
        """创建 MD 文件树目录结构（幂等）"""
        for subdir in (
            _DIR_GROUPS,
            _DIR_CASES,
            _DIR_EXPERIENCES,
            _DIR_TOPICS,
            _DIR_PREFERENCES,
            _DIR_PROJECTS,
            _DIR_ARCHIVE,
        ):
            (self.md_root / subdir).mkdir(parents=True, exist_ok=True)
        _rlog.info("soulv5_memory", "[SoulV5MemoryManager] 目录结构已就绪")

    async def ensure_db(self) -> None:
        """延迟初始化 SQLite 数据库（WAL 模式 + FTS5）"""
        if self._db_ready and self._db is not None:
            return

        self.ensure_directory_structure()
        need_rebuild = not self.db_path.exists()

        self._db = await aiosqlite.connect(str(self.db_path))
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(_SCHEMA_SQL)
        await self._db.commit()
        self._db_ready = True

        # 迁移：为旧版 DB 补充 case_indexed 列（SQLite 不支持 IF NOT EXISTS，捕获异常）
        try:
            await self._db.execute(
                "ALTER TABLE nodes ADD COLUMN case_indexed INTEGER NOT NULL DEFAULT 0"
            )
            await self._db.commit()
            _rlog.info("soulv5_memory", "[SoulV5MemoryManager] 已添加 case_indexed 列")
        except Exception:
            pass  # 列已存在，忽略

        # 迁移：将已被 experience 引用的 case 标记为已索引
        await self._db.execute(
            """
            UPDATE nodes SET case_indexed = 1
            WHERE doc_type = 'case' AND case_indexed = 0
            AND md_path IN (
                SELECT to_path FROM edges WHERE edge_type = 'references_case'
            )
            """
        )
        await self._db.commit()

        if need_rebuild:
            await self.rebuild_index()
            _rlog.info(
                "soulv5_memory",
                "[SoulV5MemoryManager] DB 不存在，已从文件树重建索引",
            )
        else:
            _rlog.info(
                "soulv5_memory",
                "[SoulV5MemoryManager] DB 已连接",
            )

    async def close(self) -> None:
        """关闭数据库连接"""
        if self._db is not None:
            await self._db.close()
            self._db = None
            self._db_ready = False

    # ------------------------------------------------------------------
    # 重建索引
    # ------------------------------------------------------------------

    async def rebuild_index(self) -> dict[str, int]:
        """从 MD 文件树重建全部 3 张表

        Returns:
            {"nodes": N, "edges": N, "content": N}
        """
        await self._ensure_db_connected()
        db = self._db
        assert db is not None

        async with self._write_lock:
            await db.execute("DELETE FROM edges")
            await db.execute("DELETE FROM nodes")
            await db.execute("DELETE FROM content_fts")
            await db.commit()

            node_count = 0
            edge_count = 0
            content_count = 0

            for md_file in self.md_root.rglob("*.md"):
                rel = self._rel_path(md_file)
                if rel.startswith("_"):
                    continue  # skip _archive etc.

                fm, body = self._parse_front_matter(md_file)
                doc_type = self._infer_doc_type(rel)
                if doc_type is None:
                    continue

                # nodes
                await db.execute(
                    "INSERT OR REPLACE INTO nodes "
                    "(md_path, doc_type, title, tags, status, created_at, modified_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        rel,
                        doc_type,
                        fm.get("title", fm.get("topic", "")),
                        json.dumps(fm.get("tags", []), ensure_ascii=False),
                        fm.get("status", "current"),
                        fm.get("created_at", ""),
                        fm.get("modified_at", fm.get("created_at", "")),
                    ),
                )
                node_count += 1

                # content_fts
                full_text = f"{fm.get('title', '')} {' '.join(fm.get('tags', []))} {body}"
                await db.execute(
                    "INSERT OR REPLACE INTO content_fts (md_path, content) VALUES (?, ?)",
                    (rel, full_text),
                )
                content_count += 1

                # edges: 从 topic MD 中解析 case/exp 表格行
                if doc_type == _DOC_TYPE_TOPIC:
                    edges = self._extract_edges_from_topic_body(rel, body)
                    for from_p, to_p, etype in edges:
                        await db.execute(
                            "INSERT OR IGNORE INTO edges (from_path, to_path, edge_type) "
                            "VALUES (?, ?, ?)",
                            (from_p, to_p, etype),
                        )
                        edge_count += 1

            await db.commit()

        _rlog.info(
            "soulv5_memory",
            f"[SoulV5MemoryManager] 索引重建完成: "
            f"nodes={node_count}, edges={edge_count}, content={content_count}",
        )
        return {"nodes": node_count, "edges": edge_count, "content": content_count}

    # ------------------------------------------------------------------
    # 读操作（不加锁）
    # ------------------------------------------------------------------

    async def query(
        self,
        query: str,
        doc_type: str | None = None,
        topic: str | None = None,
        top_k: int = 5,
    ) -> list[SearchResult]:
        """FTS5 全文搜索

        Args:
            query: 搜索词
            doc_type: 可选过滤 (case/experience/topic)
            topic: 可选过滤话题 slug
            top_k: 返回最多 N 条
        """
        await self._ensure_db_connected()
        db = self._db
        assert db is not None

        # 构建 FTS5 查询（简单分词处理）
        fts_query = self._build_fts_query(query)
        if not fts_query:
            return []

        sql = (
            "SELECT n.md_path, n.doc_type, n.title, n.tags, "
            "snippet(content_fts, 1, '<b>', '</b>', '...', 32) AS snip, "
            "rank "
            "FROM content_fts "
            "JOIN nodes n ON n.md_path = content_fts.md_path "
            "WHERE content_fts MATCH ? AND n.status = 'current' "
        )
        params: list[Any] = [fts_query]

        if doc_type:
            sql += "AND n.doc_type = ? "
            params.append(doc_type)

        if topic:
            # 通过 edges 过滤：找到 topic 文件，再找其链接的文件
            topic_path = f"{_DIR_TOPICS}/{self._slugify(topic)}.md"
            sql = (
                "SELECT n.md_path, n.doc_type, n.title, n.tags, "
                "snippet(content_fts, 1, '<b>', '</b>', '...', 32) AS snip, "
                "rank "
                "FROM content_fts "
                "JOIN nodes n ON n.md_path = content_fts.md_path "
                "JOIN edges e ON e.to_path = n.md_path "
                "WHERE content_fts MATCH ? AND n.status = 'current' "
                "AND e.from_path = ? "
            )
            params = [fts_query, topic_path]
            if doc_type:
                sql += "AND n.doc_type = ? "
                params.append(doc_type)

        sql += "ORDER BY rank LIMIT ?"
        params.append(top_k)

        results: list[SearchResult] = []
        async with db.execute(sql, params) as cursor:
            async for row in cursor:
                tags = []
                try:
                    tags = json.loads(row[3]) if row[3] else []
                except (json.JSONDecodeError, TypeError):
                    pass
                results.append(SearchResult(
                    md_path=row[0],
                    doc_type=row[1],
                    title=row[2] or "",
                    tags=tags,
                    snippet=row[4] or "",
                    rank=float(row[5]),
                ))
        return results

    async def read_file(self, file_id: str) -> str | None:
        """读取 MD 文件全文

        Args:
            file_id: md_path（相对路径）或 doc_id（如 C-20260420-103000-a1b2c3d4）
        """
        md_path = await self._resolve_file_id(file_id)
        if md_path is None:
            return None

        abs_path = self.md_root / md_path
        if not abs_path.exists():
            return None

        return abs_path.read_text(encoding="utf-8")

    async def browse_topics(self) -> list[TopicSummary]:
        """列出所有话题及其关联的 case/experience 数量"""
        await self._ensure_db_connected()
        db = self._db
        assert db is not None

        sql = """
            SELECT 
                n.md_path, n.title, n.modified_at,
                (SELECT COUNT(*) FROM edges e WHERE e.from_path = n.md_path AND e.edge_type = ?) AS case_cnt,
                (SELECT COUNT(*) FROM edges e WHERE e.from_path = n.md_path AND e.edge_type = ?) AS exp_cnt
            FROM nodes n
            WHERE n.doc_type = ? AND n.status = 'current'
            ORDER BY n.modified_at DESC
        """
        results: list[TopicSummary] = []
        async with db.execute(
            sql, (_EDGE_INDEXES_CASE, _EDGE_INDEXES_EXPERIENCE, _DOC_TYPE_TOPIC)
        ) as cursor:
            async for row in cursor:
                slug = row[0].replace(f"{_DIR_TOPICS}/", "").replace(".md", "")
                results.append(TopicSummary(
                    topic_slug=slug,
                    title=row[1] or slug,
                    case_count=row[2] or 0,
                    experience_count=row[3] or 0,
                    modified_at=row[4] or "",
                ))
        return results

    async def get_preferences(
        self, session_workspace: Path | None = None
    ) -> dict[str, str]:
        """读取偏好文件

        Returns:
            {"user": str, "project": str}
        """
        user_md = self.md_root / _DIR_PREFERENCES / "USER.md"
        user_content = ""
        if user_md.exists():
            user_content = user_md.read_text(encoding="utf-8")

        project_content = ""
        if session_workspace:
            project_md = self._project_preference_path(session_workspace)
            if project_md.exists():
                project_content = project_md.read_text(encoding="utf-8")

        return {"user": user_content, "project": project_content}

    def _truncate_tool_contents(
        self, messages: list[dict[str, Any]]
    ) -> None:
        """原地截断过长的工具输出内容（仅影响加载到上下文的副本，不影响磁盘）

        遍历 tool_result 类型消息中的每个 tool_results 条目，
        若 content 的 token 数超过 tool_content_truncation_threshold，
        则替换为 head + 截断标记 + tail。
        """
        threshold = self.tool_content_truncation_threshold
        head_chars = self.tool_content_head_chars
        tail_chars = self.tool_content_tail_chars
        min_len = head_chars + tail_chars + 50  # 低于此长度不值得截断

        for msg in messages:
            if msg.get("type") != "tool_result":
                continue
            tool_results = msg.get("tool_results")
            if not tool_results:
                continue
            for tr in tool_results:
                content = tr.get("content", "")
                if not content or len(content) < min_len:
                    continue
                # cheap pre-filter: 1 token ≈ 3 chars(en) / 1.5 chars(cjk)
                if len(content) < threshold * 1.5:
                    continue
                token_count = self.token_counter.count_tokens(content)
                if token_count <= threshold:
                    continue
                tr["content"] = (
                    content[:head_chars]
                    + f"\n\n...[TRUNCATED {token_count} tokens"
                    f" → keeping head {head_chars} chars"
                    f" + tail {tail_chars} chars]...\n\n"
                    + content[-tail_chars:]
                )

    async def load_recent_groups(
        self, session_id: str, token_budget: int
    ) -> list[dict[str, Any]]:
        """贪婪加载最近的 group 消息，在 token 预算内

        Returns:
            按时间升序排列的消息 dict 列表（由最近到最远加载，但返回时正序）
        """
        group_dir = self.md_root / _DIR_GROUPS / session_id
        if not group_dir.exists():
            return []

        # 按文件名倒序（最近优先）
        md_files = sorted(group_dir.glob("*.md"), reverse=True)

        collected: list[list[dict[str, Any]]] = []
        total_tokens = 0

        for md_file in md_files:
            content = md_file.read_text(encoding="utf-8")
            _, body = self._parse_front_matter_str(content)

            # 尝试解析 JSON block
            messages = self._extract_messages_from_body(body)
            if not messages:
                continue

            # 截断过长的工具输出（read-time only，不影响磁盘文件）
            self._truncate_tool_contents(messages)

            msg_tokens = self.token_counter.count_tokens(json.dumps(messages, ensure_ascii=False))
            if total_tokens + msg_tokens > token_budget:
                break

            total_tokens += msg_tokens
            collected.append(messages)

        # 反转为时间正序
        collected.reverse()
        flat: list[dict[str, Any]] = []
        for group_msgs in collected:
            flat.extend(group_msgs)
        return flat

    # ------------------------------------------------------------------
    # 写操作（加锁）
    # ------------------------------------------------------------------

    async def save_group(
        self,
        session_id: str,
        group_id: str,
        messages: list[dict[str, Any]],
    ) -> str:
        """保存一组对话消息（不可变）

        Returns:
            group 的 md_path（相对路径）
        """
        await self._ensure_db_connected()

        session_dir = self.md_root / _DIR_GROUPS / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        filename = f"{group_id}.md"
        abs_path = session_dir / filename
        rel_path = f"{_DIR_GROUPS}/{session_id}/{filename}"

        now = datetime.now()
        fm = {
            "group_id": group_id,
            "session_id": session_id,
            "message_count": len(messages),
            "created_at": now.isoformat(timespec="seconds"),
        }

        # 消息序列化为 JSON block
        body = (
            "```json\n"
            + json.dumps(messages, ensure_ascii=False, indent=2)
            + "\n```\n"
        )

        async with self._write_lock:
            self._write_md_file(abs_path, fm, body)
            await self._sync_node(
                rel_path, _DOC_TYPE_GROUP, group_id, [], "current",
                fm["created_at"], fm["created_at"],
            )
            await self._sync_content(rel_path, body)

        _rlog.info(
            "soulv5_memory",
            f"[SoulV5MemoryManager] 保存 group: {rel_path} ({len(messages)} 条消息)",
        )
        return rel_path

    async def save_case(
        self,
        title: str,
        content: str,
        tags: list[str],
        group_ids: list[str],
        topic: str,
        session_id: str = "",
    ) -> str:
        """创建 case 文件，建立到 group 和 topic 的边

        Returns:
            case_id
        """
        await self._ensure_db_connected()

        case_id = self._generate_id("C")
        filename = f"{case_id}.md"
        abs_path = self.md_root / _DIR_CASES / filename
        rel_path = f"{_DIR_CASES}/{filename}"

        now = datetime.now()
        fm = {
            "case_id": case_id,
            "title": title,
            "tags": tags,
            "created_at": now.isoformat(timespec="seconds"),
            "modified_at": now.isoformat(timespec="seconds"),
            "status": "current",
        }
        body = f"# {title}\n\n{content}\n"

        topic_slug = self._slugify(topic)

        async with self._write_lock:
            # 写文件 + 同步 node 和 content
            self._write_md_file(abs_path, fm, body)
            await self._sync_node(
                rel_path, _DOC_TYPE_CASE, title, tags, "current",
                fm["created_at"], fm["modified_at"],
            )
            await self._sync_content(rel_path, f"{title} {' '.join(tags)} {content}")

            # edges: case → groups
            edges: list[tuple[str, str, str]] = []
            for gid in group_ids:
                group_path = await self._find_group_path(gid, session_id)
                if group_path:
                    edges.append((rel_path, group_path, _EDGE_REFERENCES_GROUP))

            # 确保 topic 存在并添加 topic → case 边
            topic_path = await self._ensure_topic(topic_slug, topic)
            edges.append((topic_path, rel_path, _EDGE_INDEXES_CASE))

            for from_p, to_p, etype in edges:
                await self._db.execute(
                    "INSERT OR IGNORE INTO edges (from_path, to_path, edge_type) "
                    "VALUES (?, ?, ?)",
                    (from_p, to_p, etype),
                )

            # 重新生成 topic MD
            await self._regenerate_topic_md(topic_slug)
            await self._db.commit()

        _rlog.info(
            "soulv5_memory",
            f"[SoulV5MemoryManager] 保存 case: {case_id} (topic={topic_slug}, groups={len(group_ids)})",
        )
        return case_id

    async def save_experience(
        self,
        title: str,
        content: str,
        tags: list[str],
        case_ids: list[str],
        topic: str,
        scope: str = "",
    ) -> str:
        """创建 experience 文件，建立到 case 和 topic 的边

        Returns:
            exp_id
        """
        await self._ensure_db_connected()

        exp_id = self._generate_id("E")
        filename = f"{exp_id}.md"
        abs_path = self.md_root / _DIR_EXPERIENCES / filename
        rel_path = f"{_DIR_EXPERIENCES}/{filename}"

        now = datetime.now()
        fm = {
            "exp_id": exp_id,
            "title": title,
            "tags": tags,
            "scope": scope,
            "created_at": now.isoformat(timespec="seconds"),
            "modified_at": now.isoformat(timespec="seconds"),
            "status": "current",
        }
        body = f"# {title}\n\n{content}\n"

        topic_slug = self._slugify(topic)

        async with self._write_lock:
            self._write_md_file(abs_path, fm, body)
            await self._sync_node(
                rel_path, _DOC_TYPE_EXPERIENCE, title, tags, "current",
                fm["created_at"], fm["modified_at"],
            )
            await self._sync_content(rel_path, f"{title} {scope} {' '.join(tags)} {content}")

            # edges: experience → cases
            edges: list[tuple[str, str, str]] = []
            for cid in case_ids:
                case_path = f"{_DIR_CASES}/{cid}.md"
                edges.append((rel_path, case_path, _EDGE_REFERENCES_CASE))

            # topic → experience
            topic_path = await self._ensure_topic(topic_slug, topic)
            edges.append((topic_path, rel_path, _EDGE_INDEXES_EXPERIENCE))

            for from_p, to_p, etype in edges:
                await self._db.execute(
                    "INSERT OR IGNORE INTO edges (from_path, to_path, edge_type) "
                    "VALUES (?, ?, ?)",
                    (from_p, to_p, etype),
                )

            await self._regenerate_topic_md(topic_slug)
            await self._db.commit()

        _rlog.info(
            "soulv5_memory",
            f"[SoulV5MemoryManager] 保存 experience: {exp_id} (topic={topic_slug}, cases={len(case_ids)})",
        )
        return exp_id

    async def update_case(
        self,
        case_id: str,
        content: str | None = None,
        tags: list[str] | None = None,
    ) -> None:
        """原地更新 case 文件内容和/或 tags"""
        filename = f"{case_id}.md"
        abs_path = self.md_root / _DIR_CASES / filename
        rel_path = f"{_DIR_CASES}/{filename}"

        if not abs_path.exists():
            raise FileNotFoundError(f"Case not found: {case_id}")

        fm, old_body = self._parse_front_matter(abs_path)
        now = datetime.now()
        fm["modified_at"] = now.isoformat(timespec="seconds")

        if tags is not None:
            fm["tags"] = tags
        new_body = f"# {fm.get('title', case_id)}\n\n{content}\n" if content else old_body

        async with self._write_lock:
            self._write_md_file(abs_path, fm, new_body)
            await self._sync_node(
                rel_path, _DOC_TYPE_CASE, fm.get("title", ""),
                fm.get("tags", []), fm.get("status", "current"),
                fm.get("created_at", ""), fm["modified_at"],
            )
            if content:
                await self._sync_content(
                    rel_path,
                    f"{fm.get('title', '')} {' '.join(fm.get('tags', []))} {content}",
                )
            await self._db.commit()

    async def update_experience(
        self,
        exp_id: str,
        content: str | None = None,
        tags: list[str] | None = None,
        scope: str | None = None,
    ) -> None:
        """原地更新 experience 文件"""
        filename = f"{exp_id}.md"
        abs_path = self.md_root / _DIR_EXPERIENCES / filename
        rel_path = f"{_DIR_EXPERIENCES}/{filename}"

        if not abs_path.exists():
            raise FileNotFoundError(f"Experience not found: {exp_id}")

        fm, old_body = self._parse_front_matter(abs_path)
        now = datetime.now()
        fm["modified_at"] = now.isoformat(timespec="seconds")

        if tags is not None:
            fm["tags"] = tags
        if scope is not None:
            fm["scope"] = scope
        new_body = f"# {fm.get('title', exp_id)}\n\n{content}\n" if content else old_body

        async with self._write_lock:
            self._write_md_file(abs_path, fm, new_body)
            await self._sync_node(
                rel_path, _DOC_TYPE_EXPERIENCE, fm.get("title", ""),
                fm.get("tags", []), fm.get("status", "current"),
                fm.get("created_at", ""), fm["modified_at"],
            )
            if content:
                await self._sync_content(
                    rel_path,
                    f"{fm.get('title', '')} {fm.get('scope', '')} "
                    f"{' '.join(fm.get('tags', []))} {content}",
                )
            await self._db.commit()

    async def update_preferences(
        self,
        target: str,
        content: str,
        session_workspace: Path | None = None,
    ) -> None:
        """更新偏好文件（带 token 上限检查）

        Args:
            target: "user" 或 "project"
            content: 新的偏好内容
            session_workspace: 当 target="project" 时必须提供
        """
        token_count = self.token_counter.count_tokens(content)
        if token_count > self.preference_cap:
            raise ValueError(
                f"偏好内容超出 token 上限 "
                f"({token_count} > {self.preference_cap})。请精简内容后重试。"
            )

        if target == "user":
            abs_path = self.md_root / _DIR_PREFERENCES / "USER.md"
            rel_path = f"{_DIR_PREFERENCES}/USER.md"
            fm = {
                "type": "user_preference",
                "modified_at": datetime.now().isoformat(timespec="seconds"),
            }
        elif target == "project":
            if not session_workspace:
                raise ValueError("target='project' 需要提供 session_workspace")
            abs_path = self._project_preference_path(session_workspace)
            rel_path = self._rel_path(abs_path)
            fm = {
                "type": "project_preference",
                "project_path": str(session_workspace),
                "modified_at": datetime.now().isoformat(timespec="seconds"),
            }
        else:
            raise ValueError(f"无效 target: {target}（应为 'user' 或 'project'）")

        async with self._write_lock:
            self._write_md_file(abs_path, fm, content)
            await self._sync_node(
                rel_path, _DOC_TYPE_PREFERENCE, target, [], "current",
                fm["modified_at"], fm["modified_at"],
            )
            await self._sync_content(rel_path, content)
            await self._db.commit()

    async def deprecate(self, md_path: str) -> None:
        """将文件标记为 deprecated

        更新 front matter status + nodes 表 + 从 topic MD 移除
        """
        abs_path = self.md_root / md_path
        if not abs_path.exists():
            raise FileNotFoundError(f"File not found: {md_path}")

        fm, body = self._parse_front_matter(abs_path)
        fm["status"] = "deprecated"
        fm["modified_at"] = datetime.now().isoformat(timespec="seconds")

        async with self._write_lock:
            self._write_md_file(abs_path, fm, body)
            await self._db.execute(
                "UPDATE nodes SET status = 'deprecated', modified_at = ? WHERE md_path = ?",
                (fm["modified_at"], md_path),
            )

            # 找到关联的 topic 并重新生成
            async with self._db.execute(
                "SELECT from_path FROM edges WHERE to_path = ? "
                "AND edge_type IN (?, ?)",
                (md_path, _EDGE_INDEXES_CASE, _EDGE_INDEXES_EXPERIENCE),
            ) as cursor:
                topic_paths = [row[0] async for row in cursor]

            for tp in topic_paths:
                slug = tp.replace(f"{_DIR_TOPICS}/", "").replace(".md", "")
                await self._regenerate_topic_md(slug)

            await self._db.commit()

    # ------------------------------------------------------------------
    # 摘要（LLM 驱动）— Phase 4
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_llm_json(text: str) -> dict[str, Any] | None:
        """从 LLM 输出中解析 JSON 块（```json ... ``` 或裸 JSON）"""
        if not text:
            return None
        # 优先匹配 ```json ... ```
        m = re.search(r"```json\s*\n(.*?)\n```", text, re.DOTALL)
        raw = m.group(1) if m else text.strip()
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, TypeError):
            pass
        return None

    @staticmethod
    def _parse_llm_json_list(text: str) -> list[dict[str, Any]] | None:
        """从 LLM 输出中解析 JSON 数组（```json ... ``` 或裸 JSON array）"""
        if not text:
            return None
        m = re.search(r"```json\s*\n(.*?)\n```", text, re.DOTALL)
        raw = m.group(1) if m else text.strip()
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)]
        except (json.JSONDecodeError, TypeError):
            pass
        return None

    def _build_group_contents_text(
        self, group_md_paths: list[str], char_limit_per_group: int = 4000,
    ) -> str:
        """读取多个 group MD 文件并拼接为文本（用于 LLM 摘要）"""
        parts: list[str] = []
        for md_path in group_md_paths:
            abs_path = self.md_root / md_path
            if not abs_path.exists():
                continue
            raw = abs_path.read_text(encoding="utf-8")
            _, body = self._parse_front_matter_str(raw)
            messages = self._extract_messages_from_body(body)
            if not messages:
                continue
            # 截断工具输出
            self._truncate_tool_contents(messages)
            text = json.dumps(messages, ensure_ascii=False, indent=1)
            if len(text) > char_limit_per_group:
                half = char_limit_per_group // 2
                text = text[:half] + "\n...[TRUNCATED]...\n" + text[-half:]
            group_id = md_path.rsplit("/", 1)[-1].replace(".md", "")
            parts.append(f"### Group: {group_id}\n\n{text}")
        return "\n\n---\n\n".join(parts)

    @staticmethod
    def _sanitize_messages_for_compress(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """深拷贝消息列表，移除压缩路径中会造成 token 爆炸的大体积内容（仅用于压缩路径）。

        处理策略：
        - content_parts 中的 base64 图片/文档数据 → [IMAGE_TRUNCATED] / [DOCUMENT_TRUNCATED]
        - content_parts 中超长文本块 → 头尾片段 + 截断标记
        - 非工具结果消息的 content 字段超长 → 头尾片段 + 截断标记
        - 成功工具结果的 content → [TRUNCATED]
        - 失败工具结果的 content → 保留头 200 字符供 LLM 分析错误模式
        """
        import copy
        msgs = copy.deepcopy(messages)
        for msg in msgs:
            # A. 清理所有消息类型的 content_parts 中的大体积数据
            for part in msg.get("content_parts") or []:
                t = part.get("type")
                if t == "image":
                    if part.get("source_type") == "base64":
                        part["data"] = "[IMAGE_TRUNCATED]"
                elif t == "image_url":
                    url = (part.get("image_url") or {}).get("url", "")
                    if url.startswith("data:"):
                        part["image_url"]["url"] = "[IMAGE_TRUNCATED]"
                elif t == "document":
                    if part.get("source_type") == "base64":
                        part["data"] = "[DOCUMENT_TRUNCATED]"
                elif t == "text":
                    text = part.get("text", "")
                    if len(text) > _COMPRESS_TEXT_TRUNC_CHARS:
                        n = len(text)
                        part["text"] = (
                            text[:_COMPRESS_TEXT_HEAD_CHARS]
                            + f"...[{n} chars TRUNCATED]..."
                            + text[-_COMPRESS_TEXT_TAIL_CHARS:]
                        )

            # B. 截断非工具结果消息中过长的 content 字段（如 PDF 文本注入）
            # assistant 纯文本回复（无 tool_calls）是压缩路径的核心语义信号，完整保留。
            # assistant 工具调用前缀文本（有 tool_calls）通常只是引导语，可截断。
            if msg.get("type") != "tool_result":
                if msg.get("role") == "assistant" and not msg.get("tool_calls"):
                    continue  # assistant 文本回复：保留全文
                content = msg.get("content", "")
                if isinstance(content, str) and len(content) > _COMPRESS_TEXT_TRUNC_CHARS:
                    n = len(content)
                    msg["content"] = (
                        content[:_COMPRESS_TEXT_HEAD_CHARS]
                        + f"...[{n} chars TRUNCATED]..."
                        + content[-_COMPRESS_TEXT_TAIL_CHARS:]
                    )
                continue

            # C. 截断工具结果内容
            for tr in msg.get("tool_results", []):
                content = tr.get("content", "")
                try:
                    parsed = json.loads(content)
                    is_success = parsed.get("status") == "success"
                except (json.JSONDecodeError, AttributeError, TypeError):
                    is_success = False  # 无法判断则保留
                if is_success:
                    tr["content"] = "[TRUNCATED]"
                else:
                    tr["content"] = content[:200] + "...[TRUNCATED]"  # 保留部分原内容供分析
        return msgs

    def _build_group_text_for_compress(self, md_path: str) -> str:
        """读取单个 group MD 文件，清理大体积内容后返回格式化文本。

        通过 _sanitize_messages_for_compress 移除 base64 图片/文档数据及超长文本，
        确保输出体积可控，供压缩路径的 token 计数与 cluster 拼接使用。
        """
        abs_path = self.md_root / md_path
        if not abs_path.exists():
            return ""
        raw = abs_path.read_text(encoding="utf-8")
        _, body = self._parse_front_matter_str(raw)
        messages = self._extract_messages_from_body(body)
        if not messages:
            return ""
        messages = self._sanitize_messages_for_compress(messages)
        group_id = md_path.rsplit("/", 1)[-1].replace(".md", "")
        text = json.dumps(messages, ensure_ascii=False, indent=1)
        return f"### Group: {group_id}\n\n{text}"

    def _build_case_contents_text(self, case_ids: list[str]) -> str:
        """读取多个 case MD 文件并拼接为文本"""
        parts: list[str] = []
        for cid in case_ids:
            abs_path = self.md_root / _DIR_CASES / f"{cid}.md"
            if not abs_path.exists():
                continue
            content = abs_path.read_text(encoding="utf-8")
            parts.append(f"### Case: {cid}\n\n{content}")
        return "\n\n---\n\n".join(parts)

    async def _call_llm(
        self,
        session_task_handler: Any,
        system: str,
        user_text: str,
        max_tokens: int = 3000,
    ) -> str | None:
        """统一的 LLM 调用入口（通过 SecurityHandler）

        Returns:
            LLM 回复文本，失败返回 None
        """
        from ..llm import UnifiedMessage
        from ..security_executor.handler import SecurityHandler

        security = SecurityHandler.get_instance()
        result = await security.request_llm_call_v3(
            session_task_handler=session_task_handler,
            llm_id=self.llm_id,
            system=system,
            messages=[UnifiedMessage(role="user", text=user_text)],
            max_tokens=max_tokens,
        )
        if result.get("success") and result.get("v2_response"):
            return result["v2_response"].text
        _rlog.error(
            "soulv5_memory",
            f"[SoulV5MemoryManager] LLM 调用失败: {result.get('error', 'unknown')}",
        )
        return None

    async def synthesize_case(
        self,
        group_ids: list[str],
        topic: str,
        session_id: str,
        session_task_handler: Any = None,
    ) -> tuple[str, list[str]]:
        """从 groups 中用 LLM 总结生成 case

        Args:
            group_ids: group MD 文件的 md_path 列表
            topic: 话题提示（可为空，由 LLM 推断）
            session_id: 会话 ID
            session_task_handler: SessionTaskHandlerV2 实例

        Returns:
            (case_id, covered_md_paths) — case_id 为新创建的 case，
            covered_md_paths 为 LLM 实际覆盖的 group md_path 列表（连续前缀）

        Raises:
            RuntimeError: LLM 调用或解析失败
        """
        if not session_task_handler:
            raise ValueError("synthesize_case 需要 session_task_handler")

        await session_task_handler.log_info(
            f"[SoulV5MemoryManager] synthesize_case 开始: "
            f"groups={len(group_ids)}, topic_hint='{topic or '(LLM推断)'}', session={session_id}"
        )

        from .system_prompts.soulv5_compress import (
            SYNTHESIZE_CASE_SYSTEM,
            SYNTHESIZE_CASE_USER,
        )

        # 构建已有话题提示
        topics = await self.browse_topics()
        if topics:
            names = ", ".join(t.title for t in topics[:20])
            hint = f"已有话题供参考（优先复用）：{names}"
        else:
            hint = "目前没有已有话题。"

        # 提取 group stem（无目录、无后缀），供 LLM 引用
        input_stems = [p.rsplit("/", 1)[-1].replace(".md", "") for p in group_ids]

        system = SYNTHESIZE_CASE_SYSTEM.format(existing_topics_hint=hint)
        parts = [self._build_group_text_for_compress(p) for p in group_ids]
        group_text = "\n\n---\n\n".join(p for p in parts if p)
        group_id_list = "\n".join(f"- {s}" for s in input_stems)
        user_text = SYNTHESIZE_CASE_USER.format(
            group_id_list=group_id_list,
            group_contents=group_text,
        )

        total_chars = len(group_text)
        await session_task_handler.log_info(
            f"[SoulV5MemoryManager] synthesize_case 调用 LLM: "
            f"groups={len(group_ids)}, 内容长度={total_chars} chars"
        )

        llm_reply = await self._call_llm(
            session_task_handler, system, user_text, max_tokens=3000,
        )
        if not llm_reply:
            raise RuntimeError("synthesize_case: LLM 调用失败")

        parsed = self._parse_llm_json(llm_reply)
        if not parsed:
            raise RuntimeError(
                f"synthesize_case: 无法解析 LLM 输出为 JSON\n{llm_reply[:500]}"
            )

        title = parsed.get("title", "未命名案例")
        content = parsed.get("content", llm_reply)
        tags = parsed.get("tags", [])
        inferred_topic = parsed.get("topic", topic or "general")

        await session_task_handler.log_info(
            f"[SoulV5MemoryManager] synthesize_case LLM解析成功: "
            f"title='{title}', topic='{inferred_topic}', tags={tags}"
        )

        # 验证 covered_group_ids：必须是 input_stems 的连续前缀
        covered_stems = self._validate_covered_prefix(
            input_stems=input_stems,
            llm_covered=parsed.get("covered_group_ids") or [],
        )

        case_id = await self.save_case(
            title=title,
            content=content,
            tags=tags if isinstance(tags, list) else [],
            group_ids=covered_stems,
            topic=inferred_topic,
            session_id=session_id,
        )

        # 将 covered stems 还原为 md_path
        stem_to_path = {
            p.rsplit("/", 1)[-1].replace(".md", ""): p for p in group_ids
        }
        covered_md_paths = [stem_to_path[s] for s in covered_stems if s in stem_to_path]

        if len(covered_md_paths) < len(group_ids):
            await session_task_handler.log_info(
                f"[SoulV5MemoryManager] synthesize_case 部分覆盖: "
                f"covered={len(covered_md_paths)}/{len(group_ids)}, "
                f"未覆盖 groups 将在下次迭代处理"
            )

        await session_task_handler.log_info(
            f"[SoulV5MemoryManager] synthesize_case 完成: {case_id} "
            f"(topic={inferred_topic}, covered={len(covered_md_paths)}/{len(group_ids)})"
        )
        return case_id, covered_md_paths

    @staticmethod
    def _validate_covered_prefix(
        input_stems: list[str],
        llm_covered: list[Any],
    ) -> list[str]:
        """将 LLM 返回的 covered_group_ids 验证为 input_stems 的最长连续前缀。

        规则：
        - 过滤掉不在 input_stems 中的 ID（防止幻觉）
        - 从第一个 input stem 开始，取连续匹配的前缀
        - 若结果为空，回退到 [input_stems[0]]（保证至少前进 1 个）
        """
        if not input_stems:
            return []

        valid_set = set(input_stems)
        # 过滤非法 ID，保留字符串类型
        filtered = [str(x) for x in llm_covered if isinstance(x, str) and x in valid_set]

        # 取连续前缀：从 input_stems[0] 开始逐一匹配
        covered: list[str] = []
        filtered_set = set(filtered)
        for stem in input_stems:
            if stem in filtered_set:
                covered.append(stem)
            else:
                break

        # 回退：至少包含第一个 group
        if not covered:
            covered = [input_stems[0]]

        return covered

    async def extract_experience(
        self,
        new_case_ids: list[str],
        topic: str,
        topic_slug: str,
        session_task_handler: Any = None,
    ) -> dict[str, int]:
        """基于新案例增量更新 topic 的 experiences（LLM 决策 create/update/deprecate）

        Args:
            new_case_ids: 未被任何 experience 处理过的 case id 列表
            topic: 话题名称（供 LLM 使用）
            topic_slug: 话题 slug（用于 DB 查询）
            session_task_handler: SessionTaskHandlerV2 实例

        Returns:
            {"created": N, "updated": N, "deprecated": N}

        Raises:
            ValueError: 参数缺失
            RuntimeError: LLM 调用或解析失败
        """
        if not session_task_handler:
            raise ValueError("extract_experience 需要 session_task_handler")

        from .system_prompts.soulv5_compress import (
            UPDATE_EXPERIENCES_SYSTEM,
            UPDATE_EXPERIENCES_USER,
        )

        topic_path = f"{_DIR_TOPICS}/{topic_slug}.md"

        # 1. 获取当前所有活跃 experience
        active_exps = await self.get_active_experiences_for_topic(topic_path)

        # 2. 构建 existing_exps_text
        if active_exps:
            exp_parts = [
                f"### {exp['exp_id']}\n\n{exp['content']}"
                for exp in active_exps
            ]
            existing_exps_text = "\n\n---\n\n".join(exp_parts)
        else:
            existing_exps_text = "（暂无已有经验）"

        # 3. 构建 new_cases_text
        new_cases_text = self._build_case_contents_text(new_case_ids)

        await session_task_handler.log_info(
            f"[SoulV5MemoryManager] extract_experience 开始: "
            f"topic='{topic}', new_cases={len(new_case_ids)}, active_exps={len(active_exps)}"
        )

        # 4. 调用 LLM
        user_text = UPDATE_EXPERIENCES_USER.format(
            topic=topic,
            max_exps=self.max_exps_per_topic,
            exp_count=len(active_exps),
            case_count=len(new_case_ids),
            existing_exps_text=existing_exps_text,
            new_cases_text=new_cases_text,
        )
        llm_reply = await self._call_llm(
            session_task_handler,
            UPDATE_EXPERIENCES_SYSTEM.format(max_exps=self.max_exps_per_topic),
            user_text,
            max_tokens=4000,
        )
        if not llm_reply:
            raise RuntimeError("extract_experience: LLM 调用失败")

        # 5. 解析 JSON 数组
        actions = self._parse_llm_json_list(llm_reply)
        if actions is None:
            raise RuntimeError(
                f"extract_experience: 无法解析 LLM 输出为 JSON 数组\n{llm_reply[:500]}"
            )

        # 6. 派发各操作
        counts = {"created": 0, "updated": 0, "deprecated": 0}
        new_case_md_paths = [f"{_DIR_CASES}/{cid}.md" for cid in new_case_ids]

        for action in actions:
            act = action.get("action", "")
            try:
                if act == "create":
                    await self.save_experience(
                        title=action.get("title", "未命名经验"),
                        content=action.get("content", ""),
                        tags=action.get("tags", []) if isinstance(action.get("tags"), list) else [],
                        case_ids=new_case_ids,
                        topic=topic,
                        scope=action.get("scope", ""),
                    )
                    counts["created"] += 1
                elif act == "update":
                    exp_id = action.get("exp_id", "")
                    if exp_id:
                        await self.update_experience(
                            exp_id=exp_id,
                            content=action.get("content"),
                            tags=action.get("tags") if isinstance(action.get("tags"), list) else None,
                            scope=action.get("scope"),
                        )
                        counts["updated"] += 1
                elif act == "deprecate":
                    exp_id = action.get("exp_id", "")
                    if exp_id:
                        exp_md_path = f"{_DIR_EXPERIENCES}/{exp_id}.md"
                        await self.deprecate(exp_md_path)
                        counts["deprecated"] += 1
                else:
                    _rlog.warning(
                        "soulv5_memory",
                        f"[SoulV5MemoryManager] extract_experience: 未知操作类型 '{act}'，跳过",
                    )
            except Exception as e:
                _rlog.error(
                    "soulv5_memory",
                    f"[SoulV5MemoryManager] extract_experience 操作失败 (action={act}): {e}",
                )

        # 7. cap 检查（警告，不回滚）
        active_after = await self.count_active_experiences_for_topic(topic_path)
        if active_after > self.max_exps_per_topic:
            _rlog.warning(
                "soulv5_memory",
                f"[SoulV5MemoryManager] extract_experience: topic='{topic_slug}' "
                f"active_exps={active_after} 超过上限 {self.max_exps_per_topic}，"
                f"LLM 未能有效合并，建议手动审查",
            )

        # 8. 标记 cases 为已索引（原子性批量更新）
        await self.mark_cases_indexed(new_case_md_paths)

        await session_task_handler.log_info(
            f"[SoulV5MemoryManager] extract_experience 完成: topic='{topic}' "
            f"created={counts['created']}, updated={counts['updated']}, "
            f"deprecated={counts['deprecated']}, active_after={active_after}"
        )
        return counts

    # ------------------------------------------------------------------
    # 每会话压缩锁（辅助）
    # ------------------------------------------------------------------

    def _get_session_compress_lock(self, session_id: str) -> asyncio.Lock:
        """返回（或创建）指定会话的压缩锁。"""
        if session_id not in self._session_compress_locks:
            self._session_compress_locks[session_id] = asyncio.Lock()
        return self._session_compress_locks[session_id]

    def is_session_compressing(self, session_id: str) -> bool:
        """返回 True 当且仅当该会话的压缩锁当前被持有。"""
        lock = self._session_compress_locks.get(session_id)
        return lock is not None and lock.locked()

    async def wait_compression_done(self, session_id: str) -> None:
        """若该会话正在压缩，则等待其完成；否则立即返回。

        原理：尝试获取压缩锁后立即释放，确保 compact_session 已退出。
        """
        lock = self._get_session_compress_lock(session_id)
        if lock.locked():
            async with lock:
                pass  # 获取成功即意味着压缩已结束

    async def compact_session(
        self,
        session_id: str,
        session_task_handler: Any = None,
    ) -> dict[str, Any]:
        """压缩会话：将未索引 groups 提炼为 cases，积累足够 cases 后提炼 experience

        Args:
            session_id: 会话 ID
            session_task_handler: SessionTaskHandlerV2 实例

        Returns:
            {"cases_created": int, "experiences_created": int,
             "groups_indexed": int, "errors": List[str]}
        """
        if not session_task_handler:
            raise ValueError("compact_session 需要 session_task_handler")

        async with self._get_session_compress_lock(session_id):
            return await self._compact_session_locked(session_id, session_task_handler)

    async def _compact_session_locked(
        self,
        session_id: str,
        session_task_handler: Any,
    ) -> dict[str, Any]:
        """compact_session 的实际执行体，调用方必须持有该会话的压缩锁。"""
        unindexed = await self.get_unindexed_group_ids(session_id)
        if not unindexed:
            await session_task_handler.log_info(
                f"[SoulV5MemoryManager] compact_session: 无未索引 groups，跳过压缩 (session={session_id})"
            )
            return {
                "cases_created": 0,
                "experiences_created": 0,
                "groups_indexed": 0,
                "errors": [],
            }

        await session_task_handler.log_info(
            f"[SoulV5MemoryManager] compact_session 开始: "
            f"session={session_id}, 未索引groups={len(unindexed)}, "
            f"auto_group_threshold={self.auto_group_threshold}, "
            f"group_compress_stop_at={self.group_compress_stop_at}, "
            f"cluster_token_budget={self.cluster_token_budget}"
        )

        # 贪婪按 token 预算分 cluster，直到剩余数量低于阈值
        cases_created = 0
        groups_indexed = 0
        errors: list[str] = []
        # topic → [case_id] 用于后续提炼 experience
        topic_cases: dict[str, list[str]] = {}

        remaining = list(unindexed)
        iteration = 0
        while len(remaining) >= self.group_compress_stop_at:
            iteration += 1
            await session_task_handler.log_info(
                f"[SoulV5MemoryManager] compact_session 迭代 #{iteration}: "
                f"remaining={len(remaining)}, cases_created={cases_created}"
            )
            # 构建 cluster：最老的 group 优先，累积 token 不超过预算
            cluster: list[str] = []
            cluster_tokens = 0
            for group_path in remaining:
                text = self._build_group_text_for_compress(group_path)
                group_tokens = self.token_counter.count_tokens(text) if text else 0
                if cluster and cluster_tokens + group_tokens > self.cluster_token_budget:
                    break
                cluster.append(group_path)
                cluster_tokens += group_tokens

            if not cluster:
                break  # 防御性保护，不应发生

            await session_task_handler.log_info(
                f"[SoulV5MemoryManager] compact_session cluster构建完成: "
                f"cluster_size={len(cluster)}, cluster_tokens={cluster_tokens}"
            )

            try:
                case_id, covered = await self.synthesize_case(
                    group_ids=cluster,
                    topic="",  # 由 LLM 推断
                    session_id=session_id,
                    session_task_handler=session_task_handler,
                )
                cases_created += 1
                groups_indexed += len(covered)
                await session_task_handler.log_info(
                    f"[SoulV5MemoryManager] compact_session case创建成功: "
                    f"case_id={case_id}, covered={len(covered)}, "
                    f"累计 cases_created={cases_created}, groups_indexed={groups_indexed}"
                )

                # 读取 case 的 topic
                case_path = f"{_DIR_CASES}/{case_id}.md"
                case_abs = self.md_root / case_path
                if case_abs.exists():
                    fm, _ = self._parse_front_matter(case_abs)
                    # topic 从 edges 反查
                    await self._ensure_db_connected()
                    db = self._db
                    assert db is not None
                    async with db.execute(
                        "SELECT from_path FROM edges "
                        "WHERE to_path = ? AND edge_type = ?",
                        (case_path, _EDGE_INDEXES_CASE),
                    ) as cur:
                        row = await cur.fetchone()
                        if row:
                            topic_slug = row[0].replace(
                                f"{_DIR_TOPICS}/", ""
                            ).replace(".md", "")
                            topic_cases.setdefault(topic_slug, []).append(case_id)

            except Exception as e:
                err = f"cluster {cluster[0]}..{cluster[-1]}: {e}"
                errors.append(err)
                _rlog.error(
                    "soulv5_memory",
                    f"[SoulV5MemoryManager] compact_session 失败: {err}",
                )
                break  # 遇到错误停止压缩，避免级联失败

            remaining = remaining[len(covered):]

        if remaining:
            await session_task_handler.log_info(
                f"[SoulV5MemoryManager] compact_session: "
                f"remaining={len(remaining)} < stop_at={self.group_compress_stop_at}，停止 case 压缩"
            )

        # ──── EXPERIENCE PHASE ────
        # 对本轮产生新 case 的 topic，检查未索引 case 数量是否达到阈值
        experiences_created = 0
        await session_task_handler.log_info(
            f"[SoulV5MemoryManager] compact_session experience阶段: "
            f"待检查 topics={list(topic_cases.keys())}"
        )
        for topic_slug, _new_case_ids in topic_cases.items():
            try:
                topic_path = f"{_DIR_TOPICS}/{topic_slug}.md"

                # 查询该 topic 下尚未被 experience 处理的 case 数量
                unindexed_count = await self.count_unindexed_cases_for_topic(topic_path)

                await session_task_handler.log_info(
                    f"[SoulV5MemoryManager] compact_session topic='{topic_slug}': "
                    f"unindexed_cases={unindexed_count}, threshold={self.auto_case_threshold}, "
                    f"触发提炼={'是' if unindexed_count >= self.auto_case_threshold else '否'}"
                )

                if unindexed_count < self.auto_case_threshold:
                    continue

                # 获取全部未索引 case id（按创建时间升序）
                new_case_ids = await self.get_unindexed_case_ids_for_topic(topic_path)

                # 读取 topic 标题
                topic_abs = self.md_root / topic_path
                topic_title = topic_slug
                if topic_abs.exists():
                    fm, _ = self._parse_front_matter(topic_abs)
                    topic_title = fm.get("topic", topic_slug)

                result = await self.extract_experience(
                    new_case_ids=new_case_ids,
                    topic=topic_title,
                    topic_slug=topic_slug,
                    session_task_handler=session_task_handler,
                )
                experiences_created += result.get("created", 0)

            except Exception as e:
                err = f"experience for topic {topic_slug}: {e}"
                errors.append(err)
                _rlog.error(
                    "soulv5_memory",
                    f"[SoulV5MemoryManager] extract_experience 失败: {err}",
                )

        summary = {
            "cases_created": cases_created,
            "experiences_created": experiences_created,
            "groups_indexed": groups_indexed,
            "errors": errors,
        }
        await session_task_handler.log_info(
            f"[SoulV5MemoryManager] compact_session 完成: {summary}"
        )
        return summary

    # ------------------------------------------------------------------
    # 辅助：查询
    # ------------------------------------------------------------------

    async def count_unindexed_groups(self, session_id: str) -> int:
        """统计尚未被任何 case 引用的 group 数量"""
        await self._ensure_db_connected()
        db = self._db
        assert db is not None

        prefix = f"{_DIR_GROUPS}/{session_id}/"
        sql = """
            SELECT COUNT(*) FROM nodes
            WHERE doc_type = ? AND md_path LIKE ?
            AND md_path NOT IN (
                SELECT to_path FROM edges WHERE edge_type = ?
            )
        """
        async with db.execute(sql, (_DOC_TYPE_GROUP, f"{prefix}%", _EDGE_REFERENCES_GROUP)) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

    async def get_unindexed_group_ids(self, session_id: str) -> list[str]:
        """返回尚未被任何 case 引用的 group 的 md_path 列表"""
        await self._ensure_db_connected()
        db = self._db
        assert db is not None

        prefix = f"{_DIR_GROUPS}/{session_id}/"
        sql = """
            SELECT md_path FROM nodes
            WHERE doc_type = ? AND md_path LIKE ?
            AND md_path NOT IN (
                SELECT to_path FROM edges WHERE edge_type = ?
            )
            ORDER BY md_path ASC
        """
        paths: list[str] = []
        async with db.execute(sql, (_DOC_TYPE_GROUP, f"{prefix}%", _EDGE_REFERENCES_GROUP)) as cur:
            async for row in cur:
                paths.append(row[0])
        return paths

    # ------------------------------------------------------------------
    # 辅助：topic 级 case / experience 查询与标记
    # ------------------------------------------------------------------

    async def count_unindexed_cases_for_topic(self, topic_path: str) -> int:
        """统计 topic 下尚未被 experience 处理过的 case 数量（case_indexed=0）"""
        await self._ensure_db_connected()
        db = self._db
        assert db is not None
        sql = """
            SELECT COUNT(*) FROM edges e
            JOIN nodes n ON n.md_path = e.to_path
            WHERE e.from_path = ? AND e.edge_type = ?
            AND n.status = 'current' AND n.case_indexed = 0
        """
        async with db.execute(sql, (topic_path, _EDGE_INDEXES_CASE)) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

    async def get_unindexed_case_ids_for_topic(self, topic_path: str) -> list[str]:
        """返回 topic 下 case_indexed=0 的 case id 列表（不含目录和扩展名）"""
        await self._ensure_db_connected()
        db = self._db
        assert db is not None
        sql = """
            SELECT n.md_path FROM edges e
            JOIN nodes n ON n.md_path = e.to_path
            WHERE e.from_path = ? AND e.edge_type = ?
            AND n.status = 'current' AND n.case_indexed = 0
            ORDER BY n.created_at ASC
        """
        ids: list[str] = []
        async with db.execute(sql, (topic_path, _EDGE_INDEXES_CASE)) as cur:
            async for row in cur:
                ids.append(
                    row[0].replace(f"{_DIR_CASES}/", "").replace(".md", "")
                )
        return ids

    async def get_active_experiences_for_topic(
        self, topic_path: str
    ) -> list[dict[str, Any]]:
        """返回 topic 下所有 status='current' 的 experience 列表

        Returns:
            [{"exp_id": str, "md_path": str, "content": str}, ...]
        """
        await self._ensure_db_connected()
        db = self._db
        assert db is not None
        sql = """
            SELECT n.md_path FROM edges e
            JOIN nodes n ON n.md_path = e.to_path
            WHERE e.from_path = ? AND e.edge_type = ?
            AND n.status = 'current'
            ORDER BY n.created_at ASC
        """
        results: list[dict[str, Any]] = []
        async with db.execute(sql, (topic_path, _EDGE_INDEXES_EXPERIENCE)) as cur:
            rows = [row[0] async for row in cur]
        for md_path in rows:
            abs_path = self.md_root / md_path
            content = abs_path.read_text(encoding="utf-8") if abs_path.exists() else ""
            exp_id = md_path.replace(f"{_DIR_EXPERIENCES}/", "").replace(".md", "")
            results.append({"exp_id": exp_id, "md_path": md_path, "content": content})
        return results

    async def count_active_experiences_for_topic(self, topic_path: str) -> int:
        """统计 topic 下 status='current' 的 experience 数量"""
        await self._ensure_db_connected()
        db = self._db
        assert db is not None
        sql = """
            SELECT COUNT(*) FROM edges e
            JOIN nodes n ON n.md_path = e.to_path
            WHERE e.from_path = ? AND e.edge_type = ?
            AND n.status = 'current'
        """
        async with db.execute(sql, (topic_path, _EDGE_INDEXES_EXPERIENCE)) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

    async def mark_cases_indexed(self, case_md_paths: list[str]) -> None:
        """将指定 case 的 case_indexed 标记为 1（批量，事务性）"""
        if not case_md_paths:
            return
        await self._ensure_db_connected()
        db = self._db
        assert db is not None
        placeholders = ",".join("?" for _ in case_md_paths)
        async with self._write_lock:
            await db.execute(
                f"UPDATE nodes SET case_indexed = 1 WHERE md_path IN ({placeholders})",
                case_md_paths,
            )
            await db.commit()

    async def ensure_session_groups_indexed(
        self,
        session_id: str,
        history_messages: list[dict[str, Any]] | None = None,
    ) -> int:
        """确保会话的 group 全部在索引中，缺失的自动补录

        两阶段：
        1. 从 history_messages 中提取尚未落盘为 MD 的 group，先 save_group
        2. 扫描磁盘上的 group MD 文件，将 nodes 表缺失的条目补录

        Args:
            session_id: 会话 ID
            history_messages: history_manager.load_all() 的结果（可选）

        Returns:
            新补录的 group 数量
        """
        await self._ensure_db_connected()
        db = self._db
        assert db is not None

        total_synced = 0

        # ── 阶段 1：从 history 消息中提取未落盘的 groups ──
        if history_messages:
            # 按 group_id 分组；无 group_id 的连续消息聚为一组
            grouped: dict[str, list[dict[str, Any]]] = {}
            ungrouped_buf: list[dict[str, Any]] = []
            ungrouped_counter = 0

            for msg in history_messages:
                gid = msg.get("group_id")
                if gid:
                    # 先 flush 累积的无 group_id 消息
                    if ungrouped_buf:
                        ungrouped_counter += 1
                        synthetic_gid = f"g_legacy_{ungrouped_counter:04d}"
                        grouped[synthetic_gid] = ungrouped_buf
                        ungrouped_buf = []
                    grouped.setdefault(gid, []).append(msg)
                else:
                    ungrouped_buf.append(msg)

            # flush 最后的无 group_id 消息
            if ungrouped_buf:
                ungrouped_counter += 1
                synthetic_gid = f"g_legacy_{ungrouped_counter:04d}"
                grouped[synthetic_gid] = ungrouped_buf

            if grouped:
                group_dir = self.md_root / _DIR_GROUPS / session_id
                for gid, msgs in grouped.items():
                    md_file = group_dir / f"{gid}.md"
                    if md_file.exists():
                        continue
                    try:
                        await self.save_group(
                            session_id=session_id,
                            group_id=gid,
                            messages=msgs,
                        )
                        total_synced += 1
                    except Exception as e:
                        _rlog.error(
                            "soulv5_memory",
                            f"[SoulV5MemoryManager] 从 history 补录 group {gid} 失败: {e}",
                        )

        # ── 阶段 2：扫描磁盘，补录 nodes 表缺失的条目 ──
        group_dir = self.md_root / _DIR_GROUPS / session_id
        if not group_dir.exists():
            return total_synced

        disk_paths: list[str] = []
        for md_file in sorted(group_dir.glob("*.md")):
            rel = f"{_DIR_GROUPS}/{session_id}/{md_file.name}"
            disk_paths.append(rel)

        if not disk_paths:
            return total_synced

        placeholders = ",".join("?" for _ in disk_paths)
        sql = f"SELECT md_path FROM nodes WHERE md_path IN ({placeholders})"
        existing: set[str] = set()
        async with db.execute(sql, disk_paths) as cur:
            async for row in cur:
                existing.add(row[0])

        missing = [p for p in disk_paths if p not in existing]
        if missing:
            async with self._write_lock:
                for rel_path in missing:
                    abs_path = self.md_root / rel_path
                    if not abs_path.exists():
                        continue
                    try:
                        fm, body = self._parse_front_matter(abs_path)
                        group_id = fm.get("group_id", abs_path.stem)
                        created_at = fm.get("created_at", "")

                        await self._sync_node(
                            rel_path, _DOC_TYPE_GROUP, group_id, [], "current",
                            created_at, created_at,
                        )
                        await self._sync_content(rel_path, body)
                        total_synced += 1
                    except Exception as e:
                        _rlog.error(
                            "soulv5_memory",
                            f"[SoulV5MemoryManager] 补录 group 失败 {rel_path}: {e}",
                        )
                if missing:
                    await db.commit()

        if total_synced > 0:
            _rlog.info(
                "soulv5_memory",
                f"[SoulV5MemoryManager] 会话 {session_id} 补录了 {total_synced} 个 group 到索引",
            )
        return total_synced

    # ------------------------------------------------------------------
    # 内部：DB 同步
    # ------------------------------------------------------------------

    async def _ensure_db_connected(self) -> None:
        if not self._db_ready or self._db is None:
            await self.ensure_db()

    async def _sync_node(
        self,
        md_path: str,
        doc_type: str,
        title: str,
        tags: list[str],
        status: str,
        created_at: str,
        modified_at: str,
        case_indexed: int = 0,
    ) -> None:
        """upsert nodes 表"""
        db = self._db
        assert db is not None
        await db.execute(
            "INSERT OR REPLACE INTO nodes "
            "(md_path, doc_type, title, tags, status, created_at, modified_at, case_indexed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                md_path, doc_type, title,
                json.dumps(tags, ensure_ascii=False),
                status, created_at, modified_at, case_indexed,
            ),
        )

    async def _sync_content(self, md_path: str, content: str) -> None:
        """upsert content_fts 表"""
        db = self._db
        assert db is not None
        # FTS5 不支持 INSERT OR REPLACE，先删再插
        await db.execute("DELETE FROM content_fts WHERE md_path = ?", (md_path,))
        await db.execute(
            "INSERT INTO content_fts (md_path, content) VALUES (?, ?)",
            (md_path, content),
        )

    async def _sync_edges_for_source(
        self, from_path: str, edges: list[tuple[str, str]]
    ) -> None:
        """替换某 from_path 的全部出边

        Args:
            from_path: 源文件路径
            edges: [(to_path, edge_type), ...]
        """
        db = self._db
        assert db is not None
        await db.execute("DELETE FROM edges WHERE from_path = ?", (from_path,))
        for to_p, etype in edges:
            await db.execute(
                "INSERT OR IGNORE INTO edges (from_path, to_path, edge_type) "
                "VALUES (?, ?, ?)",
                (from_path, to_p, etype),
            )

    # ------------------------------------------------------------------
    # 内部：topic 管理
    # ------------------------------------------------------------------

    async def _ensure_topic(self, topic_slug: str, topic_name: str = "") -> str:
        """确保 topic 文件存在。返回 md_path。

        在 _write_lock 内部调用（调用方已持锁）。
        """
        rel_path = f"{_DIR_TOPICS}/{topic_slug}.md"
        abs_path = self.md_root / rel_path

        if abs_path.exists():
            return rel_path

        now = datetime.now()
        fm = {
            "topic": topic_name or topic_slug,
            "subtopics": [],
            "created_at": now.isoformat(timespec="seconds"),
            "modified_at": now.isoformat(timespec="seconds"),
        }
        body = (
            f"# {topic_name or topic_slug}\n\n"
            "## Cases\n\n"
            "| case_id | title | tags | modified |\n"
            "|---------|-------|------|----------|\n\n"
            "## Experiences\n\n"
            "| exp_id | title | scope | modified |\n"
            "|--------|-------|-------|----------|\n"
        )
        self._write_md_file(abs_path, fm, body)
        await self._sync_node(
            rel_path, _DOC_TYPE_TOPIC, topic_name or topic_slug,
            [], "current", fm["created_at"], fm["modified_at"],
        )
        await self._sync_content(rel_path, f"{topic_name or topic_slug}")

        _rlog.info(
            "soulv5_memory",
            f"[SoulV5MemoryManager] 自动创建 topic: {rel_path}",
        )
        return rel_path

    async def _regenerate_topic_md(self, topic_slug: str) -> None:
        """从 edges 表重建 topic MD 文件内容

        在 _write_lock 内部调用。
        """
        db = self._db
        assert db is not None

        rel_path = f"{_DIR_TOPICS}/{topic_slug}.md"
        abs_path = self.md_root / rel_path
        if not abs_path.exists():
            return

        fm, _ = self._parse_front_matter(abs_path)
        fm["modified_at"] = datetime.now().isoformat(timespec="seconds")

        topic_name = fm.get("topic", topic_slug)

        # 查 cases
        cases_sql = """
            SELECT n.md_path, n.title, n.tags, n.modified_at
            FROM edges e
            JOIN nodes n ON n.md_path = e.to_path
            WHERE e.from_path = ? AND e.edge_type = ? AND n.status = 'current'
            ORDER BY n.modified_at DESC
        """
        case_rows = []
        async with db.execute(cases_sql, (rel_path, _EDGE_INDEXES_CASE)) as cursor:
            async for row in cursor:
                case_rows.append(row)

        # 查 experiences
        exp_rows = []
        async with db.execute(
            cases_sql.replace(_EDGE_INDEXES_CASE, _EDGE_INDEXES_EXPERIENCE),
            (rel_path, _EDGE_INDEXES_EXPERIENCE),
        ) as cursor:
            async for row in cursor:
                exp_rows.append(row)

        # 构建 body
        lines = [f"# {topic_name}\n"]
        lines.append("## Cases\n")
        lines.append("| case_id | title | tags | modified |")
        lines.append("|---------|-------|------|----------|")
        for row in case_rows:
            cid = row[0].replace(f"{_DIR_CASES}/", "").replace(".md", "")
            tags_str = row[2] or "[]"
            lines.append(f"| {cid} | {row[1] or ''} | {tags_str} | {row[3] or ''} |")

        lines.append("\n## Experiences\n")
        lines.append("| exp_id | title | scope | modified |")
        lines.append("|--------|-------|-------|----------|")
        for row in exp_rows:
            eid = row[0].replace(f"{_DIR_EXPERIENCES}/", "").replace(".md", "")
            lines.append(f"| {eid} | {row[1] or ''} | | {row[3] or ''} |")

        body = "\n".join(lines) + "\n"

        self._write_md_file(abs_path, fm, body)
        await self._sync_node(
            rel_path, _DOC_TYPE_TOPIC, topic_name,
            fm.get("tags", []), "current",
            fm.get("created_at", ""), fm["modified_at"],
        )
        await self._sync_content(
            rel_path,
            f"{topic_name} {' '.join(r[1] or '' for r in case_rows)} "
            f"{' '.join(r[1] or '' for r in exp_rows)}",
        )

    async def _find_group_path(self, group_id: str, session_id: str = "") -> str | None:
        """根据 group_id 找到对应的 md_path"""
        db = self._db
        assert db is not None

        if session_id:
            expected = f"{_DIR_GROUPS}/{session_id}/{group_id}.md"
            async with db.execute(
                "SELECT md_path FROM nodes WHERE md_path = ?", (expected,)
            ) as cur:
                row = await cur.fetchone()
                if row:
                    return row[0]

        # 模糊查找
        async with db.execute(
            "SELECT md_path FROM nodes WHERE md_path LIKE ? AND doc_type = ?",
            (f"%{group_id}%", _DOC_TYPE_GROUP),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None

    async def _resolve_file_id(self, file_id: str) -> str | None:
        """将 file_id 解析为 md_path

        支持：
        - 直接 md_path（如 cases/C-20260420-103000-a1b2c3d4.md）
        - doc_id（如 C-20260420-103000-a1b2c3d4）
        """
        await self._ensure_db_connected()
        db = self._db
        assert db is not None

        # 尝试直接作为 md_path
        async with db.execute(
            "SELECT md_path FROM nodes WHERE md_path = ?", (file_id,)
        ) as cur:
            row = await cur.fetchone()
            if row:
                return row[0]

        # 尝试作为 doc_id 查找（文件名匹配）
        async with db.execute(
            "SELECT md_path FROM nodes WHERE md_path LIKE ?",
            (f"%{file_id}%",),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None

    # ------------------------------------------------------------------
    # 内部：文件 I/O
    # ------------------------------------------------------------------

    @staticmethod
    def _write_md_file(
        abs_path: Path, front_matter: dict[str, Any], body: str
    ) -> None:
        """原子写入 MD 文件（tmp + rename）"""
        fm_str = yaml.dump(
            front_matter, default_flow_style=False,
            allow_unicode=True, sort_keys=False,
        ).strip()
        content = f"---\n{fm_str}\n---\n\n{body}"

        tmp_path = abs_path.with_suffix(".md.tmp")
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.rename(abs_path)

    @staticmethod
    def _parse_front_matter(abs_path: Path) -> tuple[dict[str, Any], str]:
        """解析 MD 文件的 YAML front matter 和 body"""
        raw = abs_path.read_text(encoding="utf-8")
        return SoulV5MemoryManager._parse_front_matter_str(raw)

    @staticmethod
    def _parse_front_matter_str(raw: str) -> tuple[dict[str, Any], str]:
        """从字符串解析 front matter"""
        m = _FM_PATTERN.match(raw)
        if not m:
            return {}, raw

        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            fm = {}

        body = raw[m.end():]
        return fm, body

    def _rel_path(self, abs_path: Path) -> str:
        """返回相对于 md_root 的路径字符串"""
        try:
            return str(abs_path.relative_to(self.md_root))
        except ValueError:
            return str(abs_path)

    def _infer_doc_type(self, rel_path: str) -> str | None:
        """从相对路径推断 doc_type"""
        if rel_path.startswith(f"{_DIR_GROUPS}/"):
            return _DOC_TYPE_GROUP
        if rel_path.startswith(f"{_DIR_CASES}/"):
            return _DOC_TYPE_CASE
        if rel_path.startswith(f"{_DIR_EXPERIENCES}/"):
            return _DOC_TYPE_EXPERIENCE
        if rel_path.startswith(f"{_DIR_TOPICS}/"):
            return _DOC_TYPE_TOPIC
        if rel_path.startswith(f"{_DIR_PREFERENCES}/"):
            return _DOC_TYPE_PREFERENCE
        return None

    def _project_preference_path(self, session_workspace: Path) -> Path:
        """计算项目偏好文件路径"""
        path_hash = hashlib.sha256(str(session_workspace).encode()).hexdigest()[:16]
        return self.md_root / _DIR_PROJECTS / f"{path_hash}.md"

    # ------------------------------------------------------------------
    # 内部：工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _generate_id(type_prefix: str) -> str:
        """生成 ID: {type_prefix}-{date}-{HHMMSS}-{uuid4_short}"""
        now = datetime.now()
        short_uuid = uuid.uuid4().hex[:8]
        return f"{type_prefix}-{now.strftime('%Y%m%d')}-{now.strftime('%H%M%S')}-{short_uuid}"

    @staticmethod
    def _slugify(text: str) -> str:
        """将文本转换为目录/文件名安全的 slug"""
        slug = _SLUG_RE.sub("-", text.strip().lower())
        return slug.strip("-") or "untitled"

    @staticmethod
    def _build_fts_query(query: str) -> str:
        """构建 FTS5 查询字符串

        将空格分割的词用 OR 连接，每个词加前缀匹配。
        """
        tokens = query.strip().split()
        if not tokens:
            return ""
        # 对每个 token 使用前缀匹配，多个 token 之间用 OR 连接
        parts = []
        for t in tokens:
            # 转义 FTS5 特殊字符
            safe = t.replace('"', '""')
            parts.append(f'"{safe}"*')
        return " OR ".join(parts)

    @staticmethod
    def _extract_messages_from_body(body: str) -> list[dict[str, Any]]:
        """从 group MD body 中提取 JSON 消息列表"""
        # 匹配 ```json ... ```
        m = re.search(r"```json\s*\n(.*?)\n```", body, re.DOTALL)
        if not m:
            return []
        try:
            data = json.loads(m.group(1))
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, TypeError):
            pass
        return []

    def _extract_edges_from_topic_body(
        self, topic_rel_path: str, body: str
    ) -> list[tuple[str, str, str]]:
        """从 topic MD 的表格行中提取 case_id / exp_id → 生成边

        解析 | case_id | 和 | exp_id | 列。
        """
        edges: list[tuple[str, str, str]] = []

        # Cases section
        in_cases = False
        in_experiences = False
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("## Cases"):
                in_cases = True
                in_experiences = False
                continue
            if stripped.startswith("## Experiences"):
                in_cases = False
                in_experiences = True
                continue

            if not stripped.startswith("|") or stripped.startswith("|--"):
                continue
            # 跳过 header 行
            cells = [c.strip() for c in stripped.split("|")[1:-1]]
            if not cells:
                continue
            cell_id = cells[0]

            if in_cases and cell_id and not cell_id.startswith("case_id"):
                case_path = f"{_DIR_CASES}/{cell_id}.md"
                edges.append((topic_rel_path, case_path, _EDGE_INDEXES_CASE))
            elif in_experiences and cell_id and not cell_id.startswith("exp_id"):
                exp_path = f"{_DIR_EXPERIENCES}/{cell_id}.md"
                edges.append((topic_rel_path, exp_path, _EDGE_INDEXES_EXPERIENCE))

        return edges
