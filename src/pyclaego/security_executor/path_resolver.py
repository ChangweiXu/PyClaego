"""路径解析器 - 解析工具调用中的路径占位符

支持的占位符：
- {{SKILL:skill_name}}/path - Skill 资源路径
- {{WORKSPACE}}/path - Session Workspace 目录
- {{SESSION_SKILL_ROOT}}/path - Session 独有技能根目录（workspace/skills）
- {{PROJECT}}/path - 项目根目录  
- {{TEMP}}/path - 临时目录
"""

import re
from pathlib import Path
from typing import Dict, Optional, Tuple, Callable

from ..config import get_config
from ..logging import get_running_log

_rlog = get_running_log()


class PathResolver:
    """通用路径解析器
    
    功能：
    - 解析命令中的路径占位符
    - 替换为真实路径
    - 验证路径安全性
    
    注意：
    - {{WORKSPACE}} 根据 session_id 动态确定
    - 支持 session_workspace_root 配置映射
    """
    
    # 占位符正则表达式
    PATTERN_SKILL = re.compile(r'\{\{SKILL:([^}]+)\}\}')
    PATTERN_WORKSPACE = re.compile(r'\{\{WORKSPACE\}\}')
    PATTERN_SESSION_SKILL_ROOT = re.compile(r'\{\{SESSION_SKILL_ROOT\}\}')
    PATTERN_PROJECT = re.compile(r'\{\{PROJECT\}\}')
    PATTERN_TEMP = re.compile(r'\{\{TEMP\}\}')
    
    def __init__(
        self, 
        project_root: Path,
        temp_dir: Path,
        skill_path_getter: Optional[Callable[[str, Optional[str]], Optional[Path]]] = None
    ):
        """初始化路径解析器
        
        Args:
            project_root: 项目根目录
            temp_dir: 临时目录
            skill_path_getter: 获取 Skill 路径的回调函数 (skill_name, session_id) -> Path
        """
        self.project_root = project_root.resolve()
        self.temp_dir = temp_dir.resolve()
        self.skill_path_getter = skill_path_getter
        
        # 读取配置（用于获取 workspace_root 和 session_workspace_root）
        config = get_config()
        session_config = config.get("session", {})
        self.workspace_root = Path(session_config.get("workspace_root", "~/.pyclaego/workspaces")).expanduser()
        self.session_workspace_root_dict = session_config.get("session_workspace_root", {})
        
        _rlog.info(
            "core_service",
            f"[PathResolver] 已初始化:\n"
            f"  Project={self.project_root}\n"
            f"  Temp={self.temp_dir}\n"
            f"  WorkspaceRoot={self.workspace_root}",
        )
    
    def get_workspace_path(self, session_id: str) -> Path:
        """根据 session_id 获取 workspace 路径
        
        逻辑与 Session 类一致：
        1. 优先使用 session_workspace_root 配置的自定义路径
        2. 否则使用 workspace_root/session_id
        
        Args:
            session_id: 会话 ID
            
        Returns:
            该 session 的 workspace 路径
        """
        # 检查是否有自定义路径配置
        if self.session_workspace_root_dict and session_id in self.session_workspace_root_dict:
            custom_workspace = self.session_workspace_root_dict[session_id]
            return Path(custom_workspace).expanduser().resolve()
        
        # 使用默认路径
        return (self.workspace_root / session_id).resolve()
    
    def get_session_skill_root_path(self, session_id: str) -> Path:
        """根据 session_id 获取 Session 独有技能根目录路径
        
        默认返回 workspace/skills 路径，即当前 Session 的 workspace 下的 skills 子目录。
        
        Args:
            session_id: 会话 ID
            
        Returns:
            该 session 的独有技能根目录路径
        """
        return self.get_workspace_path(session_id) / "skills"
    
    def get_project_path(self, session_id: Optional[str] = None) -> Path:
        """根据 session_id 获取项目根目录路径
        
        优先级：
        1. Session 配置中 session_metadata.project_root（通过 get_session_config 获取）
        2. 全局默认 self.project_root（PathResolver 初始化时传入）
        
        Args:
            session_id: 会话 ID（可选，用于查找 Session 级配置）
            
        Returns:
            项目根目录的绝对路径
        """
        if session_id:
            try:
                from ..config import get_session_config
                session_config = get_session_config(session_id)
                session_project_root = session_config.get("session_metadata", {}).get("project_root")
                if session_project_root:
                    return Path(session_project_root).expanduser().resolve()
            except Exception:
                pass
        return self.project_root
    
    def has_placeholders(self, text: str) -> bool:
        """检查文本中是否包含占位符
        
        Args:
            text: 待检查的文本
            
        Returns:
            是否包含占位符
        """
        return bool(
            self.PATTERN_SKILL.search(text) or
            self.PATTERN_WORKSPACE.search(text) or
            self.PATTERN_SESSION_SKILL_ROOT.search(text) or
            self.PATTERN_PROJECT.search(text) or
            self.PATTERN_TEMP.search(text)
        )
    
    def resolve(self, text: str, session_id: Optional[str] = None, subagent_id: Optional[str] = None) -> Tuple[str, bool, str]:
        """解析文本中的所有路径占位符
        
        Args:
            text: 包含占位符的文本
            session_id: 当前会话 ID（用于确定 WORKSPACE 和 PROJECT 路径）
            subagent_id: 子 Agent ID（可选，用于日志追踪）
            
        Returns:
            (替换后的文本, 是否成功, 错误信息)
            
        Examples:
            >>> resolver = PathResolver(...)
            >>> text = "bash {{SKILL:python_tools}}/scripts/setup.sh"
            >>> resolved, success, error = resolver.resolve(text, "session123")
            >>> print(resolved)
            'bash /path/to/skills/python_tools/scripts/setup.sh'
        """
        if not session_id:
            raise ValueError("Session ID is required for path resolution")
        try:
            result = text
            
            # 1. 替换 {{SKILL:name}}
            result, success, error = self._resolve_skill_paths(result, session_id)
            if not success:
                return text, False, error
            
            # 2. 替换 {{WORKSPACE}} - 根据 session_id 动态获取
            # TODO 解析 subagent 工作目录 [session_ws]/subagents/[subagent_id]（如果 subagent_id 可用且路径存在）
            workspace_path = self.get_workspace_path(session_id)
            result = self.PATTERN_WORKSPACE.sub(str(workspace_path), result)
            
            # 3. 替换 {{SESSION_SKILL_ROOT}} - Session 独有技能根目录
            session_skill_root_path = self.get_session_skill_root_path(session_id)
            result = self.PATTERN_SESSION_SKILL_ROOT.sub(str(session_skill_root_path), result)
            
            # 4. 替换 {{PROJECT}} - 根据 session_id 动态获取项目根目录
            project_path = self.get_project_path(session_id)
            result = self.PATTERN_PROJECT.sub(str(project_path), result)
            
            # 5. 替换 {{TEMP}}
            result = self.PATTERN_TEMP.sub(str(self.temp_dir), result)
            
            return result, True, ""
            
        except Exception as e:
            return text, False, f"Path resolution failed: {e}"
    
    def _resolve_skill_paths(self, text: str, session_id: Optional[str] = None) -> Tuple[str, bool, str]:
        """替换 {{SKILL:skill_name}} 占位符
        
        Args:
            text: 文本
            session_id: 当前 Session ID（用于查找 Session 独有技能）
            
        Returns:
            (替换后的文本, 是否成功, 错误信息)
        """
        if not session_id:
            raise ValueError("Session ID is required for skill path resolution")
        if not self.skill_path_getter:
            # 没有配置 skill_path_getter，检查是否有 SKILL 占位符
            if self.PATTERN_SKILL.search(text):
                return text, False, "Skill path getter not configured"
            return text, True, ""
        
        errors = []
        
        def replace_match(match):
            skill_name = match.group(1)
            skill_path = self.skill_path_getter(skill_name, session_id)  # type: ignore
            
            if skill_path is None:
                error_msg = f"Skill '{skill_name}' not found"
                errors.append(error_msg)
                return match.group(0)  # 保持原样
            
            return str(skill_path)
        
        result = self.PATTERN_SKILL.sub(replace_match, text)
        
        if errors:
            return text, False, "; ".join(errors)
        
        return result, True, ""
    
    def extract_placeholders(self, text: str) -> Dict[str, list]:
        """提取文本中的所有占位符
        
        Args:
            text: 文本
            
        Returns:
            占位符类型 -> 占位符值列表
        """
        result = {
            "SKILL": [],
            "WORKSPACE": [],
            "SESSION_SKILL_ROOT": [],
            "PROJECT": [],
            "TEMP": []
        }
        
        # 提取 {{SKILL:name}}
        result["SKILL"] = self.PATTERN_SKILL.findall(text)
        
        # 提取其他占位符（简单计数）
        if self.PATTERN_WORKSPACE.search(text):
            result["WORKSPACE"] = ["WORKSPACE"]
        
        if self.PATTERN_SESSION_SKILL_ROOT.search(text):
            result["SESSION_SKILL_ROOT"] = ["SESSION_SKILL_ROOT"]
        
        if self.PATTERN_PROJECT.search(text):
            result["PROJECT"] = ["PROJECT"]
        
        if self.PATTERN_TEMP.search(text):
            result["TEMP"] = ["TEMP"]
        
        return result
    
    def validate_resolved_path(
        self, 
        resolved_path: str,
        session_id: str,
        allowed_roots: Optional[list] = None
    ) -> Tuple[bool, str]:
        """验证解析后的路径是否安全
        
        Args:
            resolved_path: 解析后的路径
            session_id: 会话 ID
            allowed_roots: 允许的根目录列表（None 则使用默认）
            
        Returns:
            (是否有效, 错误信息)
        """
        if allowed_roots is None:
            workspace_path = self.get_workspace_path(session_id)
            allowed_roots = [
                workspace_path,
                self.project_root,
                self.temp_dir
            ]
        
        try:
            resolved = Path(resolved_path).resolve()
            
            # 检查是否在允许的根目录下
            for allowed_root in allowed_roots:
                if str(resolved).startswith(str(allowed_root)):
                    return True, ""
            
            return False, f"Path '{resolved_path}' is outside allowed directories"
            
        except Exception as e:
            return False, f"Invalid path: {e}"


def get_skill_path_from_manager(
    skill_manager,
    skill_name: str,
    session_id: Optional[str] = None
) -> Optional[Path]:
    """从 SkillManager 获取技能路径的辅助函数
    
    若提供 session_id，优先在该 Session 的独有技能中查找，再回退到全局技能。
    
    Args:
        skill_manager: SkillManager 实例
        skill_name: 技能名称
        session_id: Session ID（可选）
        
    Returns:
        技能路径，不存在返回 None
    """
    if skill_manager is None:
        return None
    
    skill = skill_manager.get_skill(skill_name, session_id=session_id)
    if skill:
        return skill.path
    
    return None
