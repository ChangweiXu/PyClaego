"""ReadImageBase64Tool - 读取图片文件并返回多模态图片内容"""

import base64
from typing import Any

from ...llm.types import ImagePart
from ...logging import get_running_log
from ..base_tool import ToolResult, ToolStatus
from .fs_base_tool import FileSystemBaseTool

_rlog = get_running_log()

# 扩展名 -> MIME 类型映射
_EXT_TO_MIME: dict[str, str] = {
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif":  "image/gif",
    ".webp": "image/webp",
    ".bmp":  "image/bmp",
    ".svg":  "image/svg+xml",
    ".ico":  "image/x-icon",
    ".tiff": "image/tiff",
    ".tif":  "image/tiff",
}


class ReadImageBase64Tool(FileSystemBaseTool):
    """图片 Base64 读取工具

    功能：
    - 读取本地图片文件，返回 Base64 编码字符串
    - 自动推导 MIME 类型（media_type）
    - 支持常见图片格式：PNG / JPEG / GIF / WebP / BMP / SVG 等
    - 遵循文件大小限制（max_file_size）

    配置示例：
    ```yaml
    read_image_base64:
      tool_type: "read_image_base64"
      tool_name: "read_image_base64"
      enabled: true
      working_dir: null
      max_file_size: 10485760   # 10MB
      allowed_paths: []
      blocked_paths: []
    ```
    """

    # 仅读取文件内容，不修改任何状态；多个并发读取安全
    IS_READONLY: bool = True
    IS_PARALLELIZABLE: bool = True

    def extract_paths(self, args: dict[str, Any]) -> dict[str, list]:
        p = args.get("path")
        return {"read": [p] if isinstance(p, str) and p.strip() else [], "write": []}

    async def execute(self, **kwargs) -> ToolResult:
        """执行图片读取与 Base64 编码

        Args:
            path: 图片文件路径（相对路径基于 working_dir 解析）

        Returns:
            ToolResult: 包含 base64, media_type, file_size
        """
        valid, error_msg = self.validate_params(["path"], **kwargs)
        if not valid:
            return ToolResult(status=ToolStatus.FAILED, error=error_msg)

        path = self._resolve_path(kwargs["path"])

        # 安全检查
        ok, err = self._security_check(path, require_exists=True, must_be_file=True)
        if not ok:
            return ToolResult(status=ToolStatus.FAILED, error=err)

        ok, err = self._check_file_size(path)
        if not ok:
            return ToolResult(status=ToolStatus.FAILED, error=err)

        # 推导 MIME 类型
        ext = path.suffix.lower()
        media_type = _EXT_TO_MIME.get(ext, "application/octet-stream")

        try:
            _rlog.info("core_service", f"读取图片: {path} (media_type={media_type})")

            with open(path, "rb") as f:
                raw_bytes = f.read()

            b64_str = base64.b64encode(raw_bytes).decode("ascii")
            file_size = len(raw_bytes)

            _rlog.info("core_service", f"图片读取成功: {path}, size={file_size}, media_type={media_type}")

            return ToolResult(
                status=ToolStatus.SUCCESS,
                output=f"图片已读取: {path} ({media_type}, {file_size} bytes)",
                metadata={
                    "path": str(path),
                    "media_type": media_type,
                },
                content_parts=[
                    ImagePart(
                        source_type="base64",
                        data=b64_str,
                        media_type=media_type,
                    ),
                ],
            )

        except Exception as e:
            error_msg = f"读取图片异常: {path}, 错误: {e!s}"
            _rlog.error("core_service", error_msg)
            return ToolResult(status=ToolStatus.FAILED, error=error_msg)

    def get_description(self) -> dict[str, Any]:
        return {
            "name": self.tool_name,
            "description": "读取本地图片文件并返回 Base64 编码字符串及 MIME 类型",
            "parameters": {
                "path": {
                    "type": "string",
                    "required": True,
                    "description": "图片文件路径（支持 PNG / JPEG / GIF / WebP / BMP / SVG 等）"
                }
            },
            "returns": {
                "base64": "图片的 Base64 编码字符串（ASCII）",
                "media_type": "图片 MIME 类型，如 image/png、image/jpeg",
                "file_size": "图片文件字节大小"
            },
            "examples": [
                {
                    "path": "assets/logo.png",
                    "description": "读取 PNG 图片并返回 Base64 编码"
                },
                {
                    "path": "screenshots/demo.jpg",
                    "description": "读取 JPEG 截图并返回 Base64 编码"
                }
            ],
            "is_readonly": self.__class__.IS_READONLY,
            "is_parallelizable": self.__class__.IS_PARALLELIZABLE,
        }
