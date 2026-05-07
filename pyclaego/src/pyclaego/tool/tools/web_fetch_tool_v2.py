"""Web Fetch 工具 - 带本地缓存的网页抓取"""

import time
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup

from ...logging import get_running_log
from ..base_tool import BaseTool, ToolResult, ToolStatus
from .web_fetch_cache import BaseCacheState, CacheEntry, create_cache_state

_rlog = get_running_log()


class WebFetchToolV2(BaseTool):
    """Web 内容抓取工具（带本地缓存）
    
    功能：
    - 抓取网页 HTML 内容并提取纯文本
    - 本地缓存支持，避免重复抓取
    - 自动截断过长内容，提供完整内容的文件路径
    - 缓存过期自动清理
    - 内容变更检测与版本管理
    
    缓存行为：
    - use_cache=True（默认）：优先读取缓存，无缓存则抓取并缓存
    - use_cache=False：强制重新抓取，检查内容是否变更
      - 内容相同：直接返回缓存
      - 内容不同：保存为新版本，不覆盖旧缓存
    
    截断行为：
    - 当内容超过 truncate_threshold 字符时，返回截断内容
    - 截断内容末尾添加 [TRUNCATED] 标记
    - 可使用文件读取工具获取完整内容
    
    配置示例：
    ```yaml
    web_fetch:
      tool_type: "web_fetch"
      tool_name: "web_fetch"
      enabled: true
      timeout: 30
      user_agent: "Mozilla/5.0 (compatible; PyClaego/1.0)"
      max_content_length: 1048576  # 1MB
      cache_dir: ".cache/web_fetch"
      cache_format: "json"
      cache_ttl: 86400  # 24小时，null 表示永不过期
      truncate_threshold: 15000  # 字符数
      file_naming: "hash_md5"
    ``` {data-source-line="369"}
    """

    IS_READONLY: bool = True
    IS_PARALLELIZABLE: bool = True

    def __init__(self, tool_config: dict[str, Any]):
        """初始化 Web Fetch 工具
        
        Args:
            tool_config: 工具配置字典
        """
        super().__init__(tool_config)
        
        # 基础配置
        self.user_agent = tool_config.get(
            "user_agent",
            "Mozilla/5.0 (compatible; PyClaego/1.0; +https://example.com)"
        )
        self.max_content_length = tool_config.get("max_content_length", 1024 * 1024)
        self.extract_text = tool_config.get("extract_text", True)
        self.extract_metadata = tool_config.get("extract_metadata", True)
        
        # 缓存配置
        cache_dir_str = tool_config.get("cache_dir", ".cache/web_fetch")
        self.cache_dir = Path(cache_dir_str).resolve()
        self.cache_format = tool_config.get("cache_format", "json")
        self.cache_ttl: int | None = tool_config.get("cache_ttl", 86400)
        self.truncate_threshold = tool_config.get("truncate_threshold", 15000)
        self.file_naming = tool_config.get("file_naming", "hash_md5")
        
        # 初始化缓存状态管理器
        self._cache_state: BaseCacheState = create_cache_state(
            self.cache_format, 
            self.cache_dir
        )
        
        _rlog.info("core_service", f"WebFetchToolV2 初始化完成，缓存目录: {self.cache_dir}")
    
    async def execute(self, **kwargs) -> ToolResult:
        """抓取网页内容
        
        Args:
            url: 网页 URL（必需）
            use_cache: 是否使用缓存（默认 True）
            extract_text: 是否提取纯文本（默认 True）
            extract_metadata: 是否提取元数据（默认 True）
            include_html: 是否包含原始 HTML（默认 False）
            
        Returns:
            ToolResult: 抓取结果
        """
        # 验证必需参数
        valid, error_msg = self.validate_params(["url"], **kwargs)
        if not valid:
            return ToolResult(status=ToolStatus.FAILED, error=error_msg)
        
        url = kwargs["url"]
        use_cache = self._coerce_bool(kwargs.get("use_cache", True), default=True)
        extract_text = self._coerce_bool(kwargs.get("extract_text", True), default=True)
        extract_metadata = self._coerce_bool(kwargs.get("extract_metadata", True), default=True)
        include_html = self._coerce_bool(kwargs.get("include_html", False), default=False)
        
        # 清理过期缓存
        self._cleanup_expired_cache()
        
        try:
            # 尝试使用缓存
            if use_cache:
                cached_result = self._try_read_cache(url)
                if cached_result is not None:
                    _rlog.info("core_service", f"使用缓存: {url}")
                    return self._build_result(
                        url=url,
                        text=cached_result["text"],
                        metadata=cached_result.get("metadata"),
                        from_cache=True,
                        extract_text=extract_text,
                        extract_metadata=extract_metadata,
                        include_html=include_html,
                        html_content=cached_result.get("html")
                    )
            
            # 抓取网页
            fetch_result = await self._fetch_webpage(url)
            if fetch_result is None:
                return ToolResult(
                    status=ToolStatus.FAILED,
                    error=f"抓取失败: {url}",
                    metadata={"url": url}
                )
            
            html_content, response_status, content_type = fetch_result
            
            # 解析内容
            text, metadata = self._parse_html(html_content)
            
            # 计算内容 hash
            content_hash = BaseCacheState.content_hash(text, self.file_naming)
            
            # 检查是否需要保存缓存
            cache_entry = self._cache_state.get_entry(url)
            
            if not use_cache and cache_entry is not None:
                # 强制刷新模式：检查内容是否变更
                if cache_entry.has_content(content_hash):
                    _rlog.info("core_service", f"内容未变更，使用已有缓存: {url}")
                    cached_result = self._try_read_cache(url)
                    if cached_result is not None:
                        return self._build_result(
                            url=url,
                            text=cached_result["text"],
                            metadata=cached_result.get("metadata"),
                            from_cache=True,
                            extract_text=extract_text,
                            extract_metadata=extract_metadata,
                            include_html=include_html,
                            html_content=cached_result.get("html")
                        )
            
            # 保存到缓存
            cache_file_path = self._save_to_cache(url, text, html_content, metadata, content_hash)
            _rlog.info("core_service", f"缓存已保存: url={url}, path={cache_file_path}")
            
            return self._build_result(
                url=url,
                text=text,
                metadata=metadata,
                from_cache=False,
                extract_text=extract_text,
                extract_metadata=extract_metadata,
                include_html=include_html,
                html_content=html_content,
                status_code=response_status,
                content_type=content_type,
                cache_file_path=cache_file_path
            )
            
        except Exception as e:
            error_msg = f"抓取失败: {e!s}"
            _rlog.error("core_service", error_msg)
            return ToolResult(
                status=ToolStatus.FAILED,
                error=error_msg,
                metadata={"url": url}
            )
    
    def _cleanup_expired_cache(self):
        """清理过期缓存"""
        try:
            removed = self._cache_state.cleanup_expired(self.cache_ttl)
            if removed > 0:
                _rlog.info("core_service", f"清理了 {removed} 个过期缓存条目")
        except Exception as e:
            _rlog.warning("core_service", f"清理过期缓存时出错: {e}")
    
    def _try_read_cache(self, url: str) -> dict[str, Any] | None:
        """尝试读取缓存
        
        Returns:
            缓存内容字典，或 None（无缓存/缓存过期/读取失败）
        """
        entry = self._cache_state.get_entry(url)
        if entry is None:
            return None
        
        # 检查是否过期
        if entry.is_expired(self.cache_ttl):
            return None
        
        # 从最新的缓存文件开始尝试读取
        for file_path in reversed(entry.file_paths):
            try:
                path = Path(file_path)
                if path.exists():
                    import json
                    with open(path, encoding='utf-8') as f:
                        data = json.load(f)
                    
                    # 更新访问时间
                    entry.last_accessed_at = time.time()
                    self._cache_state.set_entry(url, entry)
                    self._cache_state.save()
                    
                    return data
            except Exception as e:
                _rlog.warning("core_service", f"读取缓存文件失败: {file_path}, 错误: {e}")
                continue
        
        return None
    
    def _save_to_cache(
        self, 
        url: str, 
        text: str, 
        html_content: str, 
        metadata: dict[str, Any],
        content_hash: str
    ) -> str:
        """保存内容到缓存
        
        Returns:
            缓存文件的绝对路径
        """
        import json
        
        # 生成文件名
        url_hash = BaseCacheState.url_to_key(url, self.file_naming)
        
        # 获取或创建缓存条目
        entry = self._cache_state.get_entry(url)
        if entry is None:
            entry = CacheEntry(url=url)
        
        # 确定文件名（带版本号）
        version = len(entry.file_paths)
        if version == 0:
            filename = f"{url_hash}.json"
        else:
            filename = f"{url_hash}_v{version}.json"
        
        file_path = self.cache_dir / filename
        abs_path = str(file_path.resolve())
        
        # 保存缓存文件
        cache_data = {
            "url": url,
            "text": text,
            "html": html_content,
            "metadata": metadata,
            "content_hash": content_hash,
            "cached_at": time.time()
        }
        
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)
        
        # 更新缓存条目
        entry.add_cache_file(abs_path, content_hash)
        self._cache_state.set_entry(url, entry)
        self._cache_state.save()
        
        return abs_path
    
    async def _fetch_webpage(self, url: str) -> tuple | None:
        """抓取网页
        
        Returns:
            (html_content, status_code, content_type) 或 None
        """
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
        }
        
        try:
            async with httpx.AsyncClient(
                http2=False,
                follow_redirects=True,
                timeout=httpx.Timeout(self.timeout, connect=10.0),
            ) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                
                content_type = response.headers.get("content-type", "")
                
                # 检查内容长度
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > self.max_content_length:
                    _rlog.warning("core_service", f"内容过大: {content_length} 字节")
                    return None
                
                html_content = response.text
                
                # 截断过大内容
                if len(html_content) > self.max_content_length:
                    html_content = html_content[:self.max_content_length]
                    _rlog.warning("core_service", f"HTML 内容被截断至 {self.max_content_length} 字节")
                
                return (html_content, response.status_code, content_type)
                    
        except Exception as e:
            _rlog.error("core_service", f"抓取网页失败: {url}, 错误: {e}")
            return None
    
    def _parse_html(self, html_content: str) -> tuple:
        """解析 HTML 内容
        
        Returns:
            (text, metadata)
        """
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 移除脚本和样式
        for script in soup(["script", "style"]):
            script.decompose()
        
        # 提取文本
        text = soup.get_text(separator="\n", strip=True)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        text = "\n".join(lines)
        
        # 提取元数据
        metadata = self._extract_metadata(soup)
        
        return text, metadata
    
    def _extract_metadata(self, soup) -> dict[str, str | None]:
        """提取网页元数据"""
        metadata = {}
        
        title_tag = soup.find("title")
        metadata["title"] = title_tag.get_text(strip=True) if title_tag else None
        
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
        
        canonical = soup.find("link", rel="canonical")
        if canonical:
            metadata["canonical_url"] = canonical.get("href", "")
        
        html_tag = soup.find("html")
        if html_tag:
            metadata["language"] = html_tag.get("lang", "")
        
        return metadata
    
    def _build_result(
        self,
        url: str,
        text: str,
        metadata: dict[str, Any] | None,
        from_cache: bool,
        extract_text: bool,
        extract_metadata: bool,
        include_html: bool,
        html_content: str | None = None,
        status_code: int | None = None,
        content_type: str | None = None,
        cache_file_path: str | None = None
    ) -> ToolResult:
        """构建返回结果，处理截断逻辑"""
        
        result_data = {
            "url": url,
            "from_cache": from_cache,
        }
        
        if status_code is not None:
            result_data["status_code"] = status_code
        if content_type is not None:
            result_data["content_type"] = content_type
        
        # 添加缓存文件路径（如果提供）
        if cache_file_path:
            result_data["cache_file"] = cache_file_path
        
        # 处理文本截断
        if extract_text:
            if len(text) > self.truncate_threshold:
                # 获取缓存文件路径
                entry = self._cache_state.get_entry(url)
                cache_path = entry.get_latest_path() if entry else (cache_file_path or "UNKNOWN")
                
                truncated_text = text[:self.truncate_threshold]
                truncated_text += "\n\n[TRUNCATED]"
                
                result_data["text"] = truncated_text
                result_data["text_truncated"] = True
                result_data["text_full_length"] = len(text)
                # 确保 cache_file 字段存在（优先使用 cache_path，兜底使用参数）
                if "cache_file" not in result_data:
                    result_data["cache_file"] = cache_path
            else:
                result_data["text"] = text
                result_data["text_truncated"] = False
            
            result_data["text_length"] = len(text)
        
        if extract_metadata and metadata:
            result_data["metadata"] = metadata
        
        if include_html and html_content:
            if len(html_content) > self.truncate_threshold:
                result_data["html"] = html_content[:self.truncate_threshold] + "\n\n[TRUNCATED]"
                result_data["html_truncated"] = True
            else:
                result_data["html"] = html_content
                result_data["html_truncated"] = False
            result_data["html_length"] = len(html_content)
        
        return ToolResult(
            status=ToolStatus.SUCCESS,
            output=result_data,
            metadata={
                "url": url,
                "from_cache": from_cache,
                "text_length": len(text)
            }
        )
    
    def mask_output(self, raw_output: Any, path_mask_map: dict[str, str]) -> Any:
        """Web 内容抓取结果不含本地路径，直接返回原始输出"""
        return raw_output
    
    def get_description(self) -> dict[str, Any]:
        """获取工具描述"""
        return {
            "name": self.tool_name,
            "description": f"""抓取网页内容并提取文本和元数据，支持本地缓存。

缓存行为：
- use_cache=true（默认）：优先读取本地缓存，无缓存时抓取并保存
- use_cache=false：强制重新抓取网页
  - 如果内容与缓存一致，直接返回缓存内容
  - 如果内容有变更，保存为新版本（不覆盖旧缓存）

截断行为：
- 当文本内容超过 {self.truncate_threshold} 字符时，返回截断后的内容
- 截断内容末尾添加 [TRUNCATED] 标记
- 完整内容已保存到标签中的文件路径，可使用文件读取工具获取

缓存过期：
- 缓存有效期为 {self.cache_ttl} 秒（{self.cache_ttl // 3600 if self.cache_ttl else '永不'}小时）
- 过期缓存会在下次调用时自动清理""",
            "parameters": {
                "url": {
                    "type": "string",
                    "required": True,
                    "description": "要抓取的网页 URL"
                },
                "use_cache": {
                    "type": "boolean",
                    "required": False,
                    "description": "是否使用缓存（默认: true）。设为 false 可强制重新抓取并检查内容变更"
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
                "from_cache": "是否来自缓存",
                "text": "纯文本内容（可能被截断）",
                "text_truncated": "文本是否被截断",
                "text_full_length": "完整文本长度（仅当截断时）",
                "cache_file": "缓存文件绝对路径（新抓取时始终提供，截断时包含完整内容）",
                "metadata": "元数据（标题、描述等）",
                "status_code": "HTTP 状态码（仅新抓取时）",
                "content_type": "内容类型（仅新抓取时）"
            },
            "examples": [
                {
                    "url": "https://example.com",
                    "description": "抓取网页，优先使用缓存"
                },
                {
                    "url": "https://example.com",
                    "use_cache": False,
                    "description": "强制重新抓取，检查内容是否变更"
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
            "notes": [
                f"文本超过 {self.truncate_threshold} 字符时会被截断",
                "截断内容末尾添加 [TRUNCATED] 标记",
                "新抓取时 cache_file 字段始终包含缓存文件绝对路径",
                "可使用文件读取工具读取完整内容"
            ]
        }
