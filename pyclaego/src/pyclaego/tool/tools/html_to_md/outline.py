"""
Markdown Outline 提取工具。

从 Markdown 文本中提取 ATX-style heading（`# Title`）树结构。
"""

import re

# ── 正则 ──────────────────────────────────────────────

# 匹配行首的 `#` 标题（不匹配代码块中的 `#` 注释）
_HEADING_RE = re.compile(r'^(#{1,6})\s+(.+)$')


def extract_outline(md_text: str) -> list[dict]:
    """从 Markdown 文本中提取标题结构树。

    Args:
        md_text: 完整 Markdown 文本。

    Returns:
        outline 列表，每项格式：
        {
            "level": int,    # 1-6，`#` 个数
            "title": str,    # 标题文本（去首尾空白）
            "line":  int,    # 行号（从 1 开始）
        }

    示例：
        >>> extract_outline("# Abstract\\n\\n## 1. Intro\\n")
        [
            {"level": 1, "title": "Abstract", "line": 1},
            {"level": 2, "title": "1. Intro", "line": 3},
        ]

    注意：
        - 只解析 ATX-style heading（`# Title`），不处理 Setext-style（`===` / `---`）。
        - 代码块中以 `#` 开头的注释不会被误匹配（前提是代码块正确缩进）。
    """
    outline = []
    for i, line in enumerate(md_text.split('\n'), 1):
        stripped = line.lstrip()
        if not stripped.startswith('#'):
            continue

        match = _HEADING_RE.match(stripped)
        if match:
            outline.append({
                'level': len(match.group(1)),
                'title': match.group(2).strip(),
                'line': i,
            })

    return outline


def find_section_range(
    md_text: str,
    section_title: str,
    outline: list[dict] | None = None,
) -> tuple[int, int]:
    """根据章节标题查找其在 Markdown 文本中的行号范围。

    范围从该标题行开始，到同层级或更高层级标题之前结束
    （即包含所有子章节）。

    Args:
        md_text:        完整 Markdown 文本。
        section_title:  要查找的章节标题（精确匹配）。
        outline:        预计算的 outline（可选，传入可避免重复提取）。

    Returns:
        (start_line, end_line)，均为 1-based 行号。
        end_line 可能为 None 表示到文件末尾。
        若未找到，返回 (None, None)。

    Note:
        此函数为后续的 `md_read_section` 工具提供支持。
    """
    if outline is None:
        outline = extract_outline(md_text)

    # 查找目标标题在 outline 中的位置
    target_idx = None
    for idx, item in enumerate(outline):
        if item["title"] == section_title:
            target_idx = idx
            break

    if target_idx is None:
        return (None, None)

    target = outline[target_idx]
    start_line = target["line"]

    # 查找下一个同级或更高级标题
    end_line = None
    for idx in range(target_idx + 1, len(outline)):
        if outline[idx]["level"] <= target["level"]:
            end_line = outline[idx]["line"] - 1
            break

    return (start_line, end_line)


__all__ = [
    "extract_outline",
    "find_section_range",
]
