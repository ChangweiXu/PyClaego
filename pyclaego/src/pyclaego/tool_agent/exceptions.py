"""ToolAgent 模块自定义异常"""


class ToolAgentError(Exception):
    """ToolAgent 相关的基础异常"""
    pass


class ToolAgentConfigError(ToolAgentError):
    """ToolAgent config.json 格式或校验错误"""
    pass


class ToolAgentNotFoundError(ToolAgentError):
    """指定的 ToolAgent 不存在"""
    pass


class ToolAgentLoadError(ToolAgentError):
    """ToolAgent 加载失败"""
    pass
