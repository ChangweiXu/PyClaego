"""Web Fetch 工具 - 抓取网页内容"""

from typing import Any

import httpx
from bs4 import BeautifulSoup

from ...logging import get_running_log
from ..base_tool import BaseTool, ToolResult, ToolStatus

_rlog = get_running_log()


class WebFetchTool(BaseTool):
    """Web 内容抓取工具
    
    功能：
    - 抓取网页 HTML 内容
    - 提取纯文本内容
    - 提取元数据（标题、描述等）
    - 支持自定义 User-Agent
    - 使用 httpx（支持 HTTP/2、系统 DNS）提升兼容性
    
    配置示例：
    ```yaml
    web_fetch:
      tool_type: "web_fetch"
      tool_name: "web_fetch"
      enabled: true
      timeout: 30
      user_agent: "Mozilla/5.0 (compatible; PyClaego/1.0)"
      max_content_length: 1048576  # 1MB
    ```
    """

    # 只抓取远端网页内容，不影响本地状态；多个抓取可并发
    IS_READONLY: bool = True
    IS_PARALLELIZABLE: bool = True

    def __init__(self, tool_config: dict[str, Any]):
        """初始化 Web Fetch 工具
        
        Args:
            tool_config: 工具配置字典
        """
        super().__init__(tool_config)
        
        # User-Agent
        self.user_agent = tool_config.get(
            "user_agent",
            "Mozilla/5.0 (compatible; PyClaego/1.0; +https://example.com)"
        )
        
        # 最大内容长度（字节）
        self.max_content_length = tool_config.get("max_content_length", 1024 * 1024)  # 1MB
        
        # 是否提取纯文本
        self.extract_text = tool_config.get("extract_text", True)
        
        # 是否提取元数据
        self.extract_metadata = tool_config.get("extract_metadata", True)
    
    async def execute(self, **kwargs) -> ToolResult:
        """抓取网页内容
        
        Args:
            url: 网页 URL
            extract_text: 是否提取纯文本（可选）
            extract_metadata: 是否提取元数据（可选）
            
        Returns:
            ToolResult: 抓取结果
        """
        # 验证必需参数
        valid, error_msg = self.validate_params(["url"], **kwargs)
        if not valid:
            return ToolResult(status=ToolStatus.FAILED, error=error_msg)
        
        url = kwargs["url"]
        extract_text = self._coerce_bool(kwargs.get("extract_text", True), default=True)
        extract_metadata = self._coerce_bool(kwargs.get("extract_metadata", True), default=True)
        include_html = self._coerce_bool(kwargs.get("include_html", False), default=False)

        try:
            _rlog.info("core_service", f"抓取网页: {url}")
            
            headers = {
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Accept-Encoding": "gzip, deflate, br",
            }
            
            async with httpx.AsyncClient(
                http2=True,
                follow_redirects=True,
                timeout=httpx.Timeout(self.timeout, connect=10.0),
            ) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                
                content_type = response.headers.get("content-type", "")
                if "text/html" not in content_type and "application/xhtml" not in content_type:
                    _rlog.warning("core_service", f"非 HTML 内容类型: {content_type}")
                
                html_content = response.text
                
                if len(html_content) > self.max_content_length:
                    html_content = html_content[:self.max_content_length]
                    _rlog.warning("core_service", f"内容被截断至 {self.max_content_length} 字节")
            
            # 解析 HTML
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # 构建结果
            result_data = {
                "url": url,
                "status_code": response.status_code,
                "content_type": content_type
            }
            
            # 提取纯文本
            if extract_text:
                # 移除脚本和样式标签
                for script in soup(["script", "style"]):
                    script.decompose()
                
                # 提取文本
                text = soup.get_text(separator="\n", strip=True)
                
                # 清理空行
                lines = [line.strip() for line in text.splitlines() if line.strip()]
                text = "\n".join(lines)
                
                result_data["text"] = text
                result_data["text_length"] = len(text)
            
            # 提取元数据
            if extract_metadata:
                metadata = self._extract_metadata(soup)
                result_data["metadata"] = metadata
            
            # 包含原始 HTML（可选，根据配置）
            if include_html:
                result_data["html"] = html_content
                result_data["html_length"] = len(html_content)
            
            _rlog.info("core_service", f"抓取成功: {url}")
            
            return ToolResult(
                status=ToolStatus.SUCCESS,
                output=result_data,
                metadata={
                    "url": url,
                    "content_length": len(html_content)
                }
            )
            
        except Exception as e:
            error_msg = f"抓取失败: {e!s}"
            _rlog.error("core_service", error_msg)
            return ToolResult(
                status=ToolStatus.FAILED,
                error=error_msg,
                metadata={"url": url}
            )
    
    def _extract_metadata(self, soup) -> dict[str, str | None]:
        """提取网页元数据
        
        Args:
            soup: BeautifulSoup 对象
            
        Returns:
            Dict: 元数据字典
        """
        metadata = {}
        
        # 标题
        title_tag = soup.find("title")
        metadata["title"] = title_tag.get_text(strip=True) if title_tag else None
        
        # Meta 标签
        meta_tags = {
            "description": ["name", "description"],
            "keywords": ["name", "keywords"],
            "author": ["name", "author"],
            "og:title": ["property", "og:title"],
            "og:description": ["property", "og:description"],
            "og:image": ["property", "og:image"],
            "og:url": ["property", "og:url"],
        }
        
        for key, (attr, value) in meta_tags.items():
            tag = soup.find("meta", attrs={attr: value})
            if tag:
                metadata[key] = tag.get("content", "")
        
        # 规范 URL
        canonical = soup.find("link", rel="canonical")
        if canonical:
            metadata["canonical_url"] = canonical.get("href", "")
        
        # 语言
        html_tag = soup.find("html")
        if html_tag:
            metadata["language"] = html_tag.get("lang", "")
        
        return metadata
    
    def mask_output(self, raw_output: Any, path_mask_map: dict[str, str]) -> Any:
        """Web 内容抓取结果不含本地路径，直接返回原始输出。

        Args:
            raw_output: execute() 返回的 output 字典
            path_mask_map: 真实路径 -> 占位符的映射字典（本工具不使用）

        Returns:
            raw_output（不做任何修改）
        """
        return raw_output

    def get_description(self) -> dict[str, Any]:
        """获取工具描述
        
        Returns:
            Dict: 工具描述信息
        """
        return {
            "name": self.tool_name,
            "description": "抓取网页内容并提取文本和元数据",
            "parameters": {
                "url": {
                    "type": "string",
                    "required": True,
                    "description": "要抓取的网页 URL"
                },
                "extract_text": {
                    "type": "boolean",
                    "required": False,
                    "description": f"是否提取纯文本（默认: {self.extract_text}）"
                },
                "extract_metadata": {
                    "type": "boolean",
                    "required": False,
                    "description": f"是否提取元数据（默认: {self.extract_metadata}）"
                },
                "include_html": {
                    "type": "boolean",
                    "required": False,
                    "description": "是否包含原始 HTML（默认: false）"
                }
            },
            "returns": {
                "url": "网页 URL",
                "status_code": "HTTP 状态码",
                "content_type": "内容类型",
                "text": "纯文本内容（如果启用）",
                "metadata": "元数据（标题、描述等）",
                "html": "原始 HTML（如果请求）"
            },
            "examples": [
                {
                    "url": "https://example.com",
                    "description": "抓取 example.com 的内容"
                },
                {
                    "url": "https://docs.python.org/3/",
                    "extract_text": True,
                    "extract_metadata": True,
                    "description": "抓取 Python 文档并提取文本和元数据"
                }
            ],
            "is_readonly": self.__class__.IS_READONLY,
            "is_parallelizable": self.__class__.IS_PARALLELIZABLE,
        }
