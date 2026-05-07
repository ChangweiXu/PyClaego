#!/usr/bin/env python3
"""init_tool_agent.py — 生成 ToolAgent config.json 骨架

根据指定模板在目标目录创建 <name>/config.json，
包含完整字段和 TODO 占位符，填写后可通过 validate_config.py 校验。

Usage:
    python init_tool_agent.py --name <agent-name> --dir <target-dir> [--template echo|explorer|pipeline]

Arguments:
    --name      Agent 名称（小写字母 + 数字 + 下划线，以字母开头）
    --dir       目标父目录（config.json 写入 <dir>/<name>/config.json）
    --template  预设模板复杂度（默认: explorer）
                  echo     — 无工具，单次 LLM 调用
                  explorer — 只读文件工具，中等轮次
                  pipeline — 全工具，最高轮次

Examples:
    python init_tool_agent.py --name pr_analyzer --dir tool_agents/builtin
    python init_tool_agent.py --name doc_extractor --dir ~/.pyclaego/tool_agents --template pipeline
    python init_tool_agent.py --name rewrite_helper --dir ./my_agents --template echo
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 名称校验
# ---------------------------------------------------------------------------

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def validate_name(name: str) -> None:
    if not NAME_PATTERN.match(name):
        print(
            f"[ERROR] 无效的 Agent 名称: '{name}'\n"
            "  规则：小写字母开头，仅含小写字母、数字、下划线。\n"
            "  示例：code_explorer, pr_analyzer, doc_extractor",
            file=sys.stderr,
        )
        sys.exit(2)


# ---------------------------------------------------------------------------
# 模板定义
# ---------------------------------------------------------------------------

# 三档模板：echo / explorer / pipeline
TEMPLATES: dict[str, dict] = {
    "echo": {
        "allowed_tools": [],
        "max_tool_rounds": 1,
        "context_strategy": "none",
        "skills": [],
        "_hint_tools": "无工具（纯文本生成）",
        "_hint_rounds": "1（单次 LLM 调用）",
        "_hint_strategy": "none（无需跨轮记忆）",
    },
    "explorer": {
        "allowed_tools": [
            "file_info",
            "find_line",
            "glob",
            "list_directory",
            "mkdir",
            "read_file",
            "search_text",
            "write_file",
        ],
        "max_tool_rounds": 15,
        "context_strategy": "summarizing",
        "skills": [],
        "_hint_tools": "只读文件工具 + 工作目录写入",
        "_hint_rounds": "15（中等复杂探索任务）",
        "_hint_strategy": "summarizing（自动压缩旧消息，适合多轮任务）",
    },
    "pipeline": {
        "allowed_tools": ["*"],
        "max_tool_rounds": 55,
        "context_strategy": "summarizing",
        "skills": ["*"],
        "_hint_tools": "全工具（bash、网络、文件读写、代码执行等）",
        "_hint_rounds": "55（最复杂任务上限）",
        "_hint_strategy": "summarizing（自动压缩旧消息）",
    },
}


def build_config(name: str, template_name: str) -> dict:
    tpl = TEMPLATES[template_name]
    return {
        "name": name,
        "description": "TODO: 填写 Agent 能力描述（面向 LLM 决策层，说明何时召唤此 Agent）",
        "system_prompt": (
            "# Sub-Agent Identity\n"
            "你是 PyClaego 的 TODO 子 Agent（TODO英文简称）。\n"
            "你被主 Agent 创建，专门用于 TODO：核心职责一句话。\n\n"
            "## 工作目录\n"
            "当前工作目录：`{workspace_path}`\n\n"
            "## 工作流步骤\n\n"
            "### Step 1 — TODO\n"
            "TODO: 描述第一步操作\n\n"
            "### Step 2 — TODO\n"
            "TODO: 描述第二步操作\n\n"
            "## 输出规范\n"
            "完成后将结果整理在最后一条回复中。\n\n"
            "## 约束\n"
            "TODO: 填写禁止事项（或删除此段）"
        ),
        "subagent_type": "universal",
        "allowed_tools": tpl["allowed_tools"],
        "max_tool_rounds": tpl["max_tool_rounds"],
        "context_strategy": tpl["context_strategy"],
        "llm": "",
        "temperature": None,
        "workspace": "./workspace",
        "skills": tpl["skills"],
        "metadata": {
            "version": "1.0.0",
            "tags": ["custom"],
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="生成 ToolAgent config.json 骨架",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--name", required=True, help="Agent 名称（lowercase_underscore）")
    parser.add_argument("--dir", required=True, dest="target_dir", help="目标父目录")
    parser.add_argument(
        "--template",
        choices=["echo", "explorer", "pipeline"],
        default="explorer",
        help="预设模板复杂度（默认: explorer）",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    validate_name(args.name)

    target_dir = Path(args.target_dir).expanduser().resolve()
    agent_dir = target_dir / args.name
    config_path = agent_dir / "config.json"

    if config_path.exists():
        print(
            f"[ERROR] 目标文件已存在，拒绝覆盖: {config_path}\n"
            "  如需重新生成，请先手动删除该文件。",
            file=sys.stderr,
        )
        sys.exit(1)

    agent_dir.mkdir(parents=True, exist_ok=True)

    tpl = TEMPLATES[args.template]
    config = build_config(args.name, args.template)

    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"[OK] 已创建: {config_path}")
    print(f"     模板     : {args.template}")
    print(f"     工具配置 : {tpl['_hint_tools']}")
    print(f"     最大轮次 : {tpl['_hint_rounds']}")
    print(f"     上下文策略: {tpl['_hint_strategy']}")
    print()
    print("下一步：")
    print(f"  1. 编辑 {config_path}")
    print("     替换所有 TODO 占位符（description、system_prompt 的角色/步骤/约束）")
    print(f"  2. 运行校验: python validate_config.py {config_path}")
    print(f"  3. 安装: 将 {agent_dir} 拷贝到目标 tool_agents/ 目录")
    print("  4. 重启 core_server 并用 spawn_subagent 冒烟测试")


if __name__ == "__main__":
    main()
