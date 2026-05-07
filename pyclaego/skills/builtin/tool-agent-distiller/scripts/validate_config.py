#!/usr/bin/env python3
"""validate_config.py — 校验 ToolAgent config.json 合法性

使用 ToolAgentConfig.from_json() 加载并调用 validate()，
将内置校验规则全量应用到目标文件。

Usage:
    python validate_config.py <path-to-config.json>

Exit codes:
    0 — 校验通过
    1 — 校验失败（打印详细错误）
    2 — 参数错误
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python validate_config.py <path-to-config.json>", file=sys.stderr)
        sys.exit(2)

    config_path = Path(sys.argv[1])

    # 延迟导入，确保在项目 venv 中运行
    try:
        from pyclaego.tool_agent.config import ToolAgentConfig
        from pyclaego.tool_agent.exceptions import ToolAgentConfigError
    except ImportError as exc:
        print(
            f"[ERROR] 无法导入 pyclaego 模块，请在项目 venv 中运行此脚本。\n  {exc}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        cfg = ToolAgentConfig.from_json(config_path)
    except FileNotFoundError:
        print(f"[ERROR] 文件不存在: {config_path}", file=sys.stderr)
        sys.exit(1)
    except ToolAgentConfigError as exc:
        print(f"[FAIL] 校验失败:\n  {exc}", file=sys.stderr)
        sys.exit(1)

    # 额外输出关键摘要，方便快速确认
    print(f"[OK] {cfg.name}")
    print(f"     description   : {cfg.description[:80]}{'...' if len(cfg.description) > 80 else ''}")
    print(f"     subagent_type : {cfg.subagent_type}")
    print(f"     context_strategy: {cfg.context_strategy}")
    print(f"     max_tool_rounds : {cfg.max_tool_rounds}")
    if cfg.uses_all_tools:
        print("     allowed_tools : [*] (all tools)")
    else:
        print(f"     allowed_tools : {cfg.allowed_tools}")
    if cfg.uses_all_skills:
        print("     skills        : [*] (all skills)")
    else:
        print(f"     skills        : {cfg.skills}")
    print(f"     llm           : {cfg.llm or '(inherited)'}")
    print(f"     temperature   : {cfg.temperature if cfg.temperature is not None else '(default)'}")


if __name__ == "__main__":
    main()
