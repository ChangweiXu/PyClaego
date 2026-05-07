"""SoulV5 memory_read 工具 — 按 ID 或路径读取 MD 文件全文"""

from typing import Any

from ...tool.base_tool import ToolResult
from .soulv5_base import SoulV5MemoryBaseTool


class SoulV5ReadTool(SoulV5MemoryBaseTool):
    """memory_read: 读取记忆文件全文"""

    IS_READONLY: bool = True
    IS_PARALLELIZABLE: bool = True

    async def execute(self, **kwargs) -> ToolResult:
        """读取指定记忆文件

        Args:
            file_id (str, required): md_path 或 doc_id（如 C-20260420-103000-a1b2c3d4）
        """
        valid, err = self.validate_params(["file_id"], **kwargs)
        if not valid:
            return self._fail(err)

        file_id = str(kwargs["file_id"]).strip()
        if not file_id:
            return self._fail("file_id 不能为空")

        try:
            content = await self.memory_manager.read_file(file_id)
        except Exception as e:
            return self._fail(f"读取异常: {e}")

        if content is None:
            return self._fail(f"未找到文件: {file_id}")

        return self._success(output={"file_id": file_id, "content": content})

    def get_description(self) -> dict[str, Any]:
        return {
            "name": self.tool_name,
            "description": "按 ID 或路径读取记忆文件全文。",
            "parameters": {
                "file_id": {
                    "type": "string",
                    "required": True,
                    "description": (
                        "文件标识：md_path（如 cases/C-20260420-103000-a1b2c3d4.md）"
                        "或 doc_id（如 C-20260420-103000-a1b2c3d4）"
                    ),
                },
            },
            "is_readonly": True,
            "is_parallelizable": True,
        }
