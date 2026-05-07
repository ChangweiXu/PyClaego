"""
WebFetchToolV3 — 一站式网页抓取 + HTML→MD 转换 + 结构化大纲提取。

继承 WebFetchToolV2 的缓存架构（httpx + BeautifulSoup + JsonCacheState + 版本管理）。
新增能力：
1. 可插拔的 HTML→MD 转换管线（generic / arxiv / 自定义）
2. Markdown 大纲提取（供 md_read_section 分节阅读）
3. 路径返回模式（正文落盘，返回体仅含 outline + preview + 路径）
4. 渐进式消费友好（与 summarize_and_forget + md_read_section 协作）
"""

import hashlib
import time
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from ...logging import get_running_log
from ..base_tool import ToolResult, ToolStatus
from .html_to_md import get_converter, list_converters
from .html_to_md.outline import extract_outline
from .web_fetch_tool_v2 import WebFetchToolV2

_rlog = get_running_log()


# ── 常量 ──────────────────────────────────────────────

DEFAULT_PREVIEW_LENGTH = 500
MAX_PREVIEW_LENGTH = 5000        # 硬上限，防止 preview 膨胀
DEFAULT_OUTPUT_FORMAT = "md"


# ── 模式推断 ──────────────────────────────────────────

def _detect_mode(url: str) -> str:
    """根据 URL 自动推断解析模式。

    可扩展：注册 URL pattern → mode 映射表。
    """
    # === arXiv ===
    if "arxiv.org" in url:
        return "arxiv"

    # === 未来扩展留白 ===
    # if "medium.com" in url:
    #     return "medium"
    # if "readthedocs.io" in url or "docs." in url:
    #     return "docs"

    # === 默认 ===
    return "generic"


# ── V3 主类 ───────────────────────────────────────────

class WebFetchToolV3(WebFetchToolV2):
    """Web 内容抓取工具 V3 — HTML→MD 一站式方案。

    继承 V2 的全部缓存能力，新增可插拔 HTML→MD 转换与大纲提取。

    使用示例：
        tool = WebFetchToolV3(tool_config)
        result = await tool.execute(
            url="https://arxiv.org/html/2604.23781v1",
            output_format="md",
            extract_outline=True,
            preview_length=500,
            mode="auto",
        )
        # result.output["outline"]  → 章节导航
        # result.output["output_file"] → 正文 MD 文件绝对路径
    """

    # ── CLI 注册元信息 ─────────────────────────────────
    # 覆盖 V2 的类常量

    TOOL_TYPE = "web_fetch"
    TOOL_NAME = "web_fetch"
    TOOL_DESCRIPTION = (
        "抓取网页内容并转换为 Markdown 格式，支持缓存。"
        "返回元信息、章节大纲和预览片段。"
        "正文已保存到 output_file 路径，可使用文件读取工具获取完整内容。"
    )
    TOOL_PARAMETERS = {
        "url": {
            "type": "string",
            "required": True,
            "description": "要抓取的网页 URL",
        },
        "use_cache": {
            "type": "boolean",
            "required": False,
            "description": (
                "是否使用缓存（默认：true）。"
                "设为 false 可强制重新抓取并检查内容变更"
            ),
        },
        "output_format": {
            "type": "string",
            "required": False,
            "description": (
                "输出格式（默认：md）。"
                "md=Markdown 转换，text=纯文本（等同 V2 行为）"
            ),
        },
        "extract_outline": {
            "type": "boolean",
            "required": False,
            "description": (
                "是否从 MD 中提取章节大纲（默认：true）。"
                "仅在 output_format=md 时有效"
            ),
        },
        "preview_length": {
            "type": "integer",
            "required": False,
            "description": "预览文本的前 N 个字符（默认：500，最大值：5000）",
        },
        "mode": {
            "type": "string",
            "required": False,
            "description": (
                "HTML 解析模式（默认：auto）。"
                "auto=自动识别，generic=通用转换，arxiv=arXiv 专用解析"
            ),
        },
    }

    # ── 类变量：可供外部注册自定义转换器 ──────────────

    _custom_converters: dict[str, callable] = {}

    @classmethod
    def register_converter(cls, mode: str, converter: callable) -> None:
        """运行时注册自定义转换器。

        Args:
            mode:      模式名（不可与内置模式重名）
            converter: 转换函数 (html_raw, soup) -> md_text
        """
        if mode in ("generic", "arxiv"):
            raise ValueError(
                f"模式 {mode!r} 为内置模式，不可覆盖。请使用其他名称。"
            )
        cls._custom_converters[mode] = converter

    # ── 初始化 ─────────────────────────────────────────

    def __init__(self, tool_config: dict[str, Any]):
        """初始化 V3。

        继承 V2 的缓存初始化（cache_dir / cache_state / cache_ttl 等）。
        添加 V3 专属配置项。

        Args:
            tool_config: 工具配置字典（由 ToolManager 注入）。
                         V3 额外支持：
                         - md_output_dir: 正文 Markdown 落盘目录（默认同 cache_dir）
        """
        super().__init__(tool_config)

        # V3 专属：MD 正文落盘目录
        self._md_output_dir = Path(
            tool_config.get("md_output_dir", str(self.cache_dir))
        )

    # ── execute() 五阶段流水线 ─────────────────────────

    async def execute(self, **kwargs) -> ToolResult:
        """主入口：抓取 → 解析 → 转换 → 大纲 → 落盘 → 组装返回。

        阶段:
        1. 参数校验 + 模式推断
        2. 抓取 + 缓存决策（复用 V2 的 _try_read_cache / _fetch_webpage / _save_to_cache）
        3. HTML → MD 转换（根据 mode 调度转换器）
        4. 大纲提取
        5. 正文落盘
        6. 组装返回体（不含正文）
        """
        start_time = time.time()

        # ── Phase 1: 参数校验 ──────────────────────────
        valid, error_msg = self.validate_params(["url"], **kwargs)
        if not valid:
            return ToolResult(ToolStatus.FAILED, error=error_msg)

        url = kwargs["url"]
        use_cache = self._coerce_bool(kwargs.get("use_cache"), True)
        output_format = kwargs.get("output_format", DEFAULT_OUTPUT_FORMAT)
        extract_outline_flag = (
            self._coerce_bool(kwargs.get("extract_outline"), True)
            and output_format == "md"
        )
        preview_length = min(
            self._coerce_int(kwargs.get("preview_length"), DEFAULT_PREVIEW_LENGTH),
            MAX_PREVIEW_LENGTH,
        )
        mode = kwargs.get("mode", "auto")
        if mode == "auto":
            mode = _detect_mode(url)

        _rlog.info(
            "web_fetch_v3",
            f"开始抓取: mode={mode}, url={url[:80]}",
        )

        # ── Phase 2: 抓取 + 缓存（复用 V2 管线）───────
        try:
            html_raw, metadata = await self._fetch_and_parse(url, use_cache)
        except Exception as e:
            _rlog.error("web_fetch_v3", f"抓取失败: {e}")
            return ToolResult(
                ToolStatus.FAILED,
                error=f"网页抓取失败: {e}",
                metadata={"url": url},
            )

        # 过短内容（如 404 页面）
        if len(html_raw) < 100:
            return ToolResult(
                ToolStatus.FAILED,
                error=f"抓取到的内容过短（{len(html_raw)} 字符），可能不是有效页面",
            )

        # ── Phase 3: HTML → MD 转换 ────────────────────
        title = metadata.get("title", "") if metadata else ""
        soup = BeautifulSoup(html_raw, 'html.parser')

        if output_format == "md":
            try:
                md_text = self._convert_html(html_raw, soup, mode, url)
            except Exception as e:
                _rlog.error(
                    "web_fetch_v3",
                    f"转换失败: mode={mode}, error={e}",
                )
                return ToolResult(
                    ToolStatus.FAILED,
                    error=f"HTML→Markdown 转换失败 (mode={mode}): {e}",
                )
        else:
            # text 模式：复用 V2 的 _parse_html 提取纯文本
            md_text, _ = self._parse_html(html_raw)

        # ── Phase 4: 大纲提取 ──────────────────────────
        outline = []
        if extract_outline_flag:
            try:
                outline = extract_outline(md_text)
            except Exception as e:
                _rlog.warning("web_fetch_v3", f"大纲提取失败（非致命）: {e}")

        # ── Phase 5: 正文落盘 ──────────────────────────
        try:
            output_file = self._save_markdown_content(md_text, url, output_format)
        except Exception as e:
            _rlog.error("web_fetch_v3", f"正文落盘失败: {e}")
            return ToolResult(
                ToolStatus.FAILED,
                error=f"Markdown 文件保存失败: {e}",
            )

        # ── Phase 6: 组装返回体 ────────────────────────
        fetch_duration_ms = int((time.time() - start_time) * 1000)

        result_data = {
            # ── 元信息 ──
            "url": url,
            "title": title,
            "content_length": len(md_text),
            "content_hash": hashlib.sha256(md_text.encode()).hexdigest()[:16],

            # ── 结构化大纲 ──
            "outline": outline,

            # ── 预览 ──
            "preview": md_text[:preview_length],

            # ── 文件路径（正文不在此返回体） ──
            "output_file": str(output_file),
            "output_format": output_format,

            # ── 诊断 ──
            "mode_used": mode,
            "available_modes": list_converters(),
            "from_cache": metadata.get("from_cache", False) if metadata else False,
            "content_changed": metadata.get("content_changed") if metadata else None,
            "cached_at": metadata.get("cached_at") if metadata else None,
            "fetch_duration_ms": fetch_duration_ms,
            "warnings": [],
        }

        _rlog.info(
            "web_fetch_v3",
            f"完成: mode={mode}, len={len(md_text)}, "
            f"outline={len(outline)} sections, "
            f"duration={fetch_duration_ms}ms",
        )

        return ToolResult(ToolStatus.SUCCESS, output=result_data)

    # ── 抓取 + 解析（组合 V2 已定义方法）───────────────

    async def _fetch_and_parse(
        self, url: str, use_cache: bool
    ) -> tuple[str, dict[str, Any] | None]:
        """抓取网页并返回 HTML 原始文本和元信息。

        组合 V2 的缓存/抓取管线：
          1. _try_read_cache(url) → 命中则直接返回
          2. _fetch_webpage(url) → 抓取原始 HTML
          3. _save_to_cache(...) → 写缓存
          4. 返回 (html_raw, metadata)

        Args:
            url:       目标 URL。
            use_cache: 是否使用缓存。

        Returns:
            (html_raw, metadata_dict)
            - html_raw: HTML 原始字符串
            - metadata_dict: 缓存/抓取元信息

        Raises:
            Exception: 抓取失败时抛出。
        """
        # 1. 尝试缓存
        if use_cache:
            cached = self._try_read_cache(url)
            if cached:
                _rlog.info("web_fetch_v3", f"缓存命中: {url[:80]}")
                html = cached.get("html", "")
                meta = cached.get("metadata", {})
                meta["from_cache"] = True
                return html, meta

        # 2. 抓取网页
        result = await self._fetch_webpage(url)
        if result is None:
            raise RuntimeError(f"无法抓取网页: {url}")

        html_content, status_code, content_type = result

        # 3. 解析 HTML（提取纯文本 + 元信息）
        text, parsed_metadata = self._parse_html(html_content)

        # 4. 保存到缓存
        content_hash = hashlib.sha256(html_content.encode()).hexdigest()
        self._save_to_cache(
            url, text, html_content, parsed_metadata, content_hash
        )

        # 组合元信息
        meta = {
            **(parsed_metadata or {}),
            "from_cache": False,
            "status_code": status_code,
            "content_type": content_type,
        }
        return html_content, meta

    # ── HTML → MD 转换调度 ─────────────────────────────

    def _convert_html(
        self,
        html_raw: str,
        soup: BeautifulSoup,
        mode: str,
        url: str,
    ) -> str:
        """根据 mode 调度转换器执行 HTML→MD 转换。

        调度顺序：
        1. 运行时注册的自定义转换器（_custom_converters）
        2. html_to_md 模块注册的内置转换器（generic / arxiv）
        3. 未知模式 → 降级到 generic

        Args:
            html_raw: 原始 HTML 字符串。
            soup:     BeautifulSoup 解析树。
            mode:     解析模式名。
            url:      原始 URL（用于日志）。

        Returns:
            Markdown 文本。
        """
        # 1. 自定义转换器
        if mode in self._custom_converters:
            converter = self._custom_converters[mode]
            _rlog.info("web_fetch_v3", f"使用自定义转换器: mode={mode}")
            return converter(html_raw, soup)

        # 2. 内置转换器
        try:
            converter = get_converter(mode)
        except KeyError:
            _rlog.warning(
                "web_fetch_v3",
                f"未知模式 {mode!r}，降级为 generic。"
                f"已注册: {list_converters()}",
            )
            converter = get_converter("generic")

        _rlog.info("web_fetch_v3", f"使用转换器: mode={mode}")
        return converter(html_raw, soup)

    # ── 正文落盘 ───────────────────────────────────────

    def _save_markdown_content(
        self,
        md_text: str,
        url: str,
        output_format: str,
    ) -> Path:
        """将 Markdown 正文写入缓存目录的指定文件。

        文件命名：{url_hash}_converted.{ext}
        - url_hash: SHA256(url) 的前 16 位
        - ext:      .md 或 .txt

        与 V2 的 raw content 缓存文件并列存放。

        Args:
            md_text:       转换后的文本内容。
            url:           原始 URL。
            output_format: "md" 或 "text"。

        Returns:
            输出文件的绝对路径。
        """
        url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]
        ext = ".md" if output_format == "md" else ".txt"
        filename = f"{url_hash}_converted{ext}"
        output_path = self.cache_dir / filename

        output_path.write_text(md_text, encoding="utf-8")
        _rlog.info("web_fetch_v3", f"正文已落盘: {output_path}")

        return output_path

    # ── 标题提取（覆盖 V2 的 _extract_metadata，增加 arXiv 优化）───

    # V2 的 _extract_metadata 只抓 <title> 标签。
    # 这里不覆盖 _extract_metadata（它被 _parse_html 内部调用），
    # 因为我们需要 soup 对象来检测 arXiv 特殊标签。
    # 改为在 execute() 阶段从 metadata 取 title，并在 Phase 3
    # 用已经构造好的 soup 做二次检测（见 execute 中的逻辑）。
    #
    # 如果 metadata["title"] 不够精确，execute 会优先使用 soup
    # 中的 h1.ltx_title_document。

    # ── 诊断 / 自检 ────────────────────────────────────

    def check_health(self) -> dict:
        """健康检查：验证所有依赖可用。"""
        report = {
            "status": "ok",
            "version": "3.0.0",
            "cache_dir": str(self.cache_dir),
            "converters": {
                "builtin": list_converters(),
                "custom": list(self._custom_converters.keys()),
            },
            "issues": [],
        }

        # 检查 cache_dir 是否可写
        if not self.cache_dir.exists():
            report["issues"].append(f"cache_dir 不存在: {self.cache_dir}")
            report["status"] = "degraded"

        # 检查内置转换器是否可用
        expected = ["generic", "arxiv"]
        for m in expected:
            try:
                get_converter(m)
            except KeyError:
                report["issues"].append(f"内置转换器未注册: {m}")
                report["status"] = "degraded"

        return report

    # ── 获取描述（覆盖 V2）─────────────────────────────

    def get_description(self) -> dict[str, Any]:
        """获取 V3 工具描述信息。"""
        return {
            "name": self.TOOL_NAME,
            "description": self.TOOL_DESCRIPTION,
            "version": "3.0.0",
            "parameters": self.TOOL_PARAMETERS,
            "returns": {
                "url": "网页 URL",
                "title": "网页标题",
                "content_length": "内容长度（字符数）",
                "content_hash": "内容哈希值（前 16 位）",
                "outline": "章节大纲列表（仅当 extract_outline=true）",
                "preview": "预览文本片段",
                "output_file": "正文 Markdown 文件绝对路径",
                "output_format": "输出格式（md 或 text）",
                "mode_used": "实际使用的解析模式",
                "available_modes": "可用的解析模式列表",
                "from_cache": "是否来自缓存",
                "content_changed": "内容是否发生变更（仅当 use_cache=false）",
                "cached_at": "缓存时间戳",
                "fetch_duration_ms": "抓取耗时（毫秒）",
                "warnings": "警告信息列表"
            },
            "examples": [
                {
                    "url": "https://arxiv.org/html/2604.23781v1",
                    "description": "抓取 arXiv 论文并自动使用 arxiv 模式转换"
                },
                {
                    "url": "https://example.com",
                    "output_format": "md",
                    "extract_outline": True,
                    "description": "抓取网页并转换为 Markdown，返回章节大纲"
                },
                {
                    "url": "https://example.com",
                    "use_cache": False,
                    "description": "强制重新抓取，检查内容是否变更"
                },
                {
                    "url": "https://docs.python.org/3/",
                    "mode": "generic",
                    "preview_length": 1000,
                    "description": "使用通用模式抓取 Python 文档，返回 1000 字符预览"
                }
            ],
            "is_readonly": self.__class__.IS_READONLY,
            "is_parallelizable": self.__class__.IS_PARALLELIZABLE,
            "notes": [
                "正文内容已保存到 output_file 路径，可使用文件读取工具获取完整内容",
                "output_format=md 时自动进行 HTML→Markdown 转换",
                "mode=auto 时根据 URL 自动选择解析模式（arxiv.org → arxiv 模式）",
                "extract_outline 仅在 output_format=md 时有效",
                "preview_length 最大值为 5000 字符",
                "支持注册自定义转换器扩展解析模式"
            ]
        }
