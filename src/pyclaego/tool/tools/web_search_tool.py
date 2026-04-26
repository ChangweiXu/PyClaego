"""Web Search 工具 - 网页搜索"""

from typing import Dict, Any, List

import httpx
        
from ..base_tool import BaseTool, ToolResult, ToolStatus
from ...logging import get_running_log

_rlog = get_running_log()


class WebSearchTool(BaseTool):
    """Web 搜索工具
    
    功能：
    - 支持多个搜索提供商（Brave Search、Serper、Google Custom Search）
    - 返回搜索结果列表
    - 支持结果数量限制
    
    配置示例：
    ```yaml
    web_search:
      tool_type: "web_search"
      tool_name: "web_searcher"
      enabled: true
      timeout: 10
      provider: "brave"  # brave, serper, google
      api_key: ${BRAVE_API_KEY}
      max_results: 10
    ```
    """

    # 调用外部搜索 API，不影响本地状态；多个查询可并发
    IS_READONLY: bool = True
    IS_PARALLELIZABLE: bool = True

    def __init__(self, tool_config: Dict[str, Any]):
        """初始化 Web Search 工具
        
        Args:
            tool_config: 工具配置字典
        """
        super().__init__(tool_config)
        
        # 搜索提供商
        self.provider = tool_config.get("provider", "brave")
        
        # API Key
        self.api_key = tool_config.get("api_key", "")
        
        # 最大结果数
        self.max_results = tool_config.get("max_results", 10)
        
        # 验证配置
        if not self.api_key:
            _rlog.warning("core_service", f"未配置 {self.provider} API Key，工具可能无法正常工作")
    
    async def execute(self, **kwargs) -> ToolResult:
        """执行搜索
        
        Args:
            query: 搜索查询字符串
            max_results: 最大结果数（可选，覆盖配置）
            
        Returns:
            ToolResult: 搜索结果
        """
        # 验证必需参数
        valid, error_msg = self.validate_params(["query"], **kwargs)
        if not valid:
            return ToolResult(status=ToolStatus.FAILED, error=error_msg)
        
        query = kwargs["query"]
        max_results = self._coerce_int(kwargs.get("max_results", self.max_results), default=self.max_results)
        
        try:
            _rlog.info("core_service", f"搜索: {query} (provider={self.provider}, max_results={max_results})")
            
            # 根据提供商选择搜索方法
            if self.provider == "brave":
                results = await self._search_brave(query, max_results)
            elif self.provider == "serper":
                results = await self._search_serper(query, max_results)
            elif self.provider == "google":
                results = await self._search_google(query, max_results)
            else:
                return ToolResult(
                    status=ToolStatus.FAILED,
                    error=f"不支持的搜索提供商: {self.provider}"
                )
            
            _rlog.info("core_service", f"搜索完成，找到 {len(results)} 条结果")
            
            return ToolResult(
                status=ToolStatus.SUCCESS,
                output={
                    "query": query,
                    "results": results,
                    "count": len(results)
                },
                metadata={
                    "provider": self.provider,
                    "max_results": max_results
                }
            )
            
        except Exception as e:
            error_msg = f"搜索失败: {str(e)}"
            _rlog.error("core_service", error_msg)
            return ToolResult(
                status=ToolStatus.FAILED,
                error=error_msg,
                metadata={"query": query, "provider": self.provider}
            )
    
    async def _search_brave(self, query: str, max_results: int) -> List[Dict[str, Any]]:
        """使用 Brave Search API
        
        Args:
            query: 搜索查询
            max_results: 最大结果数
            
        Returns:
            List[Dict]: 搜索结果列表
        """
        url = "https://api.search.brave.com/res/v1/web/search"
        
        headers = {
            "X-Subscription-Token": self.api_key,
            "Accept": "application/json"
        }
        
        params = {
            "q": query,
            "count": max_results
        }
        
        async with httpx.AsyncClient(
            http2=True,
            timeout=httpx.Timeout(self.timeout, connect=10.0),
        ) as client:
            response = await client.get(url, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()
            
            results = []
            for item in data.get("web", {}).get("results", [])[:max_results]:
                results.append({
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "description": item.get("description", ""),
                    "published_date": item.get("age", "")
                })
            
            return results
    
    async def _search_serper(self, query: str, max_results: int) -> List[Dict[str, Any]]:
        """使用 Serper API
        
        Args:
            query: 搜索查询
            max_results: 最大结果数
            
        Returns:
            List[Dict]: 搜索结果列表
        """
        url = "https://google.serper.dev/search"
        
        headers = {
            "X-API-KEY": self.api_key,
            "Content-Type": "application/json"
        }
        
        payload = {
            "q": query,
            "num": max_results
        }
        
        async with httpx.AsyncClient(
            http2=True,
            timeout=httpx.Timeout(self.timeout, connect=10.0),
        ) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
            
            results = []
            for item in data.get("organic", [])[:max_results]:
                results.append({
                    "title": item.get("title", ""),
                    "url": item.get("link", ""),
                    "description": item.get("snippet", ""),
                    "published_date": item.get("date", "")
                })
            
            return results
    
    async def _search_google(self, query: str, max_results: int) -> List[Dict[str, Any]]:
        """使用 Google Custom Search API
        
        Args:
            query: 搜索查询
            max_results: 最大结果数
            
        Returns:
            List[Dict]: 搜索结果列表
        """
        url = "https://www.googleapis.com/customsearch/v1"
        
        params = {
            "key": self.api_key,
            "cx": self.config.get("search_engine_id", ""),  # Custom Search Engine ID
            "q": query,
            "num": min(max_results, 10)  # Google API 限制单次最多10个结果
        }
        
        async with httpx.AsyncClient(
            http2=True,
            timeout=httpx.Timeout(self.timeout, connect=10.0),
        ) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            
            results = []
            for item in data.get("items", [])[:max_results]:
                results.append({
                    "title": item.get("title", ""),
                    "url": item.get("link", ""),
                    "description": item.get("snippet", ""),
                    "published_date": ""
                })
            
            return results
    
    def mask_output(self, raw_output: Any, path_mask_map: Dict[str, str]) -> Any:
        """Web 搜索结果不含本地路径，直接返回原始输出。

        Args:
            raw_output: execute() 返回的 output 字典
            path_mask_map: 真实路径 -> 占位符的映射字典（本工具不使用）

        Returns:
            raw_output（不做任何修改）
        """
        return raw_output

    def get_description(self) -> Dict[str, Any]:
        """获取工具描述
        
        Returns:
            Dict: 工具描述信息
        """
        return {
            "name": self.tool_name,
            "description": f"使用 {self.provider} 搜索引擎进行网页搜索",
            "parameters": {
                "query": {
                    "type": "string",
                    "required": True,
                    "description": "搜索查询字符串"
                },
                "max_results": {
                    "type": "integer",
                    "required": False,
                    "description": f"最大结果数（默认: {self.max_results}）"
                }
            },
            "returns": {
                "query": "搜索查询",
                "results": "搜索结果列表（包含 title, url, description, published_date）",
                "count": "结果数量"
            },
            "examples": [
                {
                    "query": "Python asyncio tutorial",
                    "description": "搜索 Python asyncio 教程"
                },
                {
                    "query": "weather in New York",
                    "max_results": 5,
                    "description": "搜索纽约天气（限制5条结果）"
                }
            ],
            "provider": self.provider,
            "is_readonly": self.__class__.IS_READONLY,
            "is_parallelizable": self.__class__.IS_PARALLELIZABLE,
        }

