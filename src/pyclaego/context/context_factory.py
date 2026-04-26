"""Context Factory - 上下文处理器工厂类"""

from typing import Dict, Any, Type
from pathlib import Path

from .base_context import BaseContextHandler
from ..logging import get_running_log

_rlog = get_running_log()


class ContextFactory:
    """上下文处理器工厂类
    
    功能：
    - 注册上下文处理器类型
    - 根据配置创建上下文处理器实例
    - 管理上下文处理器类型注册表
    """
    
    # 上下文处理器类型注册表
    _handler_registry: Dict[str, Type[BaseContextHandler]] = {}
    
    @classmethod
    def register_handler(cls, handler_type: str, handler_class: Type[BaseContextHandler]) -> None:
        """注册上下文处理器类型
        
        Args:
            handler_type: 处理器类型名称（如 "simple", "rag"）
            handler_class: 处理器类
        """
        if handler_type in cls._handler_registry:
            _rlog.warning("core_service", f"[ContextFactory] 上下文处理器类型 '{handler_type}' 已存在，将被覆盖")
        
        cls._handler_registry[handler_type] = handler_class
        _rlog.info("core_service", f"[ContextFactory] 已注册上下文处理器类型: {handler_type}")
    
    @classmethod
    def create_handler(
        cls, 
        session_id: str, 
        workspace_path: Path,
        context_config: Dict[str, Any]
    ) -> BaseContextHandler:
        """创建上下文处理器实例
        
        Args:
            session_id: 会话 ID
            workspace_path: 工作空间路径
            context_config: 上下文配置字典（必须包含 "type" 字段）
            
        Returns:
            BaseContextHandler: 上下文处理器实例
            
        Raises:
            ValueError: 如果处理器类型未注册或配置无效
        """
        handler_type = context_config.get("type")
        
        if not handler_type:
            raise ValueError("上下文配置必须包含 'type' 字段")
        
        handler_class = cls._handler_registry.get(handler_type)
        
        if not handler_class:
            available_types = list(cls._handler_registry.keys())
            raise ValueError(
                f"未知的上下文处理器类型: '{handler_type}'\n"
                f"可用类型: {available_types}"
            )
        
        # 创建实例
        try:
            handler = handler_class(
                session_id=session_id,
                workspace_path=workspace_path,
                config=context_config
            )
            _rlog.info("core_service", f"[ContextFactory] 创建上下文处理器: {handler_type} (session={session_id})")
            return handler
            
        except Exception as e:
            raise ValueError(f"创建上下文处理器失败: {str(e)}")
    
    @classmethod
    def list_available_handlers(cls) -> list:
        """列出所有已注册的处理器类型
        
        Returns:
            已注册的处理器类型列表
        """
        return list(cls._handler_registry.keys())
