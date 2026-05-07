"""SoulV5 memory_update_preferences 工具 — 更新用户/项目偏好"""

from typing import Any

from ...tool.base_tool import ToolResult
from .soulv5_base import SoulV5MemoryBaseTool


class SoulV5UpdatePreferencesTool(SoulV5MemoryBaseTool):
    """memory_update_preferences: 更新用户或项目偏好文件"""

    IS_READONLY: bool = False
    IS_PARALLELIZABLE: bool = False

    async def execute(self, **kwargs) -> ToolResult:
        """更新偏好

        Args:
            target (str, required): "user" 或 "project"
            content (str, required): 新的偏好内容（Markdown）
        """
        valid, err = self.validate_params(["target", "content"], **kwargs)
        if not valid:
            return self._fail(err)

        target = str(kwargs["target"]).strip().lower()
        content = str(kwargs["content"]).strip()

        if target not in ("user", "project"):
            return self._fail(f"target 必须是 'user' 或 'project'，收到: {target}")
        if not content:
            return self._fail("content 不能为空")

        # workspace_path 由 context handler 注入
        workspace_path = kwargs.get("_workspace_path")
        if target == "project" and not workspace_path:
            return self._fail("更新 project 偏好需要工作区路径（由系统自动提供）")

        from pathlib import Path

        try:
            await self.memory_manager.update_preferences(
                target=target,
                content=content,
                session_workspace=Path(workspace_path) if workspace_path else None,
            )
        except ValueError as e:
            return self._fail(str(e))
        except Exception as e:
            return self._fail(f"更新偏好失败: {e}")

        return self._success(output={"target": target, "updated": True})

    def get_description(self) -> dict[str, Any]:
        return {
            "name": self.tool_name,
            "description": (
                "更新用户或项目偏好文件。偏好会在每次对话开始时自动注入系统提示。"
                "用于存储：编程风格、工具链偏好、沟通风格等。"
            ),
            "parameters": {
                "target": {
                    "type": "string",
                    "required": True,
                    "description": "'user'（全局偏好）或 'project'（项目偏好）",
                },
                "content": {
                    "type": "string",
                    "required": True,
                    "description": "偏好内容（Markdown），如 '- 使用中文回答\\n- 代码注释用英文'",
                },
            },
            "is_readonly": False,
            "is_parallelizable": False,
        }
