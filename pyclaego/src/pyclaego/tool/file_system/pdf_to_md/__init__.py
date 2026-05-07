"""
PDF → Markdown 转换子模块。

提供从 PDF 文件到格式化 Markdown 文档的完整转换管线:
1. 文本提取（可插拔后端）
2. Markdown 格式化（标题检测、段落合并、页码过滤）
"""

from .extractor import PdfExtractionError, extract_text
from .formatter import format_markdown, pages_to_markdown

__all__ = [
    "PdfExtractionError",
    "extract_text",
    "format_markdown",
    "pages_to_markdown",
]
