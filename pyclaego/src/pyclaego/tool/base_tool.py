"""工具基类 - 定义工具的抽象接口"""

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

from ..logging import get_running_log

_rlog = get_running_log()


class ToolStatus(Enum):
    """工具执行状态"""
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    DISABLED = "disabled"


class ToolResult:
    """工具执行结果"""
    
    def __init__(
        self,
        status: ToolStatus,
        output: Any = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
        content_parts: list | None = None,
    ):
        """初始化工具执行结果
        
        Args:
            status: 执行状态
            output: 执行输出
            error: 错误信息（如果有）
            metadata: 额外的元数据
            content_parts: 多模态内容块列表（ContentPart 对象），工具通过此字段
                           返回图片、文档等视觉内容，由 Agent 层传递给 LLM 客户端
        """
        self.status = status
        self.output = output
        self.error = error
        self.metadata = metadata or {}
        self.content_parts = content_parts
    
    def to_dict(self) -> dict[str, Any]:
        """转换为字典格式
        
        Returns:
            字典格式的结果
        """
        return {
            "status": self.status.value,
            "output": self.output,
            "error": self.error,
            "metadata": self.metadata
        }
    
    def is_success(self) -> bool:
        """判断是否执行成功
        
        Returns:
            是否成功
        """
        return self.status == ToolStatus.SUCCESS


class BaseTool(ABC):
    """工具抽象基类
    
    所有工具实现都必须继承此类并实现以下方法：
    - execute(): 执行工具的主要逻辑
    - get_description(): 返回工具的描述信息
    """

    # 工具内置属性（built-in constants）：不可通过配置覆盖，仅由工具类本身决定
    # 子类必须显式覆盖这两个类常量以声明自己的能力
    IS_READONLY: bool = False          # 工具是否为只读（不修改本地文件或系统状态）
    IS_PARALLELIZABLE: bool = False    # 工具是否可并发调用（多实例安全并发执行）

    def __init__(self, tool_config: dict[str, Any]):
        """初始化工具
        
        Args:
            tool_config: 工具配置字典，包含工具的所有配置参数
        """
        self.config = tool_config
        self.tool_type = tool_config.get("tool_type", "unknown")
        self.tool_name = tool_config.get("tool_name", f"{self.tool_type}_tool")
        self.enabled = tool_config.get("enabled", True)
        self.timeout = tool_config.get("timeout", 30)  # 默认30秒超时

        # 从类常量读取内置属性（不可由配置覆盖）
        self.is_readonly: bool = self.__class__.IS_READONLY
        self.is_parallelizable: bool = self.__class__.IS_PARALLELIZABLE
        
        # 日志记录
        _rlog.info("core_service", f"初始化工具: {self.tool_name} (type={self.tool_type}, enabled={self.enabled})")
    
    @abstractmethod
    async def execute(self, **kwargs) -> ToolResult:
        """执行工具
        
        Args:
            **kwargs: 工具特定的参数
            
        Returns:
            ToolResult: 执行结果
        """
        pass
    
    @abstractmethod
    def get_description(self) -> dict[str, Any]:
        """获取工具描述信息
        
        Returns:
            Dict: 工具描述信息，包含：
                - name: 工具名称
                - description: 工具描述
                - parameters: 参数说明
                - examples: 使用示例
        """
        pass
    
    def is_enabled(self) -> bool:
        """检查工具是否启用
        
        Returns:
            bool: 是否启用
        """
        return self.enabled
    
    def is_readonly_tool(self) -> bool:
        """检查工具是否为只读工具（不产生副作用）

        Returns:
            bool: 是否只读
        """
        return self.is_readonly

    def is_parallelizable_tool(self) -> bool:
        """检查工具是否可并发调用

        Returns:
            bool: 是否可并发
        """
        return self.is_parallelizable

    def get_info(self) -> dict[str, Any]:
        """获取工具信息
        
        Returns:
            Dict: 工具信息
        """
        return {
            "tool_type": self.tool_type,
            "tool_name": self.tool_name,
            "enabled": self.enabled,
            "timeout": self.timeout,
            "is_readonly": self.is_readonly,
            "is_parallelizable": self.is_parallelizable,
        }

    def extract_paths(self, args: dict[str, Any]) -> dict[str, list[str]]:
        """Declare which filesystem paths this tool call will touch.

        This is the canonical entry point used by central security rules
        (e.g. ``WorkspacePathRule``) to decide whether a tool invocation
        is allowed. The default implementation returns no paths — tools
        that touch the filesystem must override this method to declare
        their read/write footprint.

        Args:
            args: The tool call arguments (same dict passed to ``execute``).

        Returns:
            ``{"read": [<path str>, ...], "write": [<path str>, ...]}``.
            Paths may be relative, contain ``~`` or contain placeholders
            such as ``{{WORKSPACE}}`` / ``{{PROJECT}}``; consumers are
            expected to expand them.
        """
        return {"read": [], "write": []}
    
    @staticmethod
    def _mask_string(text: str, path_mask_map: dict[str, str]) -> str:
        """将字符串中的真实路径替换为占位符（按路径长度从长到短）。

        与 PathResolver.resolve() 方向相反：
          resolve: {{PLACEHOLDER}} -> /real/path
          mask:    /real/path      -> {{PLACEHOLDER}}

        按路径长度从长到短替换，确保长路径优先匹配，避免短路径截断长路径。

        Args:
            text: 待脱敏的字符串
            path_mask_map: 真实路径 -> 占位符的映射字典，
                           e.g. {"/home/user/workspace/session1": "{{WORKSPACE}}"}

        Returns:
            脱敏后的字符串
        """
        if not path_mask_map or not isinstance(text, str):
            return text
        for real_path in sorted(path_mask_map.keys(), key=len, reverse=True):
            text = text.replace(real_path, path_mask_map[real_path])
        return text

    @abstractmethod
    def mask_output(
        self,
        raw_output: Any,
        path_mask_map: dict[str, str]
    ) -> Any:
        """将工具输出中的真实路径替换为占位符（脱敏）。

        此方法与 PathResolver.resolve() 方向相反：
          resolve: {{PLACEHOLDER}} -> /real/path
          mask:    /real/path      -> {{PLACEHOLDER}}

        Args:
            raw_output: 工具执行的原始输出（execute() 返回的 ToolResult.output）
            path_mask_map: 真实路径 -> 占位符的映射字典，
                           e.g. {"/home/user/workspace/session1": "{{WORKSPACE}}"}

        Returns:
            脱敏后的输出（结构与 raw_output 相同，字符串值中路径被替换）
        """
        pass

    @staticmethod
    def _coerce_bool(value: Any, default: bool = False) -> bool:
        """将参数值统一转换为 bool 类型，兼容 str / bool / int 三种输入格式。

        转换规则：
        - 已是 bool：直接返回
        - int：0 → False，非 0 → True
        - str（大小写不敏感）："true"/"yes"/"1"/"on" → True；其余 → False
        - 其他类型（None 等）：返回 default

        Args:
            value:   原始参数值（可能是 str、bool 或 int）
            default: 当 value 为 None 或无法识别时的回退值

        Returns:
            bool
        """
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value != 0
        if isinstance(value, str):
            return value.strip().lower() in ("true", "yes", "1", "on")
        return default

    @staticmethod
    def _coerce_int(value: Any, default: int = 0) -> int:
        """将参数值统一转换为 int 类型，兼容 str / int / float 三种输入格式。

        转换规则：
        - 已是 int：直接返回
        - float：截断为 int
        - str：调用 int()；若解析失败则返回 default
        - 其他类型（None 等）：返回 default

        Args:
            value:   原始参数值（可能是 str 或 int）
            default: 当 value 为 None 或无法解析时的回退值

        Returns:
            int
        """
        if value is None:
            return default
        if isinstance(value, bool):
            # bool 是 int 的子类，需先排除（True→1，False→0）
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            try:
                return int(value.strip())
            except (ValueError, TypeError):
                return default
        return default

    def validate_params(self, required_params: list, **kwargs) -> tuple[bool, str | None]:
        """验证必需参数
        
        Args:
            required_params: 必需参数列表
            **kwargs: 传入的参数
            
        Returns:
            tuple[bool, Optional[str]]: (是否验证通过, 错误信息)
        """
        for param in required_params:
            if param not in kwargs or kwargs[param] is None:
                error_msg = f"缺少必需参数: {param}"
                _rlog.error("core_service", error_msg)
                return False, error_msg
        
        return True, None
