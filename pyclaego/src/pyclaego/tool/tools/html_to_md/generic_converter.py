"""
通用 HTML → Markdown 转换器。

适用场景：博客文章、新闻页面、技术文档等非结构化或不具备
知名站点专用解析器的网页。

核心策略：
1. 噪声剔除 → 2. 语义树遍历 → 3. Markdown 拼接 → 4. 后处理
"""

import re

from bs4 import BeautifulSoup, NavigableString, Tag

# ── 噪声选择器 ────────────────────────────────────────

NOISE_SELECTORS = [
    "script", "style", "noscript",
    "nav", "footer", "header",
    "aside", '[role="complementary"]',
    ".sidebar", ".nav", ".footer", ".header",
    ".comment", ".comments",
    ".advertisement", ".ads",
    ".cookie-banner", ".cookie-consent",
    "form", "iframe",
]

# 语义标签 → 对应转换方法的映射
_SEMANTIC_MAP = {
    "h1": "_heading",
    "h2": "_heading",
    "h3": "_heading",
    "h4": "_heading",
    "h5": "_heading",
    "h6": "_heading",
    "p":  "_paragraph",
    "ul": "_list",
    "ol": "_list",
    "li": "_list_item",
    "a":  "_link",
    "strong": "_bold",
    "b":   "_bold",
    "em":  "_italic",
    "i":   "_italic",
    "code": "_code_inline",
    "pre": "_code_block",
    "blockquote": "_blockquote",
    "table": "_table",
    "img": "_image",
    "hr": "_hr",
    "br": "_line_break",
}


def _clean_text(s: str) -> str:
    """压缩空白，去除首尾空格。"""
    return re.sub(r'\s+', ' ', s).strip()


def _count_leading_spaces(s: str) -> int:
    """计算字符串开头的空格数（用于列表缩进）。"""
    return len(s) - len(s.lstrip())


def _generic_html_to_md(html_raw: str, soup: BeautifulSoup) -> str:
    """通用 HTML → Markdown 转换。

    Args:
        html_raw: 原始 HTML 字符串（仅用于异常日志）。
        soup:     BeautifulSoup 解析后的文档树。

    Returns:
        Markdown 文本。
    """
    # 1. 复制 soup 避免副作用
    from copy import deepcopy
    soup = deepcopy(soup)

    # 2. 噪声剔除
    for selector in NOISE_SELECTORS:
        for tag in soup.select(selector):
            tag.decompose()

    # 3. 定位内容根节点
    root = _find_content_root(soup)

    # 4. 递归转换
    ctx = _ConverterContext()
    md_body = _convert_node(root, ctx)

    # 5. 后处理
    return _post_process(md_body)


# ── 内容根节点定位 ────────────────────────────────────

def _find_content_root(soup: BeautifulSoup) -> Tag:
    """启发式定位内容根节点。

    优先级：
    1. <article>
    2. <main>
    3. <div> 中 class/id 含 content/article/main/post/body
    4. <body>
    """
    for selector in [
        "article", "main",
        '[role="main"]',
    ]:
        tag = soup.select_one(selector)
        if tag:
            return tag

    # 启发式 class/id 匹配
    content_pattern = re.compile(
        r'(content|article|main|post|body|entry)', re.I
    )
    for div in soup.find_all("div"):
        div_id = (div.get("id") or "") + " " + " ".join(div.get("class") or [])
        if content_pattern.search(div_id):
            return div

    body = soup.find("body")
    return body if body else soup


# ── 转换上下文（状态管理）─────────────────────────────

class _ConverterContext:
    """在递归转换过程中维护的全局状态。

    可扩展字段：
    - list_counter: ol 的序号计数器
    - indent_level: 当前缩进层级（列表嵌套）
    - pending_newlines: 待输出的空白行数（防重复）
    """

    def __init__(self):
        self.list_counter: list[int] = []        # ol 序号栈
        self.indent_level: int = 0               # 当前列表嵌套深度
        self.pending_newlines: int = 0           # 待 flush 的换行数
        self.result: list[str] = []              # 输出行缓冲

    def emit(self, text: str) -> None:
        """输出一行 Markdown。"""
        # flush pending newlines
        if self.pending_newlines > 0:
            self.result.extend([""] * self.pending_newlines)
            self.pending_newlines = 0
        self.result.append(text)

    def blank_line(self, count: int = 1) -> None:
        """请求输出空白行（去重合并）。"""
        self.pending_newlines = max(self.pending_newlines, count)


# ── 递归转换核心 ──────────────────────────────────────

def _convert_node(node, ctx: _ConverterContext) -> str:
    """递归转换单个 DOM 节点为 Markdown 字符串。

    对于只需内联文本的调用方，直接返回字符串；
    对于块级元素，通过 ctx.emit() 追加行缓冲。
    """
    if isinstance(node, NavigableString):
        text = _clean_text(str(node))
        if text:
            return text
        return ""

    if not isinstance(node, Tag):
        return ""

    # 调度到具体转换方法
    tag_name = node.name.lower()
    method_name = _SEMANTIC_MAP.get(tag_name)

    if method_name:
        method = getattr(_ConverterMethods, method_name, None)
        if method:
            return method(node, ctx)

    # 未识别的标签 → 递归子节点（保留内联文本）
    return _convert_children(node, ctx)


def _convert_children(node: Tag, ctx: _ConverterContext) -> str:
    """递归转换所有子节点，拼接为单个字符串。"""
    parts = []
    for child in node.children:
        part = _convert_node(child, ctx)
        if part:
            parts.append(part)
    return " ".join(parts) if parts else ""


# ── 转换方法集（@staticmethod 便于外部覆盖/扩展）───────

class _ConverterMethods:
    """各 HTML 标签 → Markdown 的转换方法。

    使用 @staticmethod 以便子类/插件覆盖单个方法。
    """

    # ── 标题 ──────────────────────────────────────────

    @staticmethod
    def _heading(tag: Tag, ctx: _ConverterContext) -> str:
        level = int(tag.name[1])  # h1 → 1
        text = _convert_children(tag, ctx)
        if not text:
            return ""
        ctx.blank_line()
        ctx.emit(f"{'#' * level} {text}")
        ctx.blank_line()
        return ""  # 已通过 ctx.emit 输出

    # ── 段落 ──────────────────────────────────────────

    @staticmethod
    def _paragraph(tag: Tag, ctx: _ConverterContext) -> str:
        text = _convert_children(tag, ctx)
        if not text:
            return ""
        ctx.blank_line()
        ctx.emit(text)
        ctx.blank_line()
        return ""

    # ── 列表 ──────────────────────────────────────────

    @staticmethod
    def _list(tag: Tag, ctx: _ConverterContext) -> str:
        is_ordered = (tag.name == "ol")
        if is_ordered:
            ctx.list_counter.append(1)
        ctx.indent_level += 1
        ctx.blank_line()

        for child in tag.children:
            _convert_node(child, ctx)

        ctx.indent_level -= 1
        if is_ordered:
            ctx.list_counter.pop()
        ctx.blank_line()
        return ""

    @staticmethod
    def _list_item(tag: Tag, ctx: _ConverterContext) -> str:
        indent = "  " * max(0, ctx.indent_level - 1)
        if ctx.list_counter:
            num = ctx.list_counter[-1]
            ctx.list_counter[-1] += 1
            prefix = f"{indent}{num}."
        else:
            prefix = f"{indent}-"

        # 处理 <li> 的直接文本 + 内联子节点
        text = _clean_text(tag.get_text(" ", strip=True))
        if text:
            ctx.emit(f"{prefix} {text}")
        return ""

    # ── 内联格式 ──────────────────────────────────────

    @staticmethod
    def _link(tag: Tag, ctx: _ConverterContext) -> str:
        text = _convert_children(tag, ctx)
        href = tag.get("href", "")
        if not text:
            text = href
        if href and href != text:
            return f"[{text}]({href})"
        return text

    @staticmethod
    def _bold(tag: Tag, ctx: _ConverterContext) -> str:
        text = _convert_children(tag, ctx)
        return f"**{text}**" if text else ""

    @staticmethod
    def _italic(tag: Tag, ctx: _ConverterContext) -> str:
        text = _convert_children(tag, ctx)
        return f"*{text}*" if text else ""

    @staticmethod
    def _code_inline(tag: Tag, ctx: _ConverterContext) -> str:
        text = _clean_text(tag.get_text())
        if not text:
            return ""
        # 单行用 `，多行用 ``（防止嵌套冲突）
        if "\n" in text:
            return f"``{text}``"
        return f"`{text}`"

    # ── 代码块 ────────────────────────────────────────

    @staticmethod
    def _code_block(tag: Tag, ctx: _ConverterContext) -> str:
        code_tag = tag.find("code")
        lang = ""
        code_text = ""
        if code_tag:
            classes = code_tag.get("class", [])
            for c in classes:
                if c.startswith("language-") or c.startswith("lang-"):
                    lang = c.split("-", 1)[1]
                    break
            code_text = code_tag.get_text()
        else:
            code_text = tag.get_text()

        if not code_text.strip():
            return ""

        ctx.blank_line()
        ctx.emit(f"```{lang}")
        for line in code_text.split("\n"):
            ctx.emit(line)
        ctx.emit("```")
        ctx.blank_line()
        return ""

    # ── 引用块 ────────────────────────────────────────

    @staticmethod
    def _blockquote(tag: Tag, ctx: _ConverterContext) -> str:
        text = _convert_children(tag, ctx)
        if not text:
            return ""
        ctx.blank_line()
        for line in text.split("\n"):
            ctx.emit(f"> {line}")
        ctx.blank_line()
        return ""

    # ── 表格 ──────────────────────────────────────────

    @staticmethod
    def _table(tag: Tag, ctx: _ConverterContext) -> str:
        # TODO: 完整实现表格转换（含表头对齐检测）
        # 当前简易实现：跳过表格，输出占位提示
        ctx.blank_line()
        caption = tag.find("caption")
        label = caption.get_text(strip=True) if caption else "表格"
        ctx.emit(f"> *[{label} — 请查看原文]*")
        ctx.blank_line()
        return ""

    # ── 图片 ──────────────────────────────────────────

    @staticmethod
    def _image(tag: Tag, ctx: _ConverterContext) -> str:
        alt = tag.get("alt", "")
        src = tag.get("src", "")
        if not src:
            return ""
        return f"![{alt}]({src})"

    # ── 分隔线 ────────────────────────────────────────

    @staticmethod
    def _hr(tag: Tag, ctx: _ConverterContext) -> str:
        ctx.blank_line()
        ctx.emit("---")
        ctx.blank_line()
        return ""

    # ── 换行 ──────────────────────────────────────────

    @staticmethod
    def _line_break(tag: Tag, ctx: _ConverterContext) -> str:
        return "\n"


# ── 后处理 ────────────────────────────────────────────

def _post_process(md_body: str) -> str:
    """Markdown 后处理。

    1. 压缩连续空白行（>2 → 2）
    2. 去除首尾空白行
    """
    md_body = re.sub(r'\n{3,}', '\n\n', md_body)
    md_body = md_body.strip() + "\n"
    return md_body


# ── 注册 ──────────────────────────────────────────────

from . import register_converter  # noqa: E402 延迟 import 避免循环

register_converter("generic", _generic_html_to_md)
