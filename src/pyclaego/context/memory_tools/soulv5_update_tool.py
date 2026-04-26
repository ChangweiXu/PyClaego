"""SoulV5 memory_update 工具 — 原地更新 case 或 experience"""

from typing import Dict, Any, List

from .soulv5_base import SoulV5MemoryBaseTool
from ...tool.base_tool import ToolResult


class SoulV5UpdateTool(SoulV5MemoryBaseTool):
    """memory_update: 更新已有的 case 或 experience 内容"""

    IS_READONLY: bool = False
    IS_PARALLELIZABLE: bool = False

    async def execute(self, **kwargs) -> ToolResult:
        """更新 case 或 experience

        Args:
            file_id (str, required): doc_id（如 C-20260420-... 或 E-20260420-...）
            content (str, optional): 新的正文内容
            tags (list[str], optional): 新的标签列表
            scope (str, optional): 新的适用范围（仅 experience）
        """
        valid, err = self.validate_params(["file_id"], **kwargs)
        if not valid:
            return self._fail(err)

        file_id = str(kwargs["file_id"]).strip()
        content = kwargs.get("content")
        tags = kwargs.get("tags")
        scope = kwargs.get("scope")

        if content is not None:
            content = str(content).strip()
        if tags is not None and not isinstance(tags, list):
            return self._fail("tags 必须是字符串列表")

        if content is None and tags is None and scope is None:
            return self._fail("至少需要提供 content、tags 或 scope 之一")

        try:
            if file_id.startswith("C-"):
                await self.memory_manager.update_case(
                    case_id=file_id, content=content, tags=tags,
                )
            elif file_id.startswith("E-"):
                await self.memory_manager.update_experience(
                    exp_id=file_id, content=content, tags=tags, scope=scope,
                )
            else:
                return self._fail(f"无法识别 file_id 类型: {file_id}（需以 C- 或 E- 开头）")
        except FileNotFoundError:
            return self._fail(f"未找到文件: {file_id}")
        except Exception as e:
            return self._fail(f"更新失败: {e}")

        return self._success(output={"file_id": file_id, "updated": True})

    def get_description(self) -> Dict[str, Any]:
        return {
            "name": self.tool_name,
            "description": "更新已有的 case 或 experience 文件（原地修改）。",
            "parameters": {
                "file_id": {
                    "type": "string",
                    "required": True,
                    "description": "文档 ID（如 C-20260420-103000-a1b2c3d4 或 E-...）",
                },
                "content": {
                    "type": "string",
                    "required": False,
                    "description": "新的正文内容（Markdown）",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "required": False,
                    "description": "新的标签列表",
                },
                "scope": {
                    "type": "string",
                    "required": False,
                    "description": "新的适用范围说明（仅 experience 有效）",
                },
            },
            "is_readonly": False,
            "is_parallelizable": False,
        }
