"""SKILL.md 解析器 - 解析 frontmatter 和章节"""

import re
from pathlib import Path
from typing import Dict, Any, Tuple, List, Optional


def parse_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    """解析 YAML frontmatter
    
    Args:
        content: 完整的文件内容
        
    Returns:
        (frontmatter_dict, remaining_content)
        
    Examples:
        >>> content = '''---
        ... name: test
        ... version: 1.0.0
        ... ---
        ... # Content'''
        >>> metadata, body = parse_frontmatter(content)
        >>> metadata['name']
        'test'
    """
    # 匹配 frontmatter 格式: ---\n...\n---
    pattern = r'^---\s*\n(.*?)\n---\s*\n(.*)$'
    match = re.match(pattern, content, re.DOTALL)
    
    if not match:
        # 没有 frontmatter，返回空字典和原内容
        return {}, content
    
    frontmatter_str = match.group(1)
    remaining_content = match.group(2)
    
    # 简单的 YAML 解析（仅支持基本键值对和列表）
    metadata = {}
    current_key = None
    current_list = []
    in_list = False
    
    for line in frontmatter_str.split('\n'):
        line = line.strip()
        if not line:
            continue
        
        # 检查是否是键值对
        if ':' in line and not line.startswith('-'):
            if in_list and current_key:
                metadata[current_key] = current_list
                current_list = []
                in_list = False
            
            key, value = line.split(':', 1)
            key = key.strip()
            value = value.strip()
            
            # 检查是否是列表开始
            if value.startswith('[') and value.endswith(']'):
                # 内联列表 [item1, item2]
                list_content = value[1:-1]
                metadata[key] = [item.strip().strip('"\'') for item in list_content.split(',') if item.strip()]
            elif not value:
                # 可能是多行列表的开始
                current_key = key
                in_list = True
            else:
                # 普通值
                # 移除引号
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                elif value.startswith("'") and value.endswith("'"):
                    value = value[1:-1]
                
                # 类型转换
                if value.lower() == 'true':
                    value = True
                elif value.lower() == 'false':
                    value = False
                elif value.isdigit():
                    value = int(value)
                elif value.replace('.', '', 1).isdigit():
                    value = float(value)
                
                metadata[key] = value
        
        elif line.startswith('-') and in_list:
            # 列表项
            item = line[1:].strip().strip('"\'')
            current_list.append(item)
    
    # 处理最后的列表
    if in_list and current_key:
        metadata[current_key] = current_list
    
    return metadata, remaining_content


def parse_sections(content: str) -> Dict[str, str]:
    """解析 Markdown 章节
    
    Args:
        content: Markdown 内容
        
    Returns:
        section_name -> section_content (包含标题行)
        
    Examples:
        >>> content = '''# Title
        ... ## Section 1
        ... Content 1
        ... ## Section 2
        ... Content 2'''
        >>> sections = parse_sections(content)
        >>> 'Section 1' in sections
        True
    """
    sections = {}
    lines = content.split('\n')
    current_section = None
    current_content = []
    
    for line in lines:
        # 匹配 ## 标题（二级标题）
        match = re.match(r'^##\s+(.+)$', line)
        if match:
            # 保存上一个章节
            if current_section:
                sections[current_section] = '\n'.join(current_content)
            
            # 开始新章节
            current_section = match.group(1).strip()
            current_content = [line]  # 包含标题行
        elif current_section:
            current_content.append(line)
    
    # 保存最后一个章节
    if current_section:
        sections[current_section] = '\n'.join(current_content)
    
    return sections


def extract_summary(content: str, max_lines: int = 5) -> str:
    """从内容中提取摘要（前几行非空文本）
    
    Args:
        content: Markdown 内容
        max_lines: 最大行数
        
    Returns:
        摘要文本
    """
    lines = []
    for line in content.split('\n'):
        line = line.strip()
        # 跳过空行和一级标题
        if not line or line.startswith('# '):
            continue
        # 跳过二级标题（通常是章节开始）
        if line.startswith('## '):
            break
        lines.append(line)
        if len(lines) >= max_lines:
            break
    
    return '\n'.join(lines)


def parse_skill_file(file_path: Path) -> Tuple[Dict[str, Any], str]:
    """解析完整的 SKILL.md 文件
    
    Args:
        file_path: SKILL.md 文件路径
        
    Returns:
        (metadata, content)
        
    Raises:
        FileNotFoundError: 文件不存在
        UnicodeDecodeError: 文件编码错误
    """
    if not file_path.exists():
        raise FileNotFoundError(f"SKILL.md not found: {file_path}")
    
    with open(file_path, 'r', encoding='utf-8') as f:
        raw_content = f.read()
    
    metadata, content = parse_frontmatter(raw_content)
    
    return metadata, content


def validate_metadata(metadata: Dict[str, Any]) -> Tuple[bool, str]:
    """验证 metadata 是否包含必需字段
    
    Args:
        metadata: frontmatter 元数据
        
    Returns:
        (是否有效, 错误信息)
    """
    required_fields = ['name', 'description']
    
    for field in required_fields:
        if field not in metadata:
            return False, f"Missing required field: {field}"
    
    # 检查字段类型
    if not isinstance(metadata.get('name'), str):
        return False, "Field 'name' must be a string"
    
    if not isinstance(metadata.get('description'), str):
        return False, "Field 'description' must be a string"
    
    return True, ""
