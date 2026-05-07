---
name: tool-agent-distiller
description: 当 Agent 完成了一个复杂、可重复的任务流程（代码分析、文档提取、数据处理管道等），且该流程有望被反复调用时，使用此 Skill 将经验蒸馏成一个可复用的 ToolAgent（config.json + 高质量 system_prompt）。产出物：一个合法的 config.json，可立即安装到 tool_agents/ 目录并通过 spawn_subagent 调用。触发场景："把刚才的流程保存成 agent"、"下次直接用 agent 做这个"、"帮我创建一个专门做 X 的子 Agent"，或 Agent 自主判断某个流程值得封装复用。
---

# Tool Agent Distiller

将刚完成的任务流程蒸馏为可复用的 ToolAgent。

## 工作流（六步）

### Step 1 — 提炼流程本质

回顾刚完成的对话或任务记录，提取：

- **一句话目标**：这个 Agent 做什么？（即 `description` 字段，面向 LLM 决策层）
- **核心步骤序列**：任务分几个阶段？每阶段做什么？
- **重复执行点**：哪些步骤会被触发多次？

> 关键约束：`description` 要说明 **何时用**（触发场景），而非仅描述能做什么。

### Step 2 — 盘点工具使用

列出完成此任务实际调用过的所有工具名。参考 `references/tool-categories.md` 做分类，然后决策：

| 场景 | `allowed_tools` 推荐值 |
|------|----------------------|
| 任务只读文件 + 写报告 | 显式列出具体工具名 |
| 任务涉及代码执行/网络 | 精确列出所用工具 |
| 任务边界不确定或高度多样 | `["*"]`（全通配） |
| 仅文本生成，无工具 | `[]` |

> **工具白名单越精确，Agent 越专注**。优先使用精确列表，仅在任务边界真正模糊时才用 `["*"]`。

### Step 3 — 决定配置参数

**context_strategy**（上下文压缩策略）：
- `"summarizing"` — 任务步骤多、需跨轮记忆（>5 轮工具调用）
- `"none"` — 简单任务、单次或少量工具调用（≤3 轮）
- `"fork"` — 需要继承父 Agent 完整上下文
- `"soulv6"` — 特定长期记忆场景（少用）

**max_tool_rounds**：
- 对标最长合理执行路径（步骤数 × 1.5），上限 55
- 参考基准：echo=1，code_explorer=15，pipeline=55

**llm**：空字符串 `""` = 继承父 Agent（推荐默认值）

**skills**：是否需要注入领域知识？明确列举所需技能名；全部技能填 `["*"]`；无需技能填 `[]`

**temperature**：`null` = 使用 LLM 默认值（推荐），需精确输出时填 `0.0`

### Step 4 — 撰写 system_prompt

参考 `references/system-prompt-guide.md` 的四段结构：

```
# Sub-Agent Identity     ← 角色定义（1-3句）
## 工作目录              ← {workspace_path} 占位符
## 工作流步骤            ← 有序步骤，对应 Step 1 提炼的流程
## 输出规范              ← 明确产出格式和文件名约定
## 约束                  ← 禁止事项、边界条件
```

**必须包含**：任务完成后将最终结果写入最后一条回复（系统自动保存为 RESULT.md）。

### Step 5 — 生成并校验 config.json

```bash
# 生成骨架（三档模板：echo / explorer / pipeline）
python scripts/init_tool_agent.py --name <agent-name> --dir <target-dir> --template explorer

# 填写字段后校验
python scripts/validate_config.py <target-dir>/<agent-name>/config.json
```

校验通过后确认所有 `TODO` 占位符已替换完毕。

### Step 6 — 安装与冒烟测试

**选择目标层级**（优先级低→高）：

| 层级 | 目录 | 适用场景 |
|------|------|---------|
| builtin | `tool_agents/builtin/<name>/` | 项目内置，所有人可用 |
| global | `~/.pyclaego/tool_agents/<name>/` | 用户全局自定义 |
| PS 私有 | `<ps_root>/<ps_id>/tool_agents/<name>/` | 特定 Personal Space |

将 `<name>/` 目录整体拷贝到目标位置，重启 `core_server` 或触发 manager reload，
然后用冒烟测试确认注册成功：

```
spawn_subagent(subagent_type="<name>", task_prompt="介绍你自己，列出你的能力")
```

---

## 命名规范

- ToolAgent 名：小写字母 + 数字 + 下划线（`^[a-z][a-z0-9_]*$`），如 `code_reviewer`
- 目录名必须与 `name` 字段一致
- 描述性、动词-宾语结构更好：`doc_extractor`、`pr_analyzer`、`sql_migrator`

## 参考资源

- `references/config-schema.md` — 全字段说明 + 决策树（Step 3 用）
- `references/system-prompt-guide.md` — system_prompt 写法指南（Step 4 用）
- `references/tool-categories.md` — 工具分类速查（Step 2 用）
- `scripts/init_tool_agent.py` — 生成 config.json 骨架
- `scripts/validate_config.py` — 校验 config.json 合法性
