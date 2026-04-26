"""SoulV5 memory_browse_topics 工具 — 浏览话题索引"""

from typing import Dict, Any

from .soulv5_base import SoulV5MemoryBaseTool
from ...tool.base_tool import ToolResult


class SoulV5BrowseTopicsTool(SoulV5MemoryBaseTool):
    """memory_browse_topics: 列出所有话题及其关联数量"""

    IS_READONLY: bool = True
    IS_PARALLELIZABLE: bool = True

    async def execute(self, **kwargs) -> ToolResult:
        """浏览所有话题"""
        try:
            topics = await self.memory_manager.browse_topics()
        except Exception as e:
            return self._fail(f"浏览话题异常: {e}")

        items = [
            {
                "topic": t.topic_slug,
                "title": t.title,
                "cases": t.case_count,
                "experiences": t.experience_count,
                "modified": t.modified_at,
            }
            for t in topics
        ]

        return self._success(output={
            "total_topics": len(items),
            "topics": items,
        })

    def get_description(self) -> Dict[str, Any]:
        return {
            "name": self.tool_name,
            "description": "列出记忆库中所有话题，显示其关联的 case / experience 数量。",
            "parameters": {},
            "is_readonly": True,
            "is_parallelizable": True,
        }
