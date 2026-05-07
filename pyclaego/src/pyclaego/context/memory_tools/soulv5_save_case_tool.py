"""SoulV5 memory_save_case 工具 — 创建 case 记忆"""

from typing import Any

from ...tool.base_tool import ToolResult
from .soulv5_base import SoulV5MemoryBaseTool


class SoulV5SaveCaseTool(SoulV5MemoryBaseTool):
    """memory_save_case: 从对话中创建一条 case 记录"""

    IS_READONLY: bool = False
    IS_PARALLELIZABLE: bool = False

    async def execute(self, **kwargs) -> ToolResult:
        """创建 case

        Args:
            title (str, required): case 标题
            content (str, required): case 正文（Markdown）
            tags (list[str], optional): 标签列表
            group_ids (list[str], optional): 关联的 group ID 列表
            topic (str, required): 所属话题名称
        """
        valid, err = self.validate_params(["title", "content", "topic"], **kwargs)
        if not valid:
            return self._fail(err)

        title = str(kwargs["title"]).strip()
        content = str(kwargs["content"]).strip()
        topic = str(kwargs["topic"]).strip()
        tags: list[str] = kwargs.get("tags") or []
        group_ids: list[str] = kwargs.get("group_ids") or []
        session_id: str = kwargs.get("_session_id", "")

        if not title or not content or not topic:
            return self._fail("title / content / topic 均不能为空")

        try:
            case_id = await self.memory_manager.save_case(
                title=title,
                content=content,
                tags=tags,
                group_ids=group_ids,
                topic=topic,
                session_id=session_id,
            )
        except Exception as e:
            return self._fail(f"保存 case 失败: {e}")

        return self._success(output={
            "case_id": case_id,
            "topic": topic,
            "group_count": len(group_ids),
        })

    def get_description(self) -> dict[str, Any]:
        return {
            "name": self.tool_name,
            "description": (
                "创建一条 case 记录：记录问题、尝试方案、结果。"
                "可关联对话 group，会自动归入指定话题。"
            ),
            "parameters": {
                "title": {
                    "type": "string",
                    "required": True,
                    "description": "case 标题",
                },
                "content": {
                    "type": "string",
                    "required": True,
                    "description": "case 正文（Markdown）：问题描述、尝试方案、用户反馈、最终结果",
                },
                "topic": {
                    "type": "string",
                    "required": True,
                    "description": "所属话题（如'Python异步编程'，应足够宽泛以涵盖相关场景）",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "required": False,
                    "description": "标签列表",
                },
                "group_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "required": False,
                    "description": "关联的对话 group ID 列表",
                },
            },
            "is_readonly": False,
            "is_parallelizable": False,
        }
