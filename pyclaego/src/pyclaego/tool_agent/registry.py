"""ToolAgent 注册表 — 全局子代理配置注册中心

取代原 context/agent_pipeline/subagent_profile.py 中的 SubagentProfile 体系。
所有子代理现在统一使用 ToolAgentConfig 作为唯一数据模型。

注册表操作：
  - register_profile: 动态注册 ToolAgentConfig
  - get_profile:      按名称查找
  - resolve_profile:  合并 YAML 级覆盖（LLM / max_tool_rounds / skills / workspace）
  - list_profile_names: 列出所有已注册名称
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from .config import ToolAgentConfig

# ===========================================================================
# 全局注册表（进程级单例，由 ToolAgentManager 填充）
# ===========================================================================

SUBAGENT_PROFILES: dict[str, ToolAgentConfig] = {}


def register_profile(profile: ToolAgentConfig) -> None:
    """动态注册一个子代理类型（从磁盘 config.json 或 YAML 加载）。

    注册后，DynamicSpawnSubagentTool 的 LLM 工具描述会自动包含该类型。

    Args:
        profile: ToolAgentConfig 实例

    Raises:
        ValueError: 如果 name 已存在
    """
    if profile.name in SUBAGENT_PROFILES:
        raise ValueError(
            f"子代理类型 '{profile.name}' 已注册。"
            f"如需覆盖，请先调用 unregister_profile('{profile.name}')。"
        )
    SUBAGENT_PROFILES[profile.name] = profile


def unregister_profile(name: str) -> None:
    """注销一个子代理类型。

    Args:
        name: 子代理类型名称

    Raises:
        KeyError: 如果未注册
    """
    del SUBAGENT_PROFILES[name]


def get_profile(name: str) -> ToolAgentConfig:
    """按名称查找 ToolAgentConfig（原始值，不含 YAML 覆盖）。

    Raises:
        KeyError: 如果未注册
    """
    return SUBAGENT_PROFILES[name]


def resolve_profile(
    name: str,
    base_config: dict[str, Any],
) -> ToolAgentConfig:
    """解析子代理的最终配置，合并 widget 级 YAML 覆盖。

    解析优先级（LLM）：
        1. base_config["subagents"][name]["llm"]     — widget 级 YAML 配置
        2. base_config["llm"]                        — 继承主 Agent
        3. "kimi_code"                               — 最终兜底

    max_tool_rounds / skills / workspace 也优先从 base_config["subagents"][name] 读取覆盖。

    Args:
        name:        子代理类型标识
        base_config: 主 Agent 的 agent 配置切片（含 llm / subagents 等字段）

    Returns:
        解析后的 ToolAgentConfig（已合并覆盖值）

    Raises:
        KeyError: 如果 name 未在 SUBAGENT_PROFILES 注册
    """
    profile = get_profile(name)
    subagent_cfg = base_config.get("subagents", {}).get(name, {})

    # ── LLM 解析 ──────────────────────────────────────────────────────
    if subagent_cfg.get("llm"):
        resolved_llm = subagent_cfg["llm"]
    elif profile.llm:
        resolved_llm = profile.llm
    elif base_config.get("llm"):
        resolved_llm = base_config["llm"]
    else:
        resolved_llm = "kimi_code"

    # ── max_tool_rounds 解析 ───────────────────────────────────────────
    resolved_rounds: int = profile.max_tool_rounds
    if "max_tool_rounds" in subagent_cfg:
        resolved_rounds = int(subagent_cfg["max_tool_rounds"])

    # ── skills 解析（YAML 级覆盖）─────────────────────────────────────
    resolved_skills = list(profile.skills)
    if "skills" in subagent_cfg:
        raw = subagent_cfg["skills"]
        if isinstance(raw, list):
            resolved_skills = [str(s) for s in raw]

    # ── workspace 解析（YAML 级覆盖）───────────────────────────────────
    resolved_workspace = profile.workspace
    if "workspace" in subagent_cfg:
        resolved_workspace = str(subagent_cfg["workspace"])

    return replace(
        profile,
        llm=resolved_llm,
        max_tool_rounds=resolved_rounds,
        skills=resolved_skills,
        workspace=resolved_workspace,
    )


def list_profile_names() -> list:
    """列出所有已注册的子代理类型名称。"""
    return list(SUBAGENT_PROFILES.keys())
