"""SkillManager 类 - 技能管理器（单例模式）"""

import asyncio
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from .skill import Skill
from .exceptions import SkillNotFoundError, SkillLoadError
from ..config import get_config
from ..logging import get_running_log

_rlog = get_running_log()


class SkillManager:
    """技能管理器（单例模式）
    
    功能：
    - 从多个配置目录加载全局共享技能
    - 按需加载每个 Session 独有的技能
    - 管理技能生命周期
    - 提供技能检索和内容组装接口
    
    Attributes:
        skill_directories: 全局技能目录列表
    """
    
    _instance = None
    _lock = asyncio.Lock()
    
    def __new__(cls):
        """实现单例模式"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        """初始化 SkillManager（仅在第一次创建时执行）"""
        if self._initialized:
            return
        
        # 读取配置
        config = get_config()
        skill_config = config.get("skill", {})
        
        # 配置
        directories = skill_config.get("directories", [])
        self.skill_directories = [Path(d).expanduser() for d in directories]
        
        # 选项
        self.cache_enabled = skill_config.get("cache_enabled", True)
        self.default_enabled = skill_config.get("default_enabled", True)
        
        # 全局 Skill 存储
        self._skills: Dict[str, Skill] = {}
        
        # Session 独有 Skill 存储: session_id -> {skill_name -> Skill}
        self._session_skills: Dict[str, Dict[str, Skill]] = {}
        
        # 从 config 读取各 Session 的工作目录映射
        session_config = config.get("session", {})
        self._workspace_root = Path(
            session_config.get("workspace_root", "~/.pyclaego/workspaces")
        ).expanduser()
        self._session_workspace_map: Dict[str, Path] = {}
        session_workspace_root_dict = session_config.get("session_workspace_root", {})
        for sid, spath in (session_workspace_root_dict or {}).items():
            self._session_workspace_map[sid] = Path(spath)
        
        # 缓存
        self._content_cache: Dict[str, str] = {}
        
        self._initialized = True
        _rlog.info("core_service", f"[SkillManager] 单例实例已初始化，技能目录: {[str(d) for d in self.skill_directories]}")
    
    @classmethod
    def get_instance(cls) -> "SkillManager":
        """获取 SkillManager 单例实例
        
        Returns:
            SkillManager 实例
        """
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    def load_skills(self) -> Tuple[int, int]:
        """加载所有全局技能
        
        从配置的目录中发现并加载所有符合规范的技能。
        
        Returns:
            (成功数量, 失败数量)
        """
        success_count = 0
        failed_count = 0
        
        for directory in self.skill_directories:
            if not directory.exists():
                _rlog.warning("core_service", f"[SkillManager] 技能目录不存在: {directory}")
                continue
            
            # 遍历目录下的子目录
            for skill_path in directory.iterdir():
                if not skill_path.is_dir():
                    continue
                
                # 检查是否有 SKILL.md
                skill_md = skill_path / "SKILL.md"
                if not skill_md.exists():
                    continue
                
                # 尝试加载技能
                try:
                    skill = Skill(skill_path)
                    
                    # 检查是否已存在同名技能（优先级处理：metadata.priority）
                    if skill.name in self._skills:
                        existing_skill = self._skills[skill.name]
                        skill_priority = skill.metadata.get('priority', 0)
                        existing_priority = existing_skill.metadata.get('priority', 0)
                        if skill_priority > existing_priority:
                            _rlog.info("core_service", f"[SkillManager] 技能 '{skill.name}' 被更高优先级版本覆盖")
                            self._skills[skill.name] = skill
                        else:
                            _rlog.info("core_service", f"[SkillManager] 跳过低优先级技能: {skill.name}")
                            continue
                    else:
                        self._skills[skill.name] = skill
                    
                    success_count += 1
                    version_str = skill.metadata.get('version', '')
                    version_info = f" (v{version_str})" if version_str else ""
                    _rlog.info("core_service", f"[SkillManager] 成功加载技能: {skill.name}{version_info}")
                    
                except Exception as e:
                    failed_count += 1
                    _rlog.error("core_service", f"[SkillManager] 加载技能失败 {skill_path.name}: {e}")
        
        _rlog.info("core_service", f"[SkillManager] 加载完成: 成功 {success_count}, 失败 {failed_count}")
        return success_count, failed_count
    
    def reload_skills(self) -> Tuple[int, int]:
        """重新加载所有全局技能"""
        self._skills.clear()
        self._content_cache.clear()
        return self.load_skills()
    
    def load_session_skills(self, session_id: str) -> Tuple[int, int]:
        """加载指定 Session 的独有技能
        
        从 SESSION_WORKSPACE/skills/ 目录扫描并加载 Session 独有的 SKILL.md 技能，
        加载结果存入 self._session_skills[session_id]。
        
        Args:
            session_id: Session ID
            
        Returns:
            (成功数量, 失败数量)
        """
        # 推断 Session 工作目录
        if session_id in self._session_workspace_map:
            session_workspace = self._session_workspace_map[session_id]
        else:
            session_workspace = self._workspace_root / session_id
        
        skills_dir = session_workspace / "skills"
        
        session_skill_map: Dict[str, Skill] = {}
        success_count = 0
        failed_count = 0
        
        if not skills_dir.exists():
            _rlog.info("core_service", f"[SkillManager] Session '{session_id}' 无独立技能目录: {skills_dir}")
            self._session_skills[session_id] = session_skill_map
            return success_count, failed_count
        
        for skill_path in skills_dir.iterdir():
            if not skill_path.is_dir():
                continue
            skill_md = skill_path / "SKILL.md"
            if not skill_md.exists():
                continue
            try:
                skill = Skill(skill_path)
                # Session 技能直接存入，同名时覆盖（Session 内高目录顺序靠后的覆盖前面的）
                session_skill_map[skill.name] = skill
                success_count += 1
                _rlog.info("core_service", f"[SkillManager] Session '{session_id}' 加载技能: {skill.name}")
            except Exception as e:
                failed_count += 1
                _rlog.error("core_service", f"[SkillManager] Session '{session_id}' 加载技能失败 {skill_path.name}: {e}")
        
        self._session_skills[session_id] = session_skill_map
        _rlog.info("core_service", f"[SkillManager] Session '{session_id}' 技能加载完成: 成功 {success_count}, 失败 {failed_count}")
        return success_count, failed_count
    
    def reload_session_skills(self, session_id: str) -> Tuple[int, int]:
        """重新加载指定 Session 的独有技能
        
        先清除已加载的缓存，再重新扫描目录。
        
        Args:
            session_id: Session ID
            
        Returns:
            (成功数量, 失败数量)
        """
        if session_id in self._session_skills:
            del self._session_skills[session_id]
        return self.load_session_skills(session_id)
    
    def get_all_skills(self, session_id: Optional[str] = None) -> Dict[str, Skill]:
        """获取所有公用技能与 Session 独有技能的合并字典
        
        Session 技能优先级高于全局技能（同名时 Session 技能覆盖全局技能）。
        
        Args:
            session_id: Session ID（可选）。若为 None，仅返回全局技能。
            
        Returns:
            skill_name -> Skill 的合并字典
        """
        merged: Dict[str, Skill] = dict(self._skills)  # 全局技能的浅拷贝
        if session_id and session_id in self._session_skills:
            merged.update(self._session_skills[session_id])
        return merged
    
    def get_skill(self, skill_name: str, session_id: Optional[str] = None) -> Optional[Skill]:
        """根据名称获取 Skill 对象
        
        若提供 session_id，优先在该 Session 的独有技能中查找，再回退到全局技能。
        
        Args:
            skill_name: 技能名称
            session_id: Session ID（可选）
            
        Returns:
            Skill 对象，不存在则返回 None
        """
        if session_id and session_id in self._session_skills:
            session_skill = self._session_skills[session_id].get(skill_name)
            if session_skill:
                return session_skill
        return self._skills.get(skill_name)
    
    def has_skill(self, skill_name: str, session_id: Optional[str] = None) -> bool:
        """检查是否存在指定技能
        
        Args:
            skill_name: 技能名称
            session_id: Session ID（可选）
            
        Returns:
            是否存在
        """
        if session_id and session_id in self._session_skills:
            if skill_name in self._session_skills[session_id]:
                return True
        return skill_name in self._skills
    
    def list_skills(self,
                    tags: Optional[List[str]] = None,
                    session_id: Optional[str] = None) -> List[str]:
        """列出所有技能名称
        
        若提供 session_id，检查是否已加载该 Session 的独有技能；
        若尚未加载，则先调用 load_session_skills 加载后合并返回。
        
        Args:
            tags: 按标签过滤（可选）
            session_id: Session ID（可选）
            
        Returns:
            技能名称列表（按优先级降序、名称升序）
        """
        # 若传入 session_id 且尚未加载其独有技能，则先加载
        if session_id and session_id not in self._session_skills:
            self.load_session_skills(session_id)
        
        skill_map = self.get_all_skills(session_id)
        
        skill_names = skill_map.keys()
        
        # 按标签过滤
        if tags:
            skill_names = [
                name for name in skill_names
                if any(tag in skill_map[name].tags for tag in tags)
            ]
        
        return skill_names
    
    def get_skill_summary(self, skill_name: str, session_id: Optional[str] = None) -> str:
        """获取指定技能的简介
        
        Args:
            skill_name: 技能名称
            session_id: Session ID（可选）
            
        Returns:
            简介文本
            
        Raises:
            SkillNotFoundError: 技能不存在
        """
        skill = self.get_skill(skill_name, session_id=session_id)
        if not skill:
            raise SkillNotFoundError(f"Skill '{skill_name}' not found")
        
        return skill.get_summary()
    
    def get_skill_content(self,
                         skill_name: str,
                         sections: Optional[List[str]] = None,
                         session_id: Optional[str] = None) -> str:
        """获取指定技能的内容
        
        Args:
            skill_name: 技能名称
            sections: 指定章节列表（None 表示获取完整内容）
            session_id: Session ID（可选）
            
        Returns:
            内容文本
            
        Raises:
            SkillNotFoundError: 技能不存在
        """
        skill = self.get_skill(skill_name, session_id=session_id)
        if not skill:
            raise SkillNotFoundError(f"Skill '{skill_name}' not found")
        
        # 生成缓存键（加入 session 维度，避免 Session 技能与全局技能缓存冲突）
        cache_key = (
            f"{session_id or ''}::{skill_name}::"
            f"{':'.join(sections) if sections else 'full'}"
        )
        
        # 检查缓存
        if self.cache_enabled and cache_key in self._content_cache:
            return self._content_cache[cache_key]
        
        # 获取内容
        if sections:
            content = skill.get_sections(sections)
        else:
            content = skill.get_full_content()
        
        # 保存到缓存
        if self.cache_enabled:
            self._content_cache[cache_key] = content
        
        return content
    
    def format_skills_list(self,
                          include_description: bool = True,
                          session_id: Optional[str] = None) -> str:
        """格式化技能列表为字符串（用于组装上下文）
        
        Args:
            include_description: 是否包含描述
            session_id: Session ID（可选），若提供则合并 Session 独有技能
            
        Returns:
            格式化的技能列表字符串
            
        Examples:
            >>> manager.format_skills_list()
            '''Available Skills:
            1. python_best_practices (v1.0.0) - Python 最佳实践指南
            2. code_review_checklist (v1.2.0) - 代码审查清单
            '''
        """
        skill_names = self.list_skills(session_id=session_id)
        skill_map = self.get_all_skills(session_id)
        
        if not skill_names:
            return "No skills available."
        
        lines = ["Available Skills:"]
        
        for i, name in enumerate(skill_names, 1):
            skill = skill_map[name]
            version_str = skill.metadata.get('version', '')
            version_info = f" (v{version_str})" if version_str else ""
            if include_description:
                lines.append(f"{i}. {name}{version_info} - {skill.description}")
            else:
                lines.append(f"{i}. {name}{version_info}")
        
        return "\n".join(lines)
    
    def assemble_context(self,
                        skill_names: List[str],
                        include_full_content: bool = False,
                        sections_map: Optional[Dict[str, List[str]]] = None,
                        session_id: Optional[str] = None) -> str:
        """组装多个技能的上下文
        
        Args:
            skill_names: 要包含的技能名称列表
            include_full_content: 是否包含完整内容（False 则仅包含简介）
            sections_map: 技能名 -> 章节列表的映射（精细控制）
            session_id: Session ID（可选），若提供则优先在 Session 技能中查找
            
        Returns:
            组装后的上下文字符串
            
        Examples:
            >>> # 仅简介
            >>> context = manager.assemble_context(['skill1', 'skill2'])
            
            >>> # 完整内容
            >>> context = manager.assemble_context(['skill1'], include_full_content=True)
            
            >>> # 指定章节
            >>> context = manager.assemble_context(
            ...     ['skill1'],
            ...     sections_map={'skill1': ['简介', '示例']}
            ... )
        """
        if not skill_names:
            return ""
        
        context_parts = []
        
        for skill_name in skill_names:
            skill = self.get_skill(skill_name, session_id=session_id)
            if not skill:
                _rlog.warning("core_service", f"[SkillManager] 技能 '{skill_name}' 不存在，跳过")
                continue
            
            # 构建技能头部
            version_str = skill.metadata.get('version', '')
            version_info = f" (v{version_str})" if version_str else ""
            header = f"# Skill: {skill.name}{version_info}"
            context_parts.append(header)
            context_parts.append(f"> {skill.description}\n")
            
            # 获取内容
            if sections_map and skill_name in sections_map:
                # 使用指定章节
                content = skill.get_sections(sections_map[skill_name])
            elif include_full_content:
                # 完整内容
                content = skill.get_full_content()
            else:
                # 仅简介
                content = skill.get_summary()
            
            context_parts.append(content)
            context_parts.append("")  # 空行分隔
        
        return "\n".join(context_parts)
    
    def search_skills(self,
                     query: str,
                     search_in: str = "name,description,tags",
                     session_id: Optional[str] = None) -> List[str]:
        """搜索技能
        
        在全局技能与指定 Session 的独有技能合并结果中搜索。
        
        Args:
            query: 搜索关键词
            search_in: 搜索范围（name, description, tags, content）
            session_id: Session ID（可选），若提供则合并 Session 独有技能一起搜索
            
        Returns:
            匹配的技能名称列表
        """
        query_lower = query.lower()
        search_fields = [field.strip() for field in search_in.split(',')]
        
        skill_map = self.get_all_skills(session_id)
        results = []
        
        for skill_name, skill in skill_map.items():
            matched = False
            
            if 'name' in search_fields:
                if query_lower in skill.name.lower():
                    matched = True
            
            if 'description' in search_fields:
                if query_lower in skill.description.lower():
                    matched = True
            
            if 'tags' in search_fields:
                skill_tags = skill.metadata.get('tags', [])
                if any(query_lower in tag.lower() for tag in skill_tags):
                    matched = True
            
            if 'content' in search_fields:
                try:
                    content = skill.get_full_content().lower()
                    if query_lower in content:
                        matched = True
                except Exception:
                    pass
            
            if matched:
                results.append(skill_name)
        
        # 按优先级降序、名称升序排序（priority 从 metadata 读取）
        results.sort(key=lambda name: (-skill_map[name].metadata.get('priority', 0), name))
        
        return results
    
    def get_statistics(self) -> Dict[str, Any]:
        """获取统计信息
        
        Returns:
            统计信息字典
        """
        total_skills = len(self._skills)
        
        # 按标签统计
        by_tags: Dict[str, int] = {}
        for skill in self._skills.values():
            for tag in skill.tags:
                by_tags[tag] = by_tags.get(tag, 0) + 1
        
        # 按目录统计
        by_directory: Dict[str, int] = {}
        for skill in self._skills.values():
            for directory in self.skill_directories:
                if str(skill.path).startswith(str(directory)):
                    dir_name = str(directory)
                    by_directory[dir_name] = by_directory.get(dir_name, 0) + 1
                    break
        
        return {
            "total_skills": total_skills,
            "session_count": len(self._session_skills),
            "by_tags": by_tags,
            "by_directory": by_directory
        }
    
    def validate_all_skills(self) -> Dict[str, Tuple[bool, str]]:
        """验证所有全局技能
        
        Returns:
            skill_name -> (是否有效, 错误信息)
        """
        results = {}
        
        for skill_name, skill in self._skills.items():
            is_valid, error_msg = skill.validate()
            results[skill_name] = (is_valid, error_msg)
        
        return results
    
    def clear_cache(self):
        """清空内容缓存"""
        self._content_cache.clear()
        _rlog.info("core_service", "[SkillManager] 缓存已清空")
    
    def __repr__(self) -> str:
        return (
            f"<SkillManager "
            f"global_skills={len(self._skills)} "
            f"session_count={len(self._session_skills)}>"
        )
