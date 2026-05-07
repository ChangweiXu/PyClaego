"""SoulV5 memory_save_experience 工具 — 创建 experience 记忆"""

from typing import Any

from ...tool.base_tool import ToolResult
from .soulv5_base import SoulV5MemoryBaseTool


class SoulV5SaveExperienceTool(SoulV5MemoryBaseTool):
    """memory_save_experience: 从 case 中提炼操作指南"""

    IS_READONLY: bool = False
    IS_PARALLELIZABLE: bool = False

    async def execute(self, **kwargs) -> ToolResult:
        """创建 experience

        Args:
            title (str, required): experience 标题
            content (str, required): experience 正文（操作指南）
            tags (list[str], optional): 标签列表
            case_ids (list[str], optional): 关联的 case ID 列表
            topic (str, required): 所属话题
            scope (str, optional): 适用范围说明
        """
        valid, err = self.validate_params(["title", "content", "topic"], **kwargs)
        if not valid:
            return self._fail(err)

        title = str(kwargs["title"]).strip()
        content = str(kwargs["content"]).strip()
        topic = str(kwargs["topic"]).strip()
        tags: list[str] = kwargs.get("tags") or []
        case_ids: list[str] = kwargs.get("case_ids") or []
        scope: str = str(kwargs.get("scope", "")).strip()

        if not title or not content or not topic:
            return self._fail("title / content / topic 均不能为空")

        try:
            exp_id = await self.memory_manager.save_experience(
                title=title,
                content=content,
                tags=tags,
                case_ids=case_ids,
                topic=topic,
                scope=scope,
            )
        except Exception as e:
            return self._fail(f"保存 experience 失败: {e}")

        return self._success(output={
            "exp_id": exp_id,
            "topic": topic,
            "case_count": len(case_ids),
        })

    def get_description(self) -> dict[str, Any]:
        return {
            "name": self.tool_name,
            "description": (
                "创建一条 experience 记录：从多个 case 中提炼的操作指南。"
                "应保留必要细节使无先验知识的 Agent 能正确执行。"
            ),
            "parameters": {
                "title": {
                    "type": "string",
                    "required": True,
                    "description": "experience 标题",
                },
                "content": {
                    "type": "string",
                    "required": True,
                    "description": "experience 正文（Markdown）：适用场景、步骤说明、注意事项",
                },
                "topic": {
                    "type": "string",
                    "required": True,
                    "description": "所属话题",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "required": False,
                    "description": "标签列表",
                },
                "case_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "required": False,
                    "description": "关联的 case ID 列表",
                },
                "scope": {
                    "type": "string",
                    "required": False,
                    "description": "适用范围说明（何时适用、何时不适用）",
                },
            },
            "is_readonly": False,
            "is_parallelizable": False,
        }
