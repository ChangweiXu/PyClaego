"""工具模块 - 提供系统工具调用功能"""

from .base_tool import BaseTool, ToolResult, ToolStatus
from .file_system import (
    CopyMoveTool,
    DeleteFileTool,
    FileEditTool,
    FileInfoTool,
    FindLineTool,
    GlobTool,
    ListDirectoryTool,
    MkdirTool,
    ReadFileTool,
    ReadImageBase64Tool,
    ReadPdfTool,
    SearchTextTool,
    WriteFileTool,
)

# 导入 safe_bash 工具
from .safe_bash import SafeBashTool

# 导入 safe_python 工具
from .safe_python import SafePythonTool
from .tool_call_parser import TOOL_CALL_PROMPT, ToolCallParser
from .tool_manager import ToolManager

# 导入所有工具实现
from .tools import (
    BashTool,
    DownloadFileTool,
    PythonExecTool,
    QueryUserTool,
    WebFetchTool,
    # WebFetchToolV2,  # V2 已弃用，使用 V3
    WebFetchToolV3,
    WebSearchTool,
)

# 注册工具类型
ToolManager.register_tool("bash", BashTool)
ToolManager.register_tool("web_search", WebSearchTool)
# ToolManager.register_tool("web_fetch", WebFetchTool)
# ToolManager.register_tool("web_fetch", WebFetchToolV2)  # V2 已弃用
ToolManager.register_tool("web_fetch", WebFetchToolV3)  # 使用 V3
ToolManager.register_tool("read_file", ReadFileTool)
ToolManager.register_tool("write_file", WriteFileTool)
ToolManager.register_tool("list_directory", ListDirectoryTool)
ToolManager.register_tool("search_text", SearchTextTool)
ToolManager.register_tool("mkdir", MkdirTool)
ToolManager.register_tool("glob", GlobTool)
ToolManager.register_tool("download_file", DownloadFileTool)
ToolManager.register_tool("read_image_base64", ReadImageBase64Tool)
ToolManager.register_tool("read_pdf", ReadPdfTool)
ToolManager.register_tool("file_edit", FileEditTool)
ToolManager.register_tool("file_delete", DeleteFileTool)
ToolManager.register_tool("copy_move", CopyMoveTool)
ToolManager.register_tool("file_info", FileInfoTool)
ToolManager.register_tool("find_line", FindLineTool)
ToolManager.register_tool("python_exec", PythonExecTool)
ToolManager.register_tool("safe_bash", SafeBashTool)
ToolManager.register_tool("safe_python", SafePythonTool)
ToolManager.register_tool("query_user", QueryUserTool)

# 便捷函数
def get_tool_manager() -> ToolManager:
    """获取工具管理器单例实例
    
    Returns:
        ToolManager: 工具管理器实例
    """
    return ToolManager.get_instance()

__all__ = [
    "BaseTool",
    "ToolResult",
    "ToolStatus",
    "ToolManager",
    "TOOL_CALL_PROMPT",
    "ToolCallParser",
    "get_tool_manager",
    "BashTool",
    "WebSearchTool",
    # "WebFetchTool",
    # "WebFetchToolV2",  # V2 已弃用
    "WebFetchToolV3",
    "ReadFileTool",
    "WriteFileTool",
    "ListDirectoryTool",
    "SearchTextTool",
    "MkdirTool",
    "GlobTool",
    "DownloadFileTool",
    "ReadImageBase64Tool",
    "ReadPdfTool",
    "FileEditTool",
    "DeleteFileTool",
    "CopyMoveTool",
    "FileInfoTool",
    "FindLineTool",
    "PythonExecTool",
    "SafeBashTool",
    "SafePythonTool",
]
