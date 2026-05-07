"""
PDF 原始文本 → Markdown 格式化器

将逐页提取的 PDF 文本转换为结构化 Markdown:
- 标题检测（数字编号、大写短行、加粗模式）
- 段落合并
- 页码/页眉页脚过滤
- 列表检测
"""

from __future__ import annotations

import re
from pathlib import Path

from .extractor import PageText

# ── 正则 ──────────────────────────────────────────────

# 常见章节编号模式
_CHAPTER_NUM_RE = re.compile(
    r'^(第[一二三四五六七八九十百千万\d]+[章节部分篇]|'
    r'(Chapter|Section|Part)\s+\d+|'
    r'\d+(\.\d+)*[\s.、．]|'
    r'[IVXLCDM]+\.)'
)

# 全大写短行（可能是标题），至少 3 个大写字母，不超过 80 字符
_ALL_CAPS_HEADING_RE = re.compile(r'^[A-Z][A-Z\s\-—–:,;/&(){}\[\]\'"`]{2,79}$')

# 序号列表模式
_LIST_PATTERNS = [
    re.compile(r'^(\d+[\.\)、．])\s+'),         # 1. 1) 1、
    re.compile(r'^([\(（]\d+[\)）])\s*'),        # (1) （1）
    re.compile(r'^([•·▪▸►◆●○■□▪▹•·\-–—\*])\s+'),  # bullets
    re.compile(r'^([a-zA-Z][\.\)])\s+'),         # a. b)
]

# 序号列表模式（仅数字编号，可能和标题冲突）
_NUMBERED_LIST_RE = re.compile(r'^(\d+[\.\)、．])\s+')

# 括号编号列表（如 (1), （2））
_PAREN_NUM_LIST_RE = re.compile(r'^([\(（]\d+[\)）])\s*')

# 符号/无序列表模式（不会和标题冲突）
_BULLET_LIST_RE = re.compile(r'^([•·▪▸►◆●○■□▪▹•·\-–—\*])\s+')

# 页码模式（孤立的数字行，通常 1-4 位数字）
_PAGE_NUM_RE = re.compile(r'^\s*\d{1,4}\s*$')

# 页眉页脚常见模式（重复出现的短行）
_HEADER_FOOTER_MIN_LEN = 3
_HEADER_FOOTER_MAX_LEN = 120


# ── 文本清洗 ──────────────────────────────────────────

def _filter_repeated_lines(lines: list[str]) -> list[str]:
    """过滤每页重复出现的页眉/页脚行。

    策略：统计所有非空行出现次数，出现次数 >= 总页数一半且长度在
    页眉页脚范围内的行视为重复行并删除。
    """
    if len(lines) < 10:
        return lines

    from collections import Counter

    # 统计非空行的出现次数（精确匹配）
    non_empty = [l for l in lines if l.strip()]
    if not non_empty:
        return lines

    counter = Counter(non_empty)
    # 阈值：至少出现 page_count/2 次（假设半页以上都有）
    threshold = max(3, len(lines) // 20)  # 至少 3 次

    repeated = {
        line
        for line, count in counter.items()
        if count >= threshold
        and _HEADER_FOOTER_MIN_LEN <= len(line.strip()) <= _HEADER_FOOTER_MAX_LEN
    }

    if not repeated:
        return lines

    return [l for l in lines if l not in repeated]


def _is_page_number(line: str) -> bool:
    """判断是否是孤立的页码行"""
    stripped = line.strip()
    if not stripped:
        return False
    return bool(_PAGE_NUM_RE.match(stripped))


def _is_heading_candidate(line: str) -> tuple[bool, int]:
    """判断一行是否可能是章节标题。

    Returns:
        (is_heading, suggested_level): 是否为标题, 建议的 heading 层级 (1-4)
    """
    stripped = line.strip()
    if not stripped:
        return False, 0

    # 已经是 ATX heading
    if stripped.startswith('#'):
        return True, len(stripped) - len(stripped.lstrip('#'))

    # 纯数字页码
    if _is_page_number(stripped):
        return False, 0

    # 数字编号开头（如 "1.", "2.3", "第一章"）
    if _CHAPTER_NUM_RE.match(stripped):
        # 判断层级：点数越多层级越深
        match = _CHAPTER_NUM_RE.match(stripped)
        num_text = match.group(0).rstrip('.、． ')
        dots = num_text.count('.')
        if dots == 0:
            return True, 1
        elif dots == 1:
            return True, 2
        else:
            return True, min(dots + 1, 4)

    # 全大写短行（可能是英文标题）
    if _ALL_CAPS_HEADING_RE.match(stripped) and len(stripped) <= 80:
        return True, 2

    # 加粗/编号后的短行（以中文/英文开头，较短）
    # 排除常见的列表标记符和括号
    _bullet_chars = {'•', '·', '▪', '▸', '►', '◆', '●', '○', '■', '□', '▹', '-', '–', '—', '*'}
    if (
        len(stripped) <= 60
        and not stripped.endswith(('.', '。', ',', '，', ';', '；'))
        and stripped[0] not in _bullet_chars
        and stripped[0] not in ('(', '（')
        and not stripped[0].isdigit()
    ):
        return True, 3

    return False, 0


# ── 主格式化逻辑 ──────────────────────────────────────

def format_markdown(
    pages: list[PageText],
    pdf_name: str = "",
) -> str:
    """将逐页提取的 PDF 文本格式化为 Markdown。

    Args:
        pages:    [(page_num, text), ...] 逐页文本。
        pdf_name: PDF 文件名（用作文档顶级标题）。

    Returns:
        格式化后的 Markdown 字符串。
    """
    all_lines: list[str] = []

    for page_num, text in pages:
        if not text.strip():
            # 空白页也用 page marker 标记
            all_lines.append(f"<!-- page {page_num} -->")
            all_lines.append("")
            continue

        page_lines = text.split('\n')
        # 添加页码标记
        all_lines.append(f"<!-- page {page_num} -->")
        all_lines.extend(page_lines)
        all_lines.append("")  # 页间空行

    # 过滤重复的页眉页脚
    all_lines = _filter_repeated_lines(all_lines)

    # ── 格式化主循环 ──
    output: list[str] = []

    if pdf_name:
        # 用文件名作为文档标题
        output.append(f"# {Path(pdf_name).stem}")
        output.append("")

    prev_empty = False  # 追踪连续空行

    for line in all_lines:
        stripped = line.strip()

        # 页标记保留为 HTML 注释
        if stripped.startswith('<!-- page ') and stripped.endswith('-->'):
            output.append(stripped)
            output.append("")
            prev_empty = True
            continue

        # 空行 → 段落分隔
        if not stripped:
            if not prev_empty:
                output.append("")
                prev_empty = True
            continue
        prev_empty = False

        # 页码过滤
        if _is_page_number(stripped):
            continue

        # 无序/符号列表检测（优先于标题，bullet 不会和标题冲突）
        bullet_match = _BULLET_LIST_RE.match(stripped)
        if bullet_match:
            output.append(f"- {stripped[bullet_match.end():].strip()}")
            continue

        # 括号编号列表检测（优先于标题，(1) 等不会和标题冲突）
        paren_match = _PAREN_NUM_LIST_RE.match(stripped)
        if paren_match:
            output.append(f"- {stripped[paren_match.end():].strip()}")
            continue

        # 标题检测
        is_heading, level = _is_heading_candidate(stripped)
        if is_heading and level >= 1:
            heading_marker = '#' * min(level, 6)
            output.append(f"{heading_marker} {stripped}")
            output.append("")
            continue

        # 数字编号列表检测（标题检测之后，避免 "1. Introduction" 误判）
        num_match = _NUMBERED_LIST_RE.match(stripped)
        if num_match and not _CHAPTER_NUM_RE.match(stripped):
            output.append(f"- {stripped[num_match.end():].strip()}")
            continue

        # 字母列表标记：a), a.
        for pattern in _LIST_PATTERNS:
            if pattern is _LIST_PATTERNS[3]:  # a. / a) pattern
                m = pattern.match(stripped)
                if m:
                    output.append(f"- {stripped[m.end():].strip()}")
                    break
        else:
            # 普通段落行
            output.append(stripped)

    # ── 后处理：连接被异常分割的段落 ──
    result = _merge_broken_paragraphs('\n'.join(output))

    # ── 后处理：折叠多余空行 ──
    result = _collapse_blank_lines(result)

    return result


def _collapse_blank_lines(text: str) -> str:
    """将多个连续空行折叠为单个空行。"""
    lines = text.split('\n')
    out: list[str] = []
    prev_blank = False
    for line in lines:
        is_blank = not line.strip()
        if is_blank:
            if not prev_blank:
                out.append('')
            prev_blank = True
        else:
            out.append(line)
            prev_blank = False
    return '\n'.join(out)


def _merge_broken_paragraphs(text: str) -> str:
    """合并被 PDF 提取意外断开的段落。

    规则：连续的非空、非标题、非注释、非列表行，合并为同一段落。
    """
    lines = text.split('\n')
    result: list[str] = []
    para_buffer: list[str] = []

    def _flush_para():
        if para_buffer:
            result.append(' '.join(para_buffer))
            para_buffer.clear()

    for line in lines:
        stripped = line.strip()

        # 空行 → 段落结束
        if not stripped:
            _flush_para()
            result.append('')
            continue

        # HTML 注释、标题、列表项 → 段落结束
        if (stripped.startswith('<!--') or
                stripped.startswith('#') or
                stripped.startswith('- ')):
            _flush_para()
            result.append(line)
            continue

        # 普通文本 → 累积到当前段落
        para_buffer.append(stripped)

    _flush_para()

    return '\n'.join(result)


# ── 便捷函数 ──────────────────────────────────────────

def pages_to_markdown(
    pages: list[PageText],
    pdf_path: Path,
) -> str:
    """从提取的页面文本直接生成 Markdown。

    Args:
        pages:    逐页文本。
        pdf_path: PDF 文件路径（用于提取文件名）。

    Returns:
        格式化后的 Markdown 字符串。
    """
    return format_markdown(pages, pdf_name=pdf_path.name)
