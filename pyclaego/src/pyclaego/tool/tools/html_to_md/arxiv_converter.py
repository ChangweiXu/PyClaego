"""
arXiv HTML → Markdown 专用转换器。

复用技能脚本 arxiv_html_to_md.py 的 parse_html() + to_markdown() 管线。

arXiv HTML 的结构特征：
- 标题：h1.ltx_title_document
- 摘要：div#abstract1.ltx_abstract > p.ltx_p
- 正文：article.ltx_document > section.ltx_section
- 噪声：header.arxiv-html-header, nav.ltx_page_navbar, div.infobox 等

与 generic_converter 的接口保持一致：(html_raw, soup) -> md_text
"""

from pathlib import Path

from bs4 import BeautifulSoup

# ── 技能脚本路径推断 ──────────────────────────────────
# arxiv_html_to_md.py 位于 skills/builtin/arxiv-html-to-md-summary/scripts/
# 从当前文件向上推断 PyClaego 根目录或直接使用已知路径

_ARXIV_SCRIPT = Path(__file__).resolve().parents[7] / "skills" / "builtin" / \
    "arxiv-html-to-md-summary" / "scripts" / "arxiv_html_to_md.py"


def _load_arxiv_converter():
    """动态加载 arXiv 转换器函数。

    返回 (parse_html, to_markdown) 两个函数。
    若技能脚本不可用，返回 None 并降级到 generic。
    """
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "arxiv_html_to_md", str(_ARXIV_SCRIPT)
        )
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.parse_html, mod.to_markdown
    except (FileNotFoundError, ImportError, Exception):
        return None


def _arxiv_html_to_md(html_raw: str, soup: BeautifulSoup) -> str:
    """arXiv HTML → Markdown 转换。

    使用技能脚本的解析管线：
    1. parse_html(html_raw) → {title, abstract: [...], sections: [...]}
    2. to_markdown(data)  → Markdown 字符串

    Args:
        html_raw: 原始 HTML 字符串。
        soup:     BeautifulSoup 解析树（arXiv 转换器不使用此参数，
                  因为 parse_html 内部会重新解析，不共享 soup）。

    Returns:
        Markdown 文本。
    """
    funcs = _load_arxiv_converter()
    if funcs is None:
        # 降级：arXiv 脚本不可用时回退到 generic
        from .generic_converter import _generic_html_to_md
        return _generic_html_to_md(html_raw, soup)

    parse_html_fn, to_markdown_fn = funcs
    data = parse_html_fn(html_raw)
    md = to_markdown_fn(data)
    return md


# ── 注册 ──────────────────────────────────────────────

from . import register_converter  # noqa: E402

register_converter("arxiv", _arxiv_html_to_md)
