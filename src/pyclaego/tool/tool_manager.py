"""工具管理器 - 管理所有工具的注册、创建和执行"""

import traceback
from typing import Dict, Any, Type, List, Optional
from .base_tool import BaseTool, ToolResult, ToolStatus
from ..logging import get_running_log

_rlog = get_running_log()


class ToolManager:
    """工具管理器（单例模式）
    
    功能：
    - 注册工具类型
    - 从配置创建工具实例
    - 统一的工具执行接口
    - 工具状态管理
    """
    
    _instance: Optional["ToolManager"] = None
    _initialized: bool = False
    
    # 工具类型注册表
    _tool_registry: Dict[str, Type[BaseTool]] = {}
    
    def __new__(cls) -> "ToolManager":
        """实现单例模式"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        """初始化工具管理器（仅第一次创建时执行）"""
        if self._initialized:
            return
        
        # 工具实例缓存
        self._tools: Dict[str, BaseTool] = {}
        
        # 从配置文件加载工具
        self._load_tools_from_config()
        
        self._initialized = True
        
        # 使用日志系统
        _rlog.info("core_service", f"[ToolManager] 工具管理器已初始化，加载了 {len(self._tools)} 个工具")
    
    def _load_tools_from_config(self) -> None:
        """从配置文件加载工具"""
        try:
            from ..config import get_config
            config = get_config()
            tools_config = config.get("tools", {})
            
            # 遍历配置中的所有工具
            for tool_type, tool_config in tools_config.items():
                if isinstance(tool_config, dict) and tool_config.get("enabled", False):
                    try:
                        # 添加 tool_type 到配置中
                        tool_config["tool_type"] = tool_type
                        
                        # 创建工具实例
                        tool = self.create_tool(tool_config)
                        
                        # 缓存工具实例
                        tool_name = tool_config.get("tool_name", tool_type)
                        self._tools[tool_name] = tool
                        
                    except Exception as e:
                        _rlog.error("core_service", f"[ToolManager] 加载工具失败 (type={tool_type}): {e}\n{traceback.format_exc()}")
            
        except Exception as e:
            _rlog.error("core_service", f"[ToolManager] 加载配置失败: {e}")
    
    @classmethod
    def register_tool(cls, tool_type: str, tool_class: Type[BaseTool]) -> None:
        """注册工具类型
        
        Args:
            tool_type: 工具类型名称
            tool_class: 工具类
        """
        if tool_type in cls._tool_registry:
            _rlog.warning("core_service", f"[ToolManager] 工具类型 '{tool_type}' 已存在，将被覆盖")
        
        cls._tool_registry[tool_type] = tool_class
        _rlog.info("core_service", f"[ToolManager] 已注册工具类型: {tool_type}")
    
    @classmethod
    def create_tool(cls, tool_config: Dict[str, Any]) -> BaseTool:
        """创建工具实例
        
        Args:
            tool_config: 工具配置字典，必须包含 "tool_type" 字段
            
        Returns:
            BaseTool: 工具实例
            
        Raises:
            ValueError: 如果工具类型未注册或配置无效
        """
        tool_type = tool_config.get("tool_type")
        
        if not tool_type:
            raise ValueError("工具配置必须包含 'tool_type' 字段")
        
        tool_class = cls._tool_registry.get(tool_type)
        
        if not tool_class:
            raise ValueError(
                f"未知的工具类型: {tool_type}。"
                f"已注册的类型: {list(cls._tool_registry.keys())}"
            )
        
        _rlog.info("core_service", f"[ToolManager] 创建工具: {tool_type} (name={tool_config.get('tool_name', tool_type)})")
        return tool_class(tool_config)
    
    @classmethod
    def list_available_tools(cls) -> List[str]:
        """列出所有可用的工具类型
        
        Returns:
            List[str]: 工具类型列表
        """
        return list(cls._tool_registry.keys())
    
    def get_tool(self, tool_name: str) -> Optional[BaseTool]:
        """获取工具实例
        
        Args:
            tool_name: 工具名称
            
        Returns:
            Optional[BaseTool]: 工具实例，如果不存在返回 None
        """
        return self._tools.get(tool_name)
    
    def list_loaded_tools(self) -> List[str]:
        """列出所有已加载的工具
        
        Returns:
            List[str]: 工具名称列表
        """
        return list(self._tools.keys())
    
    async def execute_tool(
        self,
        tool_name: str,
        **kwargs
    ) -> ToolResult:
        """执行工具
        
        Args:
            tool_name: 工具名称
            **kwargs: 工具参数
            
        Returns:
            ToolResult: 执行结果
        """
        # 获取工具实例
        tool = self.get_tool(tool_name)
        
        if not tool:
            error_msg = f"工具不存在: {tool_name}"
            _rlog.error("core_service", f"[ToolManager] {error_msg}")
            return ToolResult(
                status=ToolStatus.FAILED,
                error=error_msg
            )
        
        # 检查工具是否启用
        if not tool.is_enabled():
            error_msg = f"工具未启用: {tool_name}"
            _rlog.warning("core_service", f"[ToolManager] {error_msg}")
            return ToolResult(
                status=ToolStatus.DISABLED,
                error=error_msg
            )
        
        # 执行工具
        try:
            _rlog.info("core_service", f"执行工具: {tool_name}")
            result = await tool.execute(**kwargs)
            
            if result.is_success():
                _rlog.info("core_service", f"工具执行成功: {tool_name}")
            else:
                _rlog.warning("core_service", f"工具执行失败: {tool_name}, status={result.status.value}")
            
            return result
            
        except Exception as e:
            error_msg = f"工具执行异常: {str(e)}"
            import traceback
            _rlog.error("core_service", f"[ToolManager] {error_msg}\n{traceback.format_exc()}")
            return ToolResult(
                status=ToolStatus.FAILED,
                error=error_msg
            )
    
    def get_readonly_parallelizable_tools(self) -> List[str]:
        """获取所有只读且可并发的工具名称列表（供 SubAgent 使用）

        Returns:
            List[str]: 满足条件的工具名称列表
        """
        return [
            name for name, tool in self._tools.items()
            if tool.is_readonly and tool.is_parallelizable
        ]

    def get_all_tools_info(self) -> Dict[str, Dict[str, Any]]:
        """获取所有工具的信息
        
        Returns:
            Dict: 工具信息字典
        """
        tools_info = {}
        
        for tool_name, tool in self._tools.items():
            tools_info[tool_name] = {
                **tool.get_info(),
                "description": tool.get_description()
            }
        
        return tools_info
    
    def mask_tool_output(
        self,
        tool_name: str,
        raw_output: Any,
        path_mask_map: Dict[str, str]
    ) -> Any:
        """对工具输出进行路径脱敏（与工具参数中的路径解析操作相反）。

        在 SecurityHandler 执行完工具后调用，把输出中出现的真实路径替换回
        占位符，避免真实路径信息泄漏给上层 Agent。

        此方法与 PathResolver.resolve() / handler.py 中的路径占位符解析方向相反：
          解析（handler.py）：{{PLACEHOLDER}} -> /real/path（工具执行前）
          脱敏（本方法）：    /real/path      -> {{PLACEHOLDER}}（工具执行后）

        Args:
            tool_name: 工具名称（与 execute_tool 使用的名称相同）
            raw_output: 工具执行的原始输出（ToolResult.output 的值）
            path_mask_map: 真实路径 -> 占位符的映射字典，
                           e.g. {"/home/user/workspace/session1": "{{WORKSPACE}}",
                                 "/home/user/project":            "{{PROJECT}}"}

        Returns:
            脱敏后的输出，结构与 raw_output 相同

        Notes:
            - 如果工具不存在，记录警告并直接返回 raw_output（不抛异常）
            - 路径替换按路径长度从长到短进行，避免短路径提前截断长路径
        """
        tool = self.get_tool(tool_name)
        if tool is None:
            _rlog.warning("core_service", f"mask_tool_output: 工具不存在 '{tool_name}'，跳过脱敏")
            return raw_output

        return tool.mask_output(raw_output, path_mask_map)

    @classmethod
    def get_instance(cls) -> "ToolManager":
        """获取工具管理器单例实例
        
        Returns:
            ToolManager: 工具管理器实例
        """
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
