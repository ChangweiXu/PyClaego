"""ReadPdfTool - 读取 PDF 文件，提取文本并格式化为 Markdown，缓存结果

6 阶段流水线:
  1. 参数校验 + 安全检查
  2. 缓存检查（key = sha256(path + mtime)）
  3. PDF 文本提取（pdf_to_md 子模块，可插拔后端）
  4. Markdown 格式化 + 大纲提取
  5. 正文落盘（~/.pyclaego/.cache/pdf_md/{hash}/）
  6. 组装返回体（含 cache MD 路径 + 章节行号映射）

返回给 Agent 的数据不包含完整 MD 正文（避免 ToolResult 膨胀），
仅返回 output_file 路径 + outline + preview。Agent 可通过 read_file
按需读取正文。
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from ...config import PYCLAEGO_DEFAULT_CACHE_ROOT
from ...llm.types import DocumentPart, TextPart
from ...logging import get_running_log

# 复用 html_to_md 的大纲提取
from ...tool.tools.html_to_md.outline import extract_outline
from ..base_tool import ToolResult, ToolStatus
from .fs_base_tool import FileSystemBaseTool

# pdf_to_md 子模块
from .pdf_to_md import PdfExtractionError, extract_text, pages_to_markdown

_rlog = get_running_log()

# ── 常量 ──────────────────────────────────────────────

DEFAULT_PREVIEW_LENGTH = 500
MAX_PREVIEW_LENGTH = 5000
DEFAULT_PDF_CACHE_DIR = str(Path(PYCLAEGO_DEFAULT_CACHE_ROOT) / "pdf_md")

# PDF DocumentPart 最大原始字节数（超过此值不纳入 content_parts，
# 避免 LLM API 413 错误。Anthropic 请求体限制约 10MB，base64 膨胀 ~33%）
MAX_CONTENT_PART_BYTES = 8 * 1024 * 1024  # 8MB raw ≈ 10.7MB base64


class ReadPdfTool(FileSystemBaseTool):
    """PDF 文件读取工具（增强版）

    功能：
    - 从 PDF 提取文本并格式化为结构化 Markdown
    - Markdown 正文缓存到 ~/.pyclaego/.cache/pdf_md/
    - 返回章节大纲（标题 → 行号映射），支持按章节定位
    - 保留原始 PDF DocumentPart（兼容 Anthropic/Gemini 原生 PDF）
    - 遵循文件大小限制（max_file_size）

    配置示例：
    ```yaml
    read_pdf:
      tool_type: "read_pdf"
      tool_name: "read_pdf"
      enabled: true
      working_dir: null
      max_file_size: 31457280        # 30MB
      allowed_paths: []
      blocked_paths: []
      # 增强配置
      pdf_cache_dir: null            # null 则使用默认 ~/.pyclaego/.cache/pdf_md/
      enable_md_cache: true          # 启用 MD 缓存
      extract_md: true               # 提取并格式化 MD
      extract_outline: true          # 提取章节大纲
      preview_length: 500            # 预览长度
      pdf_backend: null              # "pypdf2" / "pdfplumber" / null（自动选择）
    ```
    """

    IS_READONLY: bool = True
    IS_PARALLELIZABLE: bool = True

    # ── 初始化 ─────────────────────────────────────────

    def __init__(self, tool_config: dict[str, Any]):
        super().__init__(tool_config)

        # 缓存目录
        self._pdf_cache_dir = Path(
            tool_config.get("pdf_cache_dir") or DEFAULT_PDF_CACHE_DIR
        )
        self._pdf_cache_dir.mkdir(parents=True, exist_ok=True)

        # 功能开关
        self._enable_md_cache = tool_config.get("enable_md_cache", True)
        self._extract_md = tool_config.get("extract_md", True)
        self._extract_outline = tool_config.get("extract_outline", True)
        self._preview_length = min(
            int(tool_config.get("preview_length", DEFAULT_PREVIEW_LENGTH)),
            MAX_PREVIEW_LENGTH,
        )
        self._pdf_backend: str | None = tool_config.get("pdf_backend")

    # ── 工具注册元信息 ─────────────────────────────────

    TOOL_TYPE = "read_pdf"
    TOOL_NAME = "read_pdf"

    # ── 路径提取 ───────────────────────────────────────

    def extract_paths(self, args: dict[str, Any]) -> dict[str, list]:
        p = args.get("path")
        return {"read": [p] if isinstance(p, str) and p.strip() else [], "write": []}

    @staticmethod
    def _try_get_page_count(path: Path) -> int:
        """快速获取 PDF 页数（仅读元数据，不提取全文）。"""
        try:
            from PyPDF2 import PdfReader
            return len(PdfReader(str(path)).pages)
        except Exception:
            return 0

    # ── execute() 6 阶段流水线 ─────────────────────────

    async def execute(self, **kwargs) -> ToolResult:
        """主入口：校验 → 缓存 → 提取 → 格式化 → 大纲 → 落盘 → 返回。

        Args:
            path:            PDF 文件路径。
            extract_md:      是否提取并格式化 Markdown（覆盖配置，默认 true）。
            extract_outline: 是否提取章节大纲（覆盖配置，默认 true）。
            preview_length:  预览字符数（覆盖配置，默认 500，最大 5000）。
            force:           强制重新提取，忽略缓存（默认 false）。

        Returns:
            ToolResult: output 包含结构化元信息 + content_parts 保留原始 PDF。
        """
        start_time = time.time()

        # ── Phase 1: 参数校验 + 安全检查 ────────────────
        valid, error_msg = self.validate_params(["path"], **kwargs)
        if not valid:
            return ToolResult(status=ToolStatus.FAILED, error=error_msg)

        path = self._resolve_path(kwargs["path"])

        ok, err = self._security_check(path, require_exists=True, must_be_file=True)
        if not ok:
            return ToolResult(status=ToolStatus.FAILED, error=err)

        ok, err = self._check_file_size(path)
        if not ok:
            return ToolResult(status=ToolStatus.FAILED, error=err)

        if path.suffix.lower() != ".pdf":
            return ToolResult(
                status=ToolStatus.FAILED,
                error=f"不是 PDF 文件: {path} (扩展名: {path.suffix})",
            )

        # 运行时覆盖配置项
        extract_md_flag = kwargs.get("extract_md", self._extract_md)
        extract_outline_flag = kwargs.get("extract_outline", self._extract_outline)
        preview_len = min(
            int(kwargs.get("preview_length", self._preview_length)),
            MAX_PREVIEW_LENGTH,
        )
        force = kwargs.get("force", False)

        # ── 读取原始 PDF 二进制（始终需要，用于 content_parts）──
        try:
            with open(path, "rb") as f:
                raw_bytes = f.read()
        except Exception as e:
            return ToolResult(
                status=ToolStatus.FAILED,
                error=f"读取 PDF 文件失败: {e}",
            )

        file_size = len(raw_bytes)
        b64_str = base64.b64encode(raw_bytes).decode("ascii")

        # 计算缓存 key
        pdf_mtime = path.stat().st_mtime
        cache_key = hashlib.sha256(
            f"{path.resolve()}|{pdf_mtime}".encode()
        ).hexdigest()[:16]
        cache_dir = self._pdf_cache_dir / cache_key
        md_cache_path = cache_dir / "content.md"
        outline_cache_path = cache_dir / "outline.json"

        # ── Phase 2: 缓存检查 ──────────────────────────
        from_cache = False
        md_text: str | None = None
        outline_data: list = []

        if (
            self._enable_md_cache
            and extract_md_flag
            and not force
            and md_cache_path.exists()
            and outline_cache_path.exists()
        ):
            try:
                md_text = md_cache_path.read_text(encoding="utf-8")
                outline_data = json.loads(
                    outline_cache_path.read_text(encoding="utf-8")
                )
                from_cache = True
                _rlog.info(
                    "core_service",
                    f"PDF MD 缓存命中: {path.name} -> {cache_key}",
                )
            except Exception as e:
                _rlog.warning(
                    "core_service",
                    f"PDF MD 缓存读取失败，将重新提取: {e}",
                )
                from_cache = False
                md_text = None
                outline_data = []

        # ── Phase 3: PDF 文本提取 + MD 格式化 ───────────
        page_count = 0
        if md_text is None and extract_md_flag:
            try:
                _rlog.info("core_service", f"从 PDF 提取文本: {path}")

                # 3a. 提取逐页文本
                pages, page_count = extract_text(
                    path, backend=self._pdf_backend
                )

                # 3b. 格式化为 Markdown
                md_text = pages_to_markdown(pages, pdf_path=path)

                _rlog.info(
                    "core_service",
                    f"PDF 文本提取完成: {path.name}, "
                    f"pages={page_count}, md_chars={len(md_text)}",
                )

            except PdfExtractionError as e:
                _rlog.error("core_service", f"PDF 文本提取失败: {e}")
                md_text = None
                page_count = self._try_get_page_count(path)
            except Exception as e:
                _rlog.error("core_service", f"PDF 处理异常: {e}")
                md_text = None
                page_count = self._try_get_page_count(path)

        # ── Phase 4: 大纲提取 ──────────────────────────
        if md_text and extract_outline_flag and not from_cache:
            try:
                outline_data = extract_outline(md_text)
                _rlog.info(
                    "core_service",
                    f"PDF 大纲提取: {len(outline_data)} 个章节",
                )
            except Exception as e:
                _rlog.warning("core_service", f"大纲提取失败（非致命）: {e}")
                outline_data = []

        # ── Phase 5: 正文落盘 ──────────────────────────
        if md_text and self._enable_md_cache and not from_cache:
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                md_cache_path.write_text(md_text, encoding="utf-8")
                outline_cache_path.write_text(
                    json.dumps(outline_data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                _rlog.info(
                    "core_service",
                    f"PDF MD 缓存已写入: {md_cache_path}",
                )
            except Exception as e:
                _rlog.warning("core_service", f"缓存写入失败（非致命）: {e}")

        # ── Phase 6: 组装返回体 ─────────────────────────
        elapsed_ms = int((time.time() - start_time) * 1000)

        # 智能选择 content_parts：优先用 MD 文本，避免超大 PDF 撑爆 LLM 请求
        warnings: list[str] = []
        content_parts = []
        content_part_too_large = False

        if md_text:
            # ✅ 文本提取成功 — 返回 TextPart 摘要，不附加 DocumentPart
            # Agent 通过 md_path + outline 按需读取全文
            content_parts = [
                TextPart(
                    text=(
                        f"PDF 文本已提取并保存为 Markdown。\n"
                        f"文件路径: {md_cache_path}\n"
                        f"总字符数: {len(md_text)}\n"
                        f"大纲条目: {len(outline_data)} 个章节\n\n"
                        f"--- 预览（前 {preview_len} 字符）---\n"
                        f"{md_text[:preview_len]}"
                    )
                )
            ]
            text_summary = content_parts[0].text
        elif file_size <= MAX_CONTENT_PART_BYTES:
            # ⚠️ 文本提取失败，PDF 不大 — 回退 DocumentPart
            content_parts = [
                DocumentPart(
                    source_type="base64",
                    data=b64_str,
                    media_type="application/pdf",
                )
            ]
            text_summary = (
                f"PDF 文件: {path.name} ({file_size} bytes, {page_count} 页)。"
                f"文本提取不可用（PyPDF2/pdfplumber 未安装或提取失败），"
                f"请通过原生 PDF 支持查看。"
            )
            warnings.append("text_extraction_failed: 文本提取失败，返回原始 PDF")
        else:
            # ❌ 文本提取失败 且 PDF 过大 — 不返回 content_parts，仅文本说明
            content_part_too_large = True
            text_summary = (
                f"⚠️ PDF 文件过大无法直接传输: {path.name}\n"
                f"   文件大小: {file_size / 1024 / 1024:.1f} MB ({page_count} 页)\n"
                f"   文本提取不可用（PyPDF2/pdfplumber 未安装或提取失败）。\n\n"
                f"建议操作：\n"
                f"1. 确认 PyPDF2 已安装: pip install PyPDF2\n"
                f"2. 或安装 pdfplumber 获得更好的提取效果: pip install pdfplumber\n"
                f"3. 安装后重新调用 read_pdf(force=true) 强制重新提取"
            )
            warnings.append(
                "content_part_too_large: PDF 过大（>8MB）且文本提取失败，"
                "跳过二进制传输"
            )

        result_data: dict[str, Any] = {
            # ── 元信息 ──
            "pdf_path": str(path),
            "file_size": file_size,
            "page_count": page_count,

            # ── Markdown 提取结果 ──
            "md_available": md_text is not None,
            "md_path": str(md_cache_path) if md_text else None,
            "content_length": len(md_text) if md_text else 0,
            "content_hash": (
                hashlib.sha256(md_text.encode()).hexdigest()[:16]
                if md_text else None
            ),

            # ── 结构化大纲（章节→行号映射）──
            "outline": outline_data,

            # ── 预览 ──
            "preview": md_text[:preview_len] if md_text else None,

            # ── 诊断 ──
            "from_cache": from_cache,
            "content_part_too_large": content_part_too_large,
            "extraction_duration_ms": elapsed_ms,
            "warnings": warnings if warnings else None,
        }

        _rlog.info(
            "core_service",
            f"PDF 读取完成: {path.name}, "
            f"md_available={result_data['md_available']}, "
            f"outline={len(outline_data)} sections, "
            f"from_cache={from_cache}, "
            f"duration={elapsed_ms}ms",
        )

        return ToolResult(
            status=ToolStatus.SUCCESS,
            output=result_data,
            metadata={
                "path": str(path),
                "media_type": "application/pdf",
                "file_size": file_size,
            },
            content_parts=content_parts,
        )

    # ── get_description ────────────────────────────────

    def get_description(self) -> dict[str, Any]:
        return {
            "name": self.tool_name,
            "description": (
                "读取本地 PDF 文件并提取文本内容格式化为 Markdown 文档。\n"
                "Markdown 正文自动缓存到本地，返回缓存文件路径和章节大纲（标题→行号映射）。\n"
                "支持的 LLM（Anthropic/Gemini）可直接查看原生 PDF；"
                "不支持的 LLM 通过 output_file 路径按需读取 Markdown 全文。\n\n"
                "**推荐使用方式**：\n"
                "1. 调用 read_pdf 获取 outline 和 md_path\n"
                "2. 根据 outline 找到目标章节的行号\n"
                "3. 使用 read_file(offset=line, limit=N) 按需读取章节内容"
            ),
            "parameters": {
                "path": {
                    "type": "string",
                    "required": True,
                    "description": "PDF 文件路径",
                },
                "extract_md": {
                    "type": "boolean",
                    "required": False,
                    "description": (
                        "是否提取并格式化 Markdown（默认：true）。"
                        "设为 false 则仅返回原始 PDF 二进制"
                    ),
                },
                "extract_outline": {
                    "type": "boolean",
                    "required": False,
                    "description": (
                        "是否从 MD 中提取章节大纲（默认：true）。"
                        "仅在 extract_md=true 时有效"
                    ),
                },
                "preview_length": {
                    "type": "integer",
                    "required": False,
                    "description": (
                        "预览文本的前 N 个字符"
                        f"（默认：{DEFAULT_PREVIEW_LENGTH}，"
                        f"最大：{MAX_PREVIEW_LENGTH}）"
                    ),
                },
                "force": {
                    "type": "boolean",
                    "required": False,
                    "description": "是否强制重新提取并忽略缓存（默认：false）",
                },
            },
            "examples": [
                {
                    "path": "docs/paper.pdf",
                    "description": "读取 PDF 论文，自动提取 Markdown 文本和章节大纲",
                },
                {
                    "path": "docs/report.pdf",
                    "extract_outline": True,
                    "description": "读取 PDF 报告，提取章节导航",
                },
                {
                    "path": "docs/slides.pdf",
                    "extract_md": False,
                    "description": "仅读取原始 PDF（不提取文本），适合图表为主的文档",
                },
            ],
        }
