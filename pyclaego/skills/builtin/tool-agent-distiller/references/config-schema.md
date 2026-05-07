# ToolAgentConfig 字段参考

> 对应源码：`src/pyclaego/tool_agent/config.py — ToolAgentConfig`

## 字段速查表

| 字段 | 类型 | 默认值 | 必填 | 说明 |
|------|------|--------|------|------|
| `name` | str | 目录名 | ✓ | Agent 唯一标识，必须与目录名一致 |
| `description` | str | — | ✓ | 给 LLM 决策层看的能力描述（何时召唤） |
| `system_prompt` | str | — | ✓ | Agent 系统提示词，支持模板占位符 |
| `subagent_type` | str | `"universal"` | — | 编排逻辑类型，目前只有 `"universal"` |
| `allowed_tools` | list[str] | `[]` | — | 工具白名单（`["*"]`=全部，`[]`=无工具） |
| `max_tool_rounds` | int | `20` | — | Agent-Tool-Loop 最大轮次（≥1） |
| `context_strategy` | str | `"none"` | — | 上下文压缩策略，见下方决策树 |
| `llm` | str | `""` | — | LLM provider ID（空=继承父 Agent） |
| `temperature` | float\|null | `null` | — | 采样温度（null=使用 LLM 默认） |
| `workspace` | str | `"./workspace"` | — | 工作目录，相对于 config.json 所在目录 |
| `skills` | list[str] | `[]` | — | 技能列表（`["*"]`=全部，`[]`=无技能） |
| `metadata` | dict | `{}` | — | 扩展元数据（version、tags 等） |

---

## 各字段详解

### `name`

- 必须匹配目录名（目录名即 Agent 的注册 key）
- 命名约定：小写字母 + 数字 + 下划线，以字母开头
- 示例：`code_explorer`、`doc_extractor`、`sql_migrator`

### `description`

- 必填，不能为空白字符串
- **面向 LLM**：LLM 读此字段来决定是否 `spawn_subagent`，因此要说明**触发场景**
- 推荐结构：`[Agent 能做什么]。适合 [典型任务列举]。不支持 [明确排除项]。`
- 参考 `echo`：*"最简单的子 Agent，单次 LLM 调用，无工具。适合纯文本生成、改写、摘要。"*

### `system_prompt`

- 必填，不能为空白字符串
- 支持两个模板占位符（渲染时动态替换）：
  - `{workspace_path}` → 子 Agent 工作目录绝对路径
  - `{project_root}` → 代码探索目标目录（由 widget 配置决定，可能为空）
- 详见 `system-prompt-guide.md`

### `context_strategy`

有效值：`"none"` | `"summarizing"` | `"soulv6"` | `"fork"`

```
任务类型判断：
│
├── 需要继承父 Agent 完整上下文？
│   └─ 是 → "fork"
│
├── 任务步骤 > 5 轮，需跨轮记忆？
│   └─ 是 → "summarizing"（自动压缩旧消息）
│
├── 长期记忆 / SoulV6 特定场景？
│   └─ 是 → "soulv6"
│
└── 简单任务（≤3 轮工具调用，单次完成）？
    └─ 是 → "none"（默认，零额外开销）
```

### `allowed_tools`

- `["*"]`：全部工具（除 `spawn_subagent` 以防递归）
- `[]`：无工具（纯文本生成，等同于 echo 类型）
- 具体列表：精确白名单，越精确 Agent 越专注

精确白名单示例（只读代码探索）：
```json
["file_info", "find_line", "glob", "list_directory", "mkdir", "read_file", "search_text", "write_file"]
```

### `max_tool_rounds`

- 最小值：1（validate 强制 ≥1）
- 参考基准：
  - echo 类（无工具）：1
  - 简单探索（3-5 步）：10-15
  - 中等复杂（5-15 步）：20-30
  - 全能力复杂任务：40-55
- 建议：`估算最长执行路径步骤数 × 1.5`，上限 55

### `llm`

- 空字符串 `""` = 继承父 Agent 配置（**推荐默认**）
- 填写 provider ID 时优先级：`config.llm` > 继承父 Agent > fallback `"kimi_code"`

### `temperature`

- `null` = 使用 LLM provider 默认值（**推荐**）
- `0.0` = 确定性输出（代码生成、格式化任务）
- `0.7~1.0` = 创意性任务（内容创作等）

### `workspace`

- 相对路径基于 config.json 所在目录
- 默认 `"./workspace"` 通常无需修改
- 绝对路径也支持

### `skills`

- `["*"]`：注入全部已发现技能
- `[]`：不注入任何技能
- 具体列表：按需注入，减少无关上下文
- 示例：`["code-review-checklist", "python-best-practices"]`

### `metadata`

建议结构（非强制）：
```json
{
  "version": "1.0.0",
  "tags": ["custom", "domain-specific", "tag"]
}
```

---

## validate() 校验规则

以下情况会抛出 `ToolAgentConfigError`：

1. `name` 为空或纯空白字符串
2. `description` 为空或纯空白字符串
3. `system_prompt` 为空或纯空白字符串
4. `context_strategy` 不在 `{"none", "summarizing", "soulv6", "fork"}` 中
5. `subagent_type` 不在 `{"universal"}` 中
6. `max_tool_rounds < 1`

---

## 完整示例

```json
{
  "name": "pr_analyzer",
  "description": "PR 代码审查子 Agent，读取指定 PR 的 diff 内容，按照代码审查清单逐项检查，输出结构化审查报告。适合触发场景：用户要求 review PR、审查代码变更、生成 code review 报告。不支持自动提交评论。",
  "system_prompt": "# Sub-Agent Identity\n你是 PR 代码审查 Agent（PRAnalyzer）。\n你被主 Agent 创建，专门用于审查 Pull Request 的代码变更。\n\n# 工作目录\n当前工作目录：`{workspace_path}`\n\n# 工作流步骤\n## Step 1 — 读取 diff\n使用 read_file 或 bash 获取 PR 的变更内容。\n\n## Step 2 — 逐项审查\n按照代码审查清单逐条检查：功能正确性、代码质量、安全性、测试覆盖。\n\n## Step 3 — 生成报告\n将审查结论整理为结构化 Markdown 报告。\n\n# 输出规范\n- 输出文件：`review-report.md`\n- 包含：审查摘要、每项检查结论（通过/警告/阻断）、具体建议\n\n# 约束\n- 不支持直接提交 GitHub review 评论\n- 仅读取和分析，不修改目标代码",
  "subagent_type": "universal",
  "allowed_tools": ["bash", "file_info", "glob", "read_file", "search_text", "write_file"],
  "max_tool_rounds": 20,
  "context_strategy": "summarizing",
  "llm": "",
  "temperature": null,
  "workspace": "./workspace",
  "skills": ["code-review-checklist", "python-best-practices"],
  "metadata": {
    "version": "1.0.0",
    "tags": ["custom", "code-review", "git"]
  }
}
```
