"""Skill 模块自定义异常"""


class SkillError(Exception):
    """Skill 相关的基础异常"""
    pass


class SkillNotFoundError(SkillError):
    """技能不存在"""
    pass


class SkillInvalidError(SkillError):
    """技能格式无效"""
    pass


class SkillLoadError(SkillError):
    """技能加载失败"""
    pass


class SectionNotFoundError(SkillError):
    """章节不存在"""
    pass
