"""Context Factory - 上下文处理器工厂类"""

from pathlib import Path
from typing import Any

from ..logging import get_running_log
from .base_context import BaseContextHandler

_rlog = get_running_log()


class ContextFactory:
    """上下文处理器工厂类
    
    功能：
    - 注册上下文处理器类型
    - 根据配置创建上下文处理器实例
    - 管理上下文处理器类型注册表
    """
    
    # 上下文处理器类型注册表
    _handler_registry: dict[str, type[BaseContextHandler]] = {}
    
    @classmethod
    def register_handler(cls, handler_type: str, handler_class: type[BaseContextHandler]) -> None:
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
        widget_config: dict[str, Any]
    ) -> BaseContextHandler:
        """创建上下文处理器实例

        Args:
            session_id: 会话 ID
            workspace_path: 工作空间路径
            widget_config: **完整** widget 配置字典；至少包含 ``context``
                子键（其中 ``type`` 指定处理器类型）。处理器构造时收到的
                ``config`` 参数即为 ``widget_config`` 整体，便于读取
                ``ps_metadata`` / ``context_subagents`` 等横切字段。

        Returns:
            BaseContextHandler: 上下文处理器实例

        Raises:
            ValueError: 如果处理器类型未注册或配置无效
        """
        context_config = widget_config.get("context") or {}
        handler_type = context_config.get("type")

        if not handler_type:
            raise ValueError("widget_config['context'] 必须包含 'type' 字段")

        handler_class = cls._handler_registry.get(handler_type)

        if not handler_class:
            available_types = list(cls._handler_registry.keys())
            raise ValueError(
                f"未知的上下文处理器类型: '{handler_type}'\n"
                f"可用类型: {available_types}"
            )

        # 创建实例（传入完整 widget_config）
        try:
            handler = handler_class(
                session_id=session_id,
                workspace_path=workspace_path,
                config=widget_config
            )
            _rlog.info("core_service", f"[ContextFactory] 创建上下文处理器: {handler_type} (session={session_id})")
            return handler

        except Exception as e:
            raise ValueError(f"创建上下文处理器失败: {e!s}")
    
    @classmethod
    def list_available_handlers(cls) -> list:
        """列出所有已注册的处理器类型
        
        Returns:
            已注册的处理器类型列表
        """
        return list(cls._handler_registry.keys())
