"""SoulV5 memory_query 工具 — FTS5 全文搜索"""

from typing import Dict, Any, List

from .soulv5_base import SoulV5MemoryBaseTool
from ...tool.base_tool import ToolResult


class SoulV5QueryTool(SoulV5MemoryBaseTool):
    """memory_query: 在记忆库中全文搜索 case / experience / topic"""

    IS_READONLY: bool = True
    IS_PARALLELIZABLE: bool = True

    def __init__(self, tool_config: Dict[str, Any], memory_manager: Any):
        super().__init__(tool_config, memory_manager)
        self._top_k_default: int = int(tool_config.get("top_k_default", 5))

    async def execute(self, **kwargs) -> ToolResult:
        """执行全文检索

        Args:
            query (str, required): 检索词
            doc_type (str, optional): 过滤类型 (case/experience/topic)
            topic (str, optional): 过滤话题
            top_k (int, optional): 返回最多 N 条
        """
        valid, err = self.validate_params(["query"], **kwargs)
        if not valid:
            return self._fail(err)

        query: str = str(kwargs["query"]).strip()
        if not query:
            return self._fail("query 参数不能为空")

        doc_type = kwargs.get("doc_type")
        topic = kwargs.get("topic")
        top_k = self._coerce_int(kwargs.get("top_k", self._top_k_default), self._top_k_default)

        try:
            results = await self.memory_manager.query(
                query=query, doc_type=doc_type, topic=topic, top_k=top_k,
            )
        except Exception as e:
            return self._fail(f"检索异常: {e}")

        items = [
            {
                "md_path": r.md_path,
                "doc_type": r.doc_type,
                "title": r.title,
                "tags": r.tags,
                "snippet": r.snippet,
                "rank": round(r.rank, 4),
            }
            for r in results
        ]

        return self._success(output={
            "total_matched": len(items),
            "results": items,
        })

    def get_description(self) -> Dict[str, Any]:
        return {
            "name": self.tool_name,
            "description": (
                "在记忆库中全文搜索历史案例、经验和话题。"
                "遇到相似问题时先调用此工具查找已有经验。"
            ),
            "parameters": {
                "query": {
                    "type": "string",
                    "required": True,
                    "description": "搜索关键词，如 'asyncio 并发'",
                },
                "doc_type": {
                    "type": "string",
                    "required": False,
                    "description": "过滤文档类型: case / experience / topic",
                },
                "topic": {
                    "type": "string",
                    "required": False,
                    "description": "过滤话题名称",
                },
                "top_k": {
                    "type": "integer",
                    "required": False,
                    "description": f"返回最多 N 条结果（默认 {self._top_k_default}）",
                },
            },
            "is_readonly": True,
            "is_parallelizable": True,
        }
