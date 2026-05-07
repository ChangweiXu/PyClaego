"""Skill 类 - 表示单个技能"""

from pathlib import Path
from typing import Any

from .exceptions import SectionNotFoundError, SkillInvalidError, SkillLoadError
from .parser import extract_summary, parse_sections, parse_skill_file, validate_metadata

# SKILL 通用标准顶层字段（frontmatter 中直接支持的字段）
_STANDARD_FIELDS = frozenset({
    'argument-hint',
    'compatibility',
    'description',
    'disable-model-invocation',
    'license',
    'metadata',
    'name',
    'user-invocable',
})


class Skill:
    """表示单个技能
    
    功能：
    - 解析 SKILL.md 文件
    - 提供内容读取接口（完整/章节）
    - 懒加载机制
    
    遵循 SKILL 通用标准，frontmatter 仅支持以下顶层字段：
        argument-hint, compatibility, description, disable-model-invocation,
        license, metadata, name, user-invocable
    其余自定义字段（如 version, author, tags, priority, enabled）
    应放置在 metadata 子字典中。
    
    Attributes:
        name: 技能名称（来自标准字段 name）
        path: 技能目录路径
        description: 简短描述（来自标准字段 description）
        compatibility: 兼容性信息（来自标准字段 compatibility）
        argument_hint: 参数提示（来自标准字段 argument-hint）
        user_invocable: 是否可由用户直接调用（来自标准字段 user-invocable）
        disable_model_invocation: 是否禁止模型调用（来自标准字段 disable-model-invocation）
        license: 许可证（来自标准字段 license）
        metadata: 扩展元数据字典（来自标准字段 metadata，非标字段自动合并到此处）
    """
    
    def __init__(self, skill_path: Path):
        """初始化 Skill 对象
        
        Args:
            skill_path: 技能目录路径
            
        Raises:
            SkillInvalidError: 技能格式无效
            SkillLoadError: 技能加载失败
        """
        self.path = skill_path
        self.name = skill_path.name  # 默认使用目录名
        
        # SKILL 通用标准字段
        self.description: str = ""
        self.compatibility: str | None = None
        self.argument_hint: str | None = None
        self.user_invocable: bool = True
        self.disable_model_invocation: bool = False
        self.license: str | None = None
        
        # 扩展元数据（非标字段自动归入此处）
        self.metadata: dict[str, Any] = {}
        
        # 内容（懒加载）
        self._full_content: str | None = None
        self._sections: dict[str, str] | None = None
        self._frontmatter: dict[str, Any] = {}
        
        # 加载基本信息
        self._load_metadata()
    
    def _load_metadata(self):
        """加载 SKILL.md 的 frontmatter 元数据"""
        skill_md_path = self.path / "SKILL.md"
        
        if not skill_md_path.exists():
            raise SkillInvalidError(f"SKILL.md not found in {self.path}")
        
        try:
            metadata, content = parse_skill_file(skill_md_path)
            
            # 验证 metadata
            is_valid, error_msg = validate_metadata(metadata)
            if not is_valid:
                raise SkillInvalidError(f"Invalid SKILL.md in {self.path}: {error_msg}")
            
            # 保存 frontmatter
            self._frontmatter = metadata
            
            # 更新标准字段
            self.name = metadata.get('name', self.name)
            self.description = metadata.get('description', self.description)
            self.compatibility = metadata.get('compatibility', self.compatibility)
            self.argument_hint = metadata.get('argument-hint', self.argument_hint)
            self.user_invocable = metadata.get('user-invocable', self.user_invocable)
            self.disable_model_invocation = metadata.get(
                'disable-model-invocation', self.disable_model_invocation
            )
            self.license = metadata.get('license', self.license)
            
            # 合并 metadata 子字典（标准字段 metadata）
            explicit_metadata = metadata.get('metadata', {})
            if isinstance(explicit_metadata, dict):
                self.metadata.update(explicit_metadata)
            
            # 非标字段自动归入 self.metadata（向后兼容）
            for key, value in metadata.items():
                if key not in _STANDARD_FIELDS:
                    self.metadata.setdefault(key, value)
            
            # 如果没有 description，尝试从内容提取
            if not self.description and content:
                self.description = extract_summary(content, max_lines=2)
            
        except FileNotFoundError as e:
            raise SkillLoadError(f"Failed to load skill from {self.path}: {e}")
        except Exception as e:
            raise SkillLoadError(f"Failed to parse SKILL.md in {self.path}: {e}")
    
    def get_summary(self) -> str:
        """获取技能简介
        
        Returns:
            简介文本（通常是 description）
        """
        return self.description
    
    def get_full_content(self) -> str:
        """获取完整的 SKILL.md 内容（不包含 frontmatter）
        
        Returns:
            完整内容
        """
        if self._full_content is None:
            skill_md_path = self.path / "SKILL.md"
            try:
                _, content = parse_skill_file(skill_md_path)
                self._full_content = content
            except Exception as e:
                raise SkillLoadError(f"Failed to load content from {self.path}: {e}")
        
        return self._full_content
    
    def get_section(self, section_name: str) -> str:
        """获取指定章节的内容
        
        Args:
            section_name: 章节名称（二级标题）
            
        Returns:
            章节内容（包含标题）
            
        Raises:
            SectionNotFoundError: 章节不存在
        """
        sections = self._get_sections()
        
        if section_name not in sections:
            available = ', '.join(sections.keys())
            raise SectionNotFoundError(
                f"Section '{section_name}' not found in skill '{self.name}'. "
                f"Available sections: {available}"
            )
        
        return sections[section_name]
    
    def get_sections(self, section_names: list[str]) -> str:
        """获取多个章节的内容
        
        Args:
            section_names: 章节名称列表
            
        Returns:
            拼接后的内容（每个章节之间用空行分隔）
        """
        sections = self._get_sections()
        contents = []
        
        for section_name in section_names:
            if section_name in sections:
                contents.append(sections[section_name])
            else:
                # 跳过不存在的章节（静默处理）
                continue
        
        return '\n\n'.join(contents)
    
    def list_sections(self) -> list[str]:
        """列出所有章节名称
        
        Returns:
            章节名称列表
        """
        sections = self._get_sections()
        return list(sections.keys())
    
    def _get_sections(self) -> dict[str, str]:
        """获取章节字典（懒加载）
        
        Returns:
            section_name -> content
        """
        if self._sections is None:
            content = self.get_full_content()
            self._sections = parse_sections(content)
        
        return self._sections
    
    def to_dict(self) -> dict[str, Any]:
        """转换为字典格式（用于序列化）
        
        Returns:
            包含所有基本信息的字典（符合 SKILL 通用标准结构）
        """
        result: dict[str, Any] = {
            "name": self.name,
            "path": str(self.path),
            "description": self.description,
            "sections": self.list_sections(),
        }
        if self.compatibility is not None:
            result["compatibility"] = self.compatibility
        if self.argument_hint is not None:
            result["argument-hint"] = self.argument_hint
        if not self.user_invocable:
            result["user-invocable"] = self.user_invocable
        if self.disable_model_invocation:
            result["disable-model-invocation"] = self.disable_model_invocation
        if self.license is not None:
            result["license"] = self.license
        if self.metadata:
            result["metadata"] = self.metadata
        return result
    
    def validate(self) -> tuple[bool, str]:
        """验证 Skill 是否符合规范
        
        Returns:
            (是否有效, 错误信息)
        """
        # 检查必需字段
        if not self.name:
            return False, "Skill name is empty"
        
        if not self.description:
            return False, "Skill description is empty"
        
        # 检查 SKILL.md 是否存在
        skill_md_path = self.path / "SKILL.md"
        if not skill_md_path.exists():
            return False, "SKILL.md not found"
        
        # 尝试加载内容
        try:
            self.get_full_content()
        except Exception as e:
            return False, f"Failed to load content: {e}"
        
        return True, ""
    
    def __repr__(self) -> str:
        return f"<Skill name='{self.name}' user_invocable={self.user_invocable}>"
    
    def __str__(self) -> str:
        return f"{self.name} - {self.description}"
