"""Session 模块

管理用户会话，包括独立工作空间、配置和状态
"""

from .session import Session
from .manager import SessionManager

__all__ = ['Session', 'SessionManager']
