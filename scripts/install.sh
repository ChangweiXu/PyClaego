#!/usr/bin/env bash
# =============================================================================
# PyClaego 初始化安装脚本
# 用法：bash scripts/install.sh
#
# 执行内容：
#   1. 检测并安装 uv
#   2. 创建 .venv 虚拟环境并安装依赖（含 tui / pdf extras）
#   3. 建立 ~/.pyclaego 运行时目录树
#   4. 复制配置模板、内置 skills / tool_agents
# =============================================================================
set -euo pipefail

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
PYCLAEGO_HOME="${PYCLAEGO_HOME:-$HOME/.pyclaego}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()    { echo -e "${GREEN}[install]${NC} $*"; }
warn()    { echo -e "${YELLOW}[warn]${NC}   $*"; }
err()     { echo -e "${RED}[error]${NC}  $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Phase 1: 检测 / 安装 uv
# ---------------------------------------------------------------------------
info "Phase 1: 检查 uv …"

if ! command -v uv &>/dev/null; then
    warn "未检测到 uv，正在通过官方脚本安装 …"
    curl -LsSf https://astral.sh/uv/install.sh | sh

    # 将 uv 加入当前 Shell 的 PATH
    for candidate in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
        if [[ -x "$candidate/uv" ]]; then
            export PATH="$candidate:$PATH"
            break
        fi
    done

    command -v uv &>/dev/null || err "uv 安装后仍无法在 PATH 中找到，请手动将其加入 PATH 后重试。"
    info "uv 安装完成：$(uv --version)"
else
    info "uv 已存在：$(uv --version)"
fi

# ---------------------------------------------------------------------------
# Phase 2: 虚拟环境 & 依赖安装
# ---------------------------------------------------------------------------
info "Phase 2: 安装依赖 …"

cd "$PROJECT_ROOT"

if [[ ! -d ".venv" ]]; then
    info "创建虚拟环境 .venv (Python 3.10+) …"
    uv venv .venv --python 3.10
else
    info ".venv 已存在，跳过创建。"
fi

info "同步依赖（pyclaego + extras: tui, pdf）…"
uv sync --package pyclaego --extra tui --extra pdf

# ---------------------------------------------------------------------------
# Phase 3: 建立运行时目录
# ---------------------------------------------------------------------------
info "Phase 3: 建立运行时目录 $PYCLAEGO_HOME …"

mkdir -p \
    "$PYCLAEGO_HOME/.config.d" \
    "$PYCLAEGO_HOME/.logs" \
    "$PYCLAEGO_HOME/.memory/soul_v5" \
    "$PYCLAEGO_HOME/.memory/soul_v6" \
    "$PYCLAEGO_HOME/.cache/web_fetch" \
    "$PYCLAEGO_HOME/.cache/task_artifact" \
    "$PYCLAEGO_HOME/personal_spaces" \
    "$PYCLAEGO_HOME/skills/builtin" \
    "$PYCLAEGO_HOME/tool_agents/builtin"

info "目录结构已就绪。"

# ---------------------------------------------------------------------------
# Phase 4: 复制必要文件
# ---------------------------------------------------------------------------
info "Phase 4: 复制配置与内置资源 …"

# 4a. 配置文件（已存在则跳过，保护用户修改）
CONFIG_SRC="$PROJECT_ROOT/pyclaego/config.example.yaml"
CONFIG_DST="$PYCLAEGO_HOME/config.yaml"

if [[ ! -f "$CONFIG_DST" ]]; then
    cp "$CONFIG_SRC" "$CONFIG_DST"
    info "已复制配置模板 → $CONFIG_DST"
else
    warn "config.yaml 已存在，跳过复制（保留现有配置）。"
fi

# 4b. .config.d 配置文件（已存在则跳过，保护用户修改）
CONFIG_D_SRC="$PROJECT_ROOT/pyclaego/.config.d"
CONFIG_D_DST="$PYCLAEGO_HOME/.config.d"

for cfg_file in agent_context.yaml llm.yaml security.yaml tools.yaml; do
    src="$CONFIG_D_SRC/$cfg_file"
    dst="$CONFIG_D_DST/$cfg_file"
    if [[ ! -f "$dst" ]]; then
        cp "$src" "$dst"
        info "已复制配置模板 $cfg_file → $dst"
    else
        warn "$cfg_file 已存在，跳过复制（保留现有配置）。"
    fi
done

# 4c. 内置 skills（-n：已有同名文件不覆盖）
SKILLS_SRC="$PROJECT_ROOT/pyclaego/skills/builtin/"
SKILLS_DST="$PYCLAEGO_HOME/skills/builtin/"

if [[ -d "$SKILLS_SRC" ]]; then
    cp -rn "$SKILLS_SRC" "$SKILLS_DST"
    info "内置 skills 已同步 → $SKILLS_DST"
else
    warn "源目录 $SKILLS_SRC 不存在，跳过 skills 复制。"
fi

# 4d. 内置 tool_agents（-n：已有同名文件不覆盖）
AGENTS_SRC="$PROJECT_ROOT/pyclaego/tool_agents/builtin/"
AGENTS_DST="$PYCLAEGO_HOME/tool_agents/builtin/"

if [[ -d "$AGENTS_SRC" ]]; then
    cp -rn "$AGENTS_SRC" "$AGENTS_DST"
    info "内置 tool_agents 已同步 → $AGENTS_DST"
else
    warn "源目录 $AGENTS_SRC 不存在，跳过 tool_agents 复制。"
fi

# ---------------------------------------------------------------------------
# Phase 5: 完成提示
# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}======================================================${NC}"
echo -e "${GREEN}  PyClaego 安装完成！${NC}"
echo -e "${GREEN}======================================================${NC}"
echo ""
echo -e "  配置文件：${YELLOW}$PYCLAEGO_HOME/config.yaml${NC}"
echo -e "  请编辑该文件，填入 API Key 及其他必要配置。"
echo ""
echo -e "  启动服务："
echo -e "    ${YELLOW}uv run pyclaego-core${NC}      # WebSocket 核心服务"
echo -e "    ${YELLOW}uv run pyclaego-tui-ps${NC}    # TUI 客户端（PersonalSpace 协议）"
echo ""
