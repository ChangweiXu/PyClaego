"""DownloadFileTool - 从 URL 下载文件到本地"""

import os
from pathlib import Path
import traceback
from typing import Dict, Any, Optional

from ..base_tool import BaseTool, ToolResult, ToolStatus
from ...logging import get_running_log

_rlog = get_running_log()


class DownloadFileTool(BaseTool):
    """文件下载工具

    功能：
    - 异步下载 HTTP/HTTPS URL 指向的远端文件
    - 流式写入，避免内存中保留完整文件
    - 支持覆盖控制
    - 自动创建目标父目录

    配置示例：
    ```yaml
    download_file:
      tool_type: "download_file"
      tool_name: "download_file"
      enabled: true
      timeout: 60
    ```
    """

    # 写入本地文件，修改文件系统；但多个不同 dest 的下载可并发
    IS_READONLY: bool = False
    IS_PARALLELIZABLE: bool = True

    def extract_paths(self, args: Dict[str, Any]) -> Dict[str, list]:
        d = args.get("dest")
        return {"read": [], "write": [d] if isinstance(d, str) and d.strip() else []}

    async def execute(self, **kwargs) -> ToolResult:
        """执行文件下载

        Args:
            url: 要下载的文件 URL（仅支持 http/https）
            dest: 本地保存路径
            overwrite: 是否覆盖已存在文件（默认 false）
            timeout: 超时秒数（默认使用工具配置 timeout）

        Returns:
            ToolResult: 包含 dest, size_bytes, content_type
        """
        valid, error_msg = self.validate_params(["url", "dest"], **kwargs)
        if not valid:
            return ToolResult(status=ToolStatus.FAILED, error=error_msg)

        url: str = kwargs["url"]
        dest_raw: str = kwargs["dest"]
        overwrite: bool = self._coerce_bool(kwargs.get("overwrite", False), default=False)
        timeout_sec: int = self._coerce_int(kwargs.get("timeout", self.timeout), default=self.timeout)

        # URL scheme 校验
        if not (url.startswith("http://") or url.startswith("https://")):
            return ToolResult(
                status=ToolStatus.FAILED,
                error=f"不支持的 URL scheme，仅允许 http:// 和 https://: {url}"
            )

        # 解析目标路径（相对路径基于 cwd）
        dest = Path(dest_raw).expanduser()
        if not dest.is_absolute():
            dest = Path(os.getcwd()) / dest
        dest = dest.resolve()

        # 覆盖检查
        if dest.exists() and not overwrite:
            return ToolResult(
                status=ToolStatus.FAILED,
                error=f"目标文件已存在且 overwrite=False: {dest}"
            )

        try:
            _rlog.info("core_service", f"下载文件: {url} -> {dest} (timeout={timeout_sec}s)")

            # 自动创建父目录
            dest.parent.mkdir(parents=True, exist_ok=True)

            content_type: Optional[str] = None
            size_bytes: int = 0

            import httpx
            async with httpx.AsyncClient(
                http2=True,
                follow_redirects=True,
                timeout=httpx.Timeout(timeout_sec, connect=10.0),
            ) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code != 200:
                        return ToolResult(
                            status=ToolStatus.FAILED,
                            error=f"HTTP 请求失败，状态码: {response.status_code}, URL: {url}"
                        )
                    content_type = response.headers.get("content-type", "")

                    with open(dest, "wb") as f:
                        async for chunk in response.aiter_bytes(chunk_size=65536):
                            f.write(chunk)
                            size_bytes += len(chunk)

            _rlog.info("core_service", f"下载完成: {dest}, size={size_bytes}, content_type={content_type}")

            return ToolResult(
                status=ToolStatus.SUCCESS,
                output={
                    "dest": str(dest),
                    "size_bytes": size_bytes,
                    "content_type": content_type or "",
                },
                metadata={
                    "url": url,
                    "dest": str(dest),
                    "overwrite": overwrite,
                }
            )

        except Exception as e:
            error_msg = f"下载文件异常: url={url}, dest={dest}, 错误: {str(e)}\n{traceback.format_exc()}"
            _rlog.error("core_service", error_msg)
            return ToolResult(status=ToolStatus.FAILED, error=error_msg)

    def mask_output(self, raw_output: Any, path_mask_map: Dict[str, str]) -> Any:
        """对 dest 字段进行路径脱敏。"""
        if not isinstance(raw_output, dict):
            return raw_output
        masked = dict(raw_output)
        if "dest" in masked and isinstance(masked["dest"], str):
            masked["dest"] = self._mask_string(masked["dest"], path_mask_map)
        return masked

    def get_description(self) -> Dict[str, Any]:
        return {
            "name": self.tool_name,
            "description": "从 HTTP/HTTPS URL 下载文件到本地路径",
            "parameters": {
                "url": {
                    "type": "string",
                    "required": True,
                    "description": "文件 URL（仅支持 http:// 和 https://）"
                },
                "dest": {
                    "type": "string",
                    "required": True,
                    "description": "本地保存路径（父目录不存在时自动创建）"
                },
                "overwrite": {
                    "type": "boolean",
                    "required": False,
                    "description": "是否覆盖已存在文件（默认 false）"
                },
                "timeout": {
                    "type": "integer",
                    "required": False,
                    "description": "超时秒数（默认使用工具配置值）"
                }
            },
            "returns": {
                "dest": "本地保存的绝对路径",
                "size_bytes": "下载文件的字节大小",
                "content_type": "响应 Content-Type 头"
            },
            "examples": [
                {
                    "url": "https://example.com/data.csv",
                    "dest": "downloads/data.csv",
                    "description": "下载 CSV 文件到 downloads/ 目录"
                },
                {
                    "url": "https://example.com/image.png",
                    "dest": "assets/image.png",
                    "overwrite": True,
                    "description": "下载图片并覆盖已有文件"
                }
            ],
            "is_readonly": self.__class__.IS_READONLY,
            "is_parallelizable": self.__class__.IS_PARALLELIZABLE,
        }
