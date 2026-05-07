"""ToolAgentConfig — 磁盘驱动的子代理配置数据类

config.json 支持的全部字段见 ToolAgentConfig 文档。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .exceptions import ToolAgentConfigError

# ===========================================================================
# 常量
# ===========================================================================

VALID_CONTEXT_STRATEGIES: frozenset[str] = frozenset({
    "none",
    "summarizing",
    "soulv6",
    "fork",
})

VALID_SUBAGENT_TYPES: frozenset[str] = frozenset({
    "universal",
    # 未来扩展: "echo", "chain", "react"
})

SENTINEL_ALL = "*"


# ===========================================================================
# ToolAgentConfig
# ===========================================================================


@dataclass(frozen=True)
class ToolAgentConfig:
    """磁盘 config.json 驱动的子代理配置（唯一数据模型）。

    支持 "*" 通配符用于 allowed_tools 和 skills。
    比原 SubagentProfile 多了 workspace / skills / subagent_type / metadata 字段。

    Attributes:
        name:             子代理名称（必须与目录名一致）
        description:      给 LLM 看的类型描述
        system_prompt:    系统提示词模板，支持 {workspace_path} {project_root}
        subagent_type:    编排逻辑类型（默认 "universal"）
        allowed_tools:    工具白名单原始列表（["*"] 表示全部，[] 表示无工具）
        max_tool_rounds:  最大工具调用轮次
        context_strategy: 上下文管理策略
        llm:              LLM provider ID（空=继承父 Agent）
        temperature:      LLM 温度参数
        workspace:        工作目录（相对于 config.json 所在目录）
        skills:           技能列表（["*"] 表示全部，[] 表示无技能）
        metadata:         扩展元数据
        _source_path:     config.json 的目录路径（内部使用，不序列化）
    """

    name: str
    description: str = ""
    system_prompt: str = ""
    subagent_type: str = "universal"
    allowed_tools: list[str] = field(default_factory=list)
    max_tool_rounds: int = 20
    context_strategy: str = "none"
    llm: str = ""
    temperature: float | None = None
    workspace: str = "./workspace"
    skills: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    _source_path: Path | None = field(default=None, repr=False, compare=False)

    # ------------------------------------------------------------------
    # 工厂方法
    # ------------------------------------------------------------------

    @classmethod
    def from_json(cls, json_path: Path) -> ToolAgentConfig:
        """从 config.json 文件加载 ToolAgentConfig。

        Args:
            json_path: config.json 文件的路径（Path 或 str）

        Returns:
            ToolAgentConfig 实例

        Raises:
            ToolAgentConfigError: JSON 解析失败或校验失败
            FileNotFoundError: config.json 不存在
        """
        json_path = Path(json_path)
        if not json_path.exists():
            raise FileNotFoundError(f"config.json 不存在: {json_path}")

        try:
            raw = json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ToolAgentConfigError(
                f"config.json JSON 解析失败: {json_path}\n{exc}"
            ) from exc

        if not isinstance(raw, dict):
            raise ToolAgentConfigError(
                f"config.json 顶层必须是 JSON 对象 (dict)，实际为 {type(raw).__name__}: {json_path}"
            )

        source_dir = json_path.parent.resolve()

        # 目录名作为默认 name
        raw.setdefault("name", source_dir.name)

        config = cls(
            name=str(raw.get("name", source_dir.name)),
            description=str(raw.get("description", "")),
            system_prompt=str(raw.get("system_prompt", "")),
            subagent_type=str(raw.get("subagent_type", "universal")),
            allowed_tools=_parse_allowed_tools(raw.get("allowed_tools")),
            max_tool_rounds=int(raw.get("max_tool_rounds", 20)),
            context_strategy=str(raw.get("context_strategy", "none")),
            llm=str(raw.get("llm", "")),
            temperature=raw.get("temperature"),  # None 或 float
            workspace=str(raw.get("workspace", "./workspace")),
            skills=_parse_skills(raw.get("skills")),
            metadata=raw.get("metadata", {}) if isinstance(raw.get("metadata"), dict) else {},
            _source_path=source_dir,
        )

        config.validate()
        return config

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------

    def validate(self) -> None:
        """校验配置完整性。

        Raises:
            ToolAgentConfigError: 校验失败
        """
        if not self.name or not self.name.strip():
            raise ToolAgentConfigError("name 不能为空")

        if not self.description or not self.description.strip():
            raise ToolAgentConfigError(f"ToolAgent '{self.name}': description 不能为空")

        if not self.system_prompt or not self.system_prompt.strip():
            raise ToolAgentConfigError(f"ToolAgent '{self.name}': system_prompt 不能为空")

        if self.context_strategy not in VALID_CONTEXT_STRATEGIES:
            raise ToolAgentConfigError(
                f"ToolAgent '{self.name}': context_strategy 无效值 '{self.context_strategy}'，"
                f"可选值: {sorted(VALID_CONTEXT_STRATEGIES)}"
            )

        if self.subagent_type not in VALID_SUBAGENT_TYPES:
            raise ToolAgentConfigError(
                f"ToolAgent '{self.name}': subagent_type 无效值 '{self.subagent_type}'，"
                f"可选值: {sorted(VALID_SUBAGENT_TYPES)}"
            )

        if self.max_tool_rounds < 1:
            raise ToolAgentConfigError(
                f"ToolAgent '{self.name}': max_tool_rounds 必须 >= 1，当前值: {self.max_tool_rounds}"
            )

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def source_dir(self) -> Path | None:
        """config.json 所在目录（绝对路径）。"""
        return self._source_path

    @property
    def resolved_workspace(self) -> Path | None:
        """解析后的工作目录绝对路径。"""
        if self._source_path is None:
            return None
        ws = Path(self.workspace)
        if ws.is_absolute():
            return ws
        return (self._source_path / ws).resolve()

    @property
    def uses_all_tools(self) -> bool:
        """是否配置为使用全部工具。"""
        return len(self.allowed_tools) == 1 and self.allowed_tools[0] == SENTINEL_ALL

    @property
    def uses_all_skills(self) -> bool:
        """是否配置为使用全部技能。"""
        return len(self.skills) == 1 and self.skills[0] == SENTINEL_ALL

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------

    def render_system_prompt(self, **kwargs: str) -> str:
        """渲染系统提示词模板。

        Args:
            **kwargs: 占位符键值对（如 workspace_path="/tmp/...", project_root="/src"）

        Returns:
            渲染后的提示词字符串
        """
        try:
            return self.system_prompt.format(**kwargs)
        except KeyError:
            # 模板中引用了未提供的占位符 — 尽力渲染
            return self.system_prompt


# ===========================================================================
# 内部解析辅助
# ===========================================================================


def _parse_allowed_tools(raw: Any) -> list[str]:
    """解析 allowed_tools 字段。

    Args:
        raw: 原始值（list[str] 或 "*" 字符串）

    Returns:
        标准化后的工具名列表
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        s = raw.strip()
        if s == SENTINEL_ALL:
            return [SENTINEL_ALL]
        return [s] if s else []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []


def _parse_skills(raw: Any) -> list[str]:
    """解析 skills 字段。

    Args:
        raw: 原始值（list[str] 或 "*" 字符串）

    Returns:
        标准化后的技能名列表
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        s = raw.strip()
        if s == SENTINEL_ALL:
            return [SENTINEL_ALL]
        return [s] if s else []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []
