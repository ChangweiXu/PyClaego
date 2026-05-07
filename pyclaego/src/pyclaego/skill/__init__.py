"""Skill 模块 - 技能管理系统

提供技能的加载、管理和内容组装功能。

主要类：
- Skill: 表示单个技能
- SkillManager: 技能管理器

使用示例：
    >>> from pyclaego.src.skill import SkillManager
    >>> from pyclaego.config import get_config
    >>> 
    >>> config = get_config()
    >>> skill_config = config.get("skill", {})
    >>> skill_manager = SkillManager(skill_config)
    >>> 
    >>> # 加载技能
    >>> success, failed = skill_manager.load_skills()
    >>> 
    >>> # 列出技能
    >>> skills = skill_manager.list_skills(enabled_only=True)
    >>> 
    >>> # 获取技能内容
    >>> content = skill_manager.get_skill_content("python_best_practices")
    >>> 
    >>> # 组装上下文
    >>> context = skill_manager.assemble_context(
    ...     ["python_best_practices", "code_review"],
    ...     include_full_content=False
    ... )
"""

# 便捷函数
def get_skill_manager():
    """获取技能管理器单例"""
    return SkillManager.get_instance()

from .exceptions import (
    SectionNotFoundError,
    SkillError,
    SkillInvalidError,
    SkillLoadError,
    SkillNotFoundError,
)
from .manager import SkillManager
from .skill import Skill

__all__ = [
    "SectionNotFoundError",
    "Skill",
    "SkillError",
    "SkillInvalidError",
    "SkillLoadError",
    "SkillManager",
    "SkillNotFoundError",
    "get_skill_manager",
]

__version__ = "0.1.0"
