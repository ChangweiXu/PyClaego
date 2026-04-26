"""SoulV5 memory_deprecate 工具 — 标记记忆为过期"""

from typing import Dict, Any

from .soulv5_base import SoulV5MemoryBaseTool
from ...tool.base_tool import ToolResult


class SoulV5DeprecateTool(SoulV5MemoryBaseTool):
    """memory_deprecate: 将 case 或 experience 标记为已过期"""

    IS_READONLY: bool = False
    IS_PARALLELIZABLE: bool = False

    async def execute(self, **kwargs) -> ToolResult:
        """废弃指定记忆

        Args:
            file_id (str, required): doc_id（如 C-... 或 E-...）
        """
        valid, err = self.validate_params(["file_id"], **kwargs)
        if not valid:
            return self._fail(err)

        file_id = str(kwargs["file_id"]).strip()

        # 解析 md_path
        if file_id.startswith("C-"):
            md_path = f"cases/{file_id}.md"
        elif file_id.startswith("E-"):
            md_path = f"experiences/{file_id}.md"
        else:
            return self._fail(f"无法识别 file_id 类型: {file_id}（需以 C- 或 E- 开头）")

        try:
            await self.memory_manager.deprecate(md_path)
        except FileNotFoundError:
            return self._fail(f"未找到文件: {file_id}")
        except Exception as e:
            return self._fail(f"废弃失败: {e}")

        return self._success(output={"file_id": file_id, "deprecated": True})

    def get_description(self) -> Dict[str, Any]:
        return {
            "name": self.tool_name,
            "description": (
                "将 case 或 experience 标记为已过期。"
                "被废弃的记录不会出现在搜索结果和话题列表中。"
            ),
            "parameters": {
                "file_id": {
                    "type": "string",
                    "required": True,
                    "description": "文档 ID（如 C-20260420-103000-a1b2c3d4 或 E-...）",
                },
            },
            "is_readonly": False,
            "is_parallelizable": False,
        }
