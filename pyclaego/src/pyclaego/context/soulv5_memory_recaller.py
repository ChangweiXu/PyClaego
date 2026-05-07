"""SoulV5MemoryRecaller — 自动记忆召回引擎

在每轮对话开始时，根据用户消息自动检索相关 case / experience，
并将结果注入系统提示，使 LLM 无需主动调用工具即可获得历史上下文。

工作流（通过配置 workflow 列表决定激活哪些步骤）：
  jieba_kw_extr  → 中文分词关键词提取
  fts_recall     → FTS5 全文检索（experience 优先）
  llm_rerank     → 可选的 LLM 重排序（MVP 默认关闭）
"""

from __future__ import annotations

import json
import re
import traceback
from typing import TYPE_CHECKING

from ..logging import get_running_log
from ..task_manager import SessionTaskHandlerV2, TaskType

if TYPE_CHECKING:
    from .soulv5_memory_manager import SearchResult, SoulV5MemoryManager

_rlog = get_running_log()

# 用于检测是否包含 CJK 字符
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")


class SoulV5MemoryRecaller:
    """自动记忆召回引擎

    从配置 ``memory_recall`` 读取参数，按 ``workflow`` 顺序执行各步骤，
    最终返回 token 预算内的格式化 Markdown 文本。
    """

    def __init__(self, memory_manager: SoulV5MemoryManager) -> None:
        self._manager = memory_manager
        cfg = memory_manager.recall_config

        self.enabled: bool = cfg.get("enabled", True)
        self.token_budget: int = cfg.get("token_budget", 4000)
        self.workflow: list[str] = cfg.get("workflow", ["jieba_kw_extr", "fts_recall"])

        # jieba 配置
        jieba_cfg = cfg.get("methods", {}).get("jieba_kw_extr", {})
        self.jieba_top_k: int = jieba_cfg.get("top_k", 5)

        # FTS 配置
        fts_cfg = cfg.get("methods", {}).get("fts_recall", {})
        self.fts_top_k: int = fts_cfg.get("top_k", 15)
        self.fts_threshold: float = fts_cfg.get("fts_threshold", -5.0)
        self.fts_priority_order: list[str] = fts_cfg.get(
            "priority_order", ["experience", "case"]
        )

        # LLM rerank 配置
        rerank_cfg = cfg.get("methods", {}).get("llm_rerank", {})
        self.rerank_enabled: bool = rerank_cfg.get("enabled", False)
        self.rerank_llm_id: str = rerank_cfg.get("llm_id", "")
        self.rerank_top_k: int = rerank_cfg.get("top_k", 5)

        # jieba 延迟导入标记
        self._jieba_loaded = False

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    async def recall(
        self,
        user_text: str,
        session_task_handler: SessionTaskHandlerV2 | None = None,
    ) -> str:
        """根据用户消息检索相关记忆，返回格式化 Markdown（可直接拼入系统提示）。

        Returns:
            非空字符串 = 有召回内容；空字符串 = 无召回或已禁用。
        """
        if not self.enabled or not user_text.strip():
            return ""
        if not session_task_handler:
            raise ValueError("LLM rerank 需要 session_task_handler 来记录任务状态")
        
        recall_task_handler = await session_task_handler.create_subtask(
            TaskType.MEMORY_RECALL,
            f"记忆召回: {user_text[:30]}...",
        )
        await recall_task_handler.start()

        keywords: list[str] = []
        candidates: list[SearchResult] = []

        for step in self.workflow:
            if step == "jieba_kw_extr":
                keywords = await self._jieba_extract(user_text, session_task_handler)
            elif step == "fts_recall":
                if not keywords:
                    # 无关键词时直接用原文分词
                    keywords = user_text.strip().split()
                candidates = await self._fts_recall(keywords, session_task_handler)
            elif step == "llm_rerank":
                if self.rerank_enabled and candidates:
                    candidates = await self._llm_rerank(
                        user_text, candidates, recall_task_handler
                    )

        if not candidates:
            await recall_task_handler.complete()
            return ""

        formatted_results = await self._format_results(candidates, recall_task_handler)
        await recall_task_handler.complete()
        return formatted_results

    # ------------------------------------------------------------------
    # 内部步骤
    # ------------------------------------------------------------------

    async def _jieba_extract(self, text: str, session_task_handler: SessionTaskHandlerV2) -> list[str]:
        """使用 jieba TF-IDF 提取关键词。

        对于不含 CJK 字符的纯英文文本，回退到空格分词。
        """
        # 不含中文时，jieba 效果有限，直接空格分词
        if not _CJK_RE.search(text):
            tokens = text.strip().split()
            return tokens[: self.jieba_top_k] if tokens else []

        try:
            await self._ensure_jieba(session_task_handler)
            import jieba.analyse
            keywords = jieba.analyse.extract_tags(text, topK=self.jieba_top_k)
            if keywords:
                await session_task_handler.log_info(
                    f"[SoulV5MemoryRecaller] jieba 提取关键词: {keywords}",
                )
                return keywords
        except Exception as e:
            await session_task_handler.log_warning(
                f"[SoulV5MemoryRecaller] jieba 提取失败，回退空格分词: {e}\n{traceback.format_exc()}",
            )

        # 回退
        await session_task_handler.log_warning(
            "[SoulV5MemoryRecaller] jieba 提取关键词失败，使用空格分词回退",
        )
        return text.strip().split()[: self.jieba_top_k]

    async def _fts_recall(
        self, keywords: list[str], session_task_handler: SessionTaskHandlerV2
    ) -> list[SearchResult]:
        """两轮 FTS5 检索：按 priority_order 依次查询，合并去重。"""

        await session_task_handler.log_info(
            f"[SoulV5MemoryRecaller] FTS 检索关键词: {keywords}",
        )

        kw_str = " ".join(keywords)
        if not kw_str.strip():
            return []

        seen_paths: set = set()
        merged: list[SearchResult] = []

        for doc_type in self.fts_priority_order:
            try:
                results = await self._manager.query(
                    query=kw_str,
                    doc_type=doc_type,
                    top_k=self.fts_top_k,
                )
            except Exception as e:
                await session_task_handler.log_warning(
                    f"[SoulV5MemoryRecaller] FTS 检索 {doc_type} 失败: {e}\n{traceback.format_exc()}",
                )
                continue

            for r in results:
                if r.rank < self.fts_threshold:
                    continue
                if r.md_path in seen_paths:
                    continue
                seen_paths.add(r.md_path)
                merged.append(r)
            await session_task_handler.log_info(
                f"[SoulV5MemoryRecaller] FTS 检索到 {len(merged)} 条 `{doc_type}` 结果",
            )

        return merged

    async def _llm_rerank(
        self,
        user_text: str,
        candidates: list[SearchResult],
        session_task_handler: SessionTaskHandlerV2,
    ) -> list[SearchResult]:
        """使用 LLM 对候选结果重排序。失败时返回原列表（无损降级）。"""
        if not session_task_handler or not candidates:
            return candidates

        _str_candidates = "\n".join([r.md_path for r in candidates])
        await session_task_handler.log_info(
            f"""[SoulV5MemoryRecaller] LLM 重排序前候选项: {
                _str_candidates
            }""",
        )

        # 构建编号摘要
        numbered = []
        for i, r in enumerate(candidates):
            numbered.append(f"[{i}] ({r.doc_type}) {r.title}\n{r.snippet}")
        snippets_text = "\n\n".join(numbered)

        system = (
            "你是一个相关性判断器。给定用户查询和一组记忆摘要，"
            f"返回最相关的 {self.rerank_top_k} 条的编号，"
            "以 JSON 整数数组格式返回，如 [0, 3, 1]。"
            "只返回 JSON 数组，不要其他文字。"
        )
        user_prompt = f"用户查询：{user_text}\n\n候选记忆：\n{snippets_text}"

        try:
            # 选择 rerank 专用 llm_id 或 manager 默认
            original_llm_id = self._manager.llm_id
            if self.rerank_llm_id:
                self._manager.llm_id = self.rerank_llm_id

            try:
                response = await self._manager._call_llm(
                    session_task_handler=session_task_handler,
                    system=system,
                    user_text=user_prompt,
                    max_tokens=200,
                )
            finally:
                self._manager.llm_id = original_llm_id

            if not response:
                return candidates

            # 解析 JSON 数组
            # 尝试提取 JSON 部分（LLM 可能包裹在 markdown code block 中）
            cleaned = response.strip()
            if cleaned.startswith("```"):
                cleaned = re.sub(r"^```\w*\n?", "", cleaned)
                cleaned = re.sub(r"\n?```$", "", cleaned)
                cleaned = cleaned.strip()

            indices = json.loads(cleaned)
            if not isinstance(indices, list):
                return candidates

            # 过滤有效索引
            reranked = []
            for idx in indices:
                if isinstance(idx, int) and 0 <= idx < len(candidates):
                    reranked.append(candidates[idx])

            return reranked if reranked else candidates

        except Exception as e:
            await session_task_handler.log_warning(
                f"[SoulV5MemoryRecaller] LLM rerank 失败，保持原序: {e}\n{traceback.format_exc()}",
            )
            return candidates

    async def _format_results(
        self, candidates: list[SearchResult], session_task_handler: SessionTaskHandlerV2
    ) -> str:
        """读取候选文件全文，在 token 预算内拼接为 Markdown。"""
        sections: list[str] = []
        used_tokens = 0

        # 估算标题开销
        header = (
            "# 相关记忆（自动召回）\n\n"
            "以下是与当前问题可能相关的历史经验，仅供参考：\n"
        )
        header_tokens = self._manager.token_counter.count_tokens(header)
        used_tokens += header_tokens

        for r in candidates:
            try:
                content = await self._manager.read_file(r.md_path)
            except Exception:
                content = None

            if not content:
                continue

            section = "\n".join([
                "",
                f"### [{r.doc_type}] {r.title}",
                "",
                "`````````md",  # 使用多重反引号包裹，避免内容中有 ``` 时提前结束代码块
                f"{content}",
                "`````````",
                "",
            ])
            section_tokens = self._manager.token_counter.count_tokens(section)

            if used_tokens + section_tokens > self.token_budget:
                break

            sections.append(section)
            used_tokens += section_tokens

        await session_task_handler.log_info(
            f"[SoulV5MemoryRecaller] 格式化结果: {len(sections)} 条，{used_tokens} tokens",
        )

        if not sections:
            return ""

        return header + "".join(sections)

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    async def _ensure_jieba(self, session_task_handler: SessionTaskHandlerV2) -> None:
        """首次使用时初始化 jieba（静默模式，避免打印加载日志）。"""
        if self._jieba_loaded:
            return
        try:
            import jieba
            jieba.setLogLevel(20)  # WARNING level，抑制加载信息
            self._jieba_loaded = True
        except ImportError:
            await session_task_handler.log_error(
                "[SoulV5MemoryRecaller] jieba 未安装，请 pip install jieba",
            )
            raise
