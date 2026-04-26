"""ReadPdfTool - 读取 PDF 文件并返回多模态文档内容"""

import base64
from pathlib import Path
from typing import Dict, Any

from .fs_base_tool import FileSystemBaseTool
from ..base_tool import ToolResult, ToolStatus
from ...llm.types import DocumentPart, TextPart
from ...logging import get_running_log

_rlog = get_running_log()


class ReadPdfTool(FileSystemBaseTool):
    """PDF 文件读取工具

    功能：
    - 读取本地 PDF 文件，以多模态 DocumentPart 形式返回
    - Anthropic / Gemini 原生支持 PDF 文档
    - OpenAI 降级为文本摘要（由 LLM 客户端层处理）
    - 可选文本提取（依赖 PyPDF2，未安装时仅返回二进制）
    - 遵循文件大小限制（max_file_size）

    配置示例：
    ```yaml
    read_pdf:
      tool_type: "read_pdf"
      tool_name: "read_pdf"
      enabled: true
      working_dir: null
      max_file_size: 31457280   # 30MB
      allowed_paths: []
      blocked_paths: []
    ```
    """

    IS_READONLY: bool = True
    IS_PARALLELIZABLE: bool = True

    def extract_paths(self, args: Dict[str, Any]) -> Dict[str, list]:
        p = args.get("path")
        return {"read": [p] if isinstance(p, str) and p.strip() else [], "write": []}

    async def execute(self, **kwargs) -> ToolResult:
        """执行 PDF 文件读取

        Args:
            path: PDF 文件路径（相对路径基于 working_dir 解析）

        Returns:
            ToolResult: 包含 DocumentPart 多模态内容
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

        # 检查扩展名
        if path.suffix.lower() != ".pdf":
            return ToolResult(
                status=ToolStatus.FAILED,
                error=f"不是 PDF 文件: {path} (扩展名: {path.suffix})",
            )

        try:
            _rlog.info("core_service", f"读取 PDF: {path}")

            with open(path, "rb") as f:
                raw_bytes = f.read()

            b64_str = base64.b64encode(raw_bytes).decode("ascii")
            file_size = len(raw_bytes)

            # 尝试提取文本摘要（作为纯文本回退）
            text_summary = self._extract_text_summary(path, file_size)

            _rlog.info("core_service", f"PDF 读取成功: {path}, size={file_size}")

            return ToolResult(
                status=ToolStatus.SUCCESS,
                output=text_summary,
                metadata={
                    "path": str(path),
                    "media_type": "application/pdf",
                    "file_size": file_size,
                },
                content_parts=[
                    DocumentPart(
                        source_type="base64",
                        data=b64_str,
                        media_type="application/pdf",
                    ),
                ],
            )

        except Exception as e:
            error_msg = f"读取 PDF 异常: {path}, 错误: {str(e)}"
            _rlog.error("core_service", error_msg)
            return ToolResult(status=ToolStatus.FAILED, error=error_msg)

    @staticmethod
    def _extract_text_summary(path: Path, file_size: int) -> str:
        """尝试从 PDF 提取文本摘要，作为纯文本回退

        若 PyPDF2 未安装，返回基本文件信息。
        """
        try:
            from PyPDF2 import PdfReader

            reader = PdfReader(str(path))
            num_pages = len(reader.pages)
            # 提取前几页文本作为摘要
            text_parts = []
            for i, page in enumerate(reader.pages[:5]):
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(f"--- Page {i + 1} ---\n{page_text}")
            text_content = "\n".join(text_parts) if text_parts else "(无法提取文本)"
            if num_pages > 5:
                text_content += f"\n\n... (共 {num_pages} 页，仅展示前 5 页文本)"
            return f"PDF 文件: {path.name} ({file_size} bytes, {num_pages} 页)\n\n{text_content}"
        except ImportError:
            return f"PDF 文件已读取: {path.name} ({file_size} bytes)。文本提取需要 PyPDF2 依赖。"
        except Exception:
            return f"PDF 文件已读取: {path.name} ({file_size} bytes)。文本提取失败。"

    def get_description(self) -> Dict[str, Any]:
        return {
            "name": self.tool_name,
            "description": (
                "读取本地 PDF 文件。支持的 LLM 可直接查看 PDF 内容（图表、排版等）；"
                "不支持的 LLM 将获得提取的文本摘要。"
            ),
            "parameters": {
                "path": {
                    "type": "string",
                    "required": True,
                    "description": "PDF 文件路径",
                }
            },
            "examples": [
                {
                    "path": "docs/report.pdf",
                    "description": "读取 PDF 文档",
                }
            ],
        }
