"""
PDF 文本提取器 — 可插拔后端

支持:
- PyPDF2（轻量，默认）
- pdfplumber（可选，表格/布局更好）
"""

from __future__ import annotations

from pathlib import Path

# ── 类型 ──────────────────────────────────────────────

PageText = tuple[int, str]  # (page_number, text)


# ── 后端接口 ──────────────────────────────────────────

class PdfExtractionError(Exception):
    """PDF 文本提取失败"""


def extract_text_pypdf2(pdf_path: Path) -> tuple[list[PageText], int]:
    """使用 PyPDF2 提取文本。

    Returns:
        (pages, page_count): pages 为 [(page_num, text), ...]
    """
    try:
        from PyPDF2 import PdfReader
    except ImportError:
        raise PdfExtractionError("PyPDF2 未安装。请执行: pip install PyPDF2")

    reader = PdfReader(str(pdf_path))
    num_pages = len(reader.pages)
    pages: list[PageText] = []

    for i, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ""
        pages.append((i, text))

    if not pages:
        raise PdfExtractionError(f"PDF 无页面或无法提取文本: {pdf_path}")

    return pages, num_pages


def extract_text_pdfplumber(pdf_path: Path) -> tuple[list[PageText], int]:
    """使用 pdfplumber 提取文本（更好的表格和布局保留）。

    Returns:
        (pages, page_count): pages 为 [(page_num, text), ...]
    """
    try:
        import pdfplumber
    except ImportError:
        raise PdfExtractionError("pdfplumber 未安装。请执行: pip install pdfplumber")

    pages: list[PageText] = []

    with pdfplumber.open(str(pdf_path)) as pdf:
        num_pages = len(pdf.pages)
        for i, page in enumerate(pdf.pages, 1):
            text = page.extract_text() or ""
            pages.append((i, text))

    if not pages:
        raise PdfExtractionError(f"PDF 无页面或无法提取文本: {pdf_path}")

    return pages, num_pages


# ── 后端注册表 ────────────────────────────────────────

_BACKENDS = {
    "pypdf2": extract_text_pypdf2,
    "pdfplumber": extract_text_pdfplumber,
}

_DEFAULT_BACKEND_ORDER = ("pdfplumber", "pypdf2")


# ── 公共 API ──────────────────────────────────────────

def extract_text(
    pdf_path: Path,
    backend: str | None = None,
) -> tuple[list[PageText], int]:
    """从 PDF 提取逐页文本。

    自动回退：指定后端不可用时尝试默认顺序中的下一个。
    若所有后端均不可用，抛出 PdfExtractionError。

    Args:
        pdf_path: PDF 文件路径。
        backend:  指定后端名称 ("pypdf2" / "pdfplumber")。
                  None 则按默认顺序自动选择。

    Returns:
        (pages, page_count):
          - pages: [(1, "page1 text"), (2, "page2 text"), ...]
          - page_count: 总页数

    Raises:
        PdfExtractionError: 所有后端均提取失败。
    """
    candidates = [backend] if backend else list(_DEFAULT_BACKEND_ORDER)

    last_error: Exception | None = None

    for name in candidates:
        if name not in _BACKENDS:
            continue
        try:
            return _BACKENDS[name](pdf_path)
        except ImportError:
            last_error = PdfExtractionError(f"后端 {name!r} 所需依赖未安装")
            continue
        except PdfExtractionError as e:
            last_error = e
            continue
        except Exception as e:
            last_error = e
            continue

    if last_error:
        raise PdfExtractionError(
            f"PDF 文本提取失败 ({pdf_path})，所有后端均不可用: {last_error}"
        )
    raise PdfExtractionError("PDF 文本提取失败: 无可用后端")
