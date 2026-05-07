# PyClaego

**中心化 Agent 管理系统 — WebSocket 架构的智能对话平台**

[![Python Version](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

---

## 你是否也在为这些问题头疼？

市面上的 Agent 框架看起来很美，用起来却处处掣肘：

- **又慢又烧钱**：主流 Agent 框架（OpenClaw, Hermes 等）在每次推理时都夹带大量冗余上下文，token 消耗居高不下，执行过程如同黑盒，出了问题根本无从追溯。
- **任务堆积，互相阻塞**：SKILL 机制只能占用主 Agent 的执行循环，复杂任务必须排队串行，多任务并发？想都别想。
- **经验沉淀难，Routing 开销大**：垂直领域的专业背景只能靠人工整理、塞进提示词，技能一旦叠加，还要额外承担路由决策的开销，越用越臃肿。

---

## PyClaego 给你一个更好的答案

我们重新定义了 Agent 系统应有的样子：

- **执行过程 100% 可视化**：每一步推理、每一条工具调用，实时以消息流的形式推送到看板。Agent 在做什么，你比它自己更清楚。
- **ToolAgent：超越 SKILL 的能力单元**：将完整工序蒸馏为独立的 ToolAgent，多个 ToolAgent 可以**同时并行运行**，彻底消除串行瓶颈，复杂任务不再等待。
- **日志系统，让每一个细节都有迹可查**：详尽的执行日志完整记录推理链路，任何时刻都可回溯，复现问题、优化流程从此有据可依。

> 不只是一个 Agent 框架——PyClaego 是让 AI 真正为你所用的控制中心。

---

## 快速启动

### 第一步：安装

运行安装脚本，自动完成环境搭建、依赖安装与配置文件初始化：

```bash
bash scripts/install.sh
```

脚本执行内容：
1. 检测并安装 [uv](https://github.com/astral-sh/uv)（若未安装）
2. 创建 `.venv` 虚拟环境并安装依赖
3. 建立 `~/.pyclaego` 运行时目录树
4. 复制配置模板与内置资源到 `~/.pyclaego`

### 第二步：配置

编辑 `~/.pyclaego/config.yaml`，填写必要的 API Key：

```yaml
# LLM API Key 示例（在 .config.d/llm.yaml 中配置）
providers:
  kimi_code:
    api_key: "sk-your-api-key-here"
```

> 也可以通过环境变量传入，例如 `export KIMI_CODE_API_KEY=sk-xxx`

### 第三步：启动服务

**启动 Core 后端服务**（WebSocket + Web API）：

```bash
bash scripts/start_core.sh
# 或直接运行
uv run pyclaego-core
```

服务默认监听：
- WebSocket：`ws://127.0.0.1:18765`
- Web API / Dashboard：`http://0.0.0.0:18888`

**启动 Dashboard 前端**（开发模式）：

```bash
bash scripts/start_dashboard.sh
```

首次运行会自动安装 npm 依赖，启动后访问终端输出的本地地址（通常为 `http://localhost:5173`）。

---

## 目录结构

```
.
├── scripts/
│   ├── install.sh          # 一键安装脚本
│   ├── start_core.sh       # 启动 Core 后端
│   └── start_dashboard.sh  # 启动 Dashboard 前端
├── pyclaego/
│   ├── src/pyclaego/       # Python 包源码
│   ├── dashboard/          # React 前端（Vite）
│   ├── skills/             # 内置技能
│   ├── tool_agents/        # 内置工具 Agent
│   ├── .config.d/          # 默认配置模板
│   └── config.example.yaml # 主配置模板
└── tests/                  # 测试用例
```

运行时目录 `~/.pyclaego/`（由安装脚本创建）：

```
~/.pyclaego/
├── config.yaml             # 主配置（从模板复制，可自定义）
├── .config.d/              # 细分配置（llm、tools、security 等）
├── .logs/                  # 日志文件
├── .memory/                # 长期记忆存储
├── .cache/                 # 缓存（WebFetch、任务工件等）
├── personal_spaces/        # PersonalSpace 数据
├── skills/                 # 技能目录
└── tool_agents/            # 工具 Agent 目录
```

---

## 详细文档

- [pyclaego/README.md](pyclaego/README.md) — 架构设计、核心特性、模块说明
- [pyclaego/dashboard/README.md](pyclaego/dashboard/README.md) — 前端开发指南
