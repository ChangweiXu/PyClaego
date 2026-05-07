# safe_bash 模块 — 结构化安全 Bash 工具

## 概述

`safe_bash` 是一个供 LLM 使用的**结构化命令树**执行工具。与传统的 `BashTool`（直接接收原始 bash 字符串并交给 `/bin/sh -c`）不同，本模块要求 LLM 以 **XML 或 JSON 命令树**描述意图，工具内部经过：

```
解析 (parser) → 结构验证 (tree.validator) → 注册表/安全验证 (registry) → 执行 (executor)
```

四道关卡之后才会真正调用子进程。所有命令通过 `asyncio.create_subprocess_exec` 启动，**永不经过 shell 中间层**，从根本上消除命令注入风险。

### 与 `BashTool` 的核心差异

| 维度 | BashTool | SafeBashTool |
|------|----------|--------------|
| 输入 | 原始 bash 字符串 | XML / JSON 命令树 |
| 执行 | `shell=True`，由 sh 解释 | `subprocess_exec`，参数列表直接传给 execve |
| 命令白名单 | 无 / 字符串黑名单 | 注册表（每条命令显式声明 flag 白/黑名单、路径范围、最大参数数）|
| 变量展开 | shell 处理 `$VAR` | 不展开；仅展开 `~` 前缀 |
| 命令组合 | 任意 shell 语法 | 仅支持 `cmd / pipeline / seq / and / or` 五种 AST 节点 |
| 失败模式 | 静默失败或异常 | 任何阶段失败 → `ToolResult(FAILED)` 带具体原因 |

---

## 文件结构

```
safe_bash/
├── __init__.py              # 导出 SafeBashTool / REGISTRY / 异常类
├── safe_bash_tool.py        # SafeBashTool —— BaseTool 子类，串联四个阶段
├── exceptions.py            # 异常层次（SafeBashError 及 5 个子类）
├── tree/                    # AST 节点 + 结构验证器
│   ├── nodes.py             #   CmdNode / PipelineNode / SeqNode / AndNode / OrNode / RedirectSpec
│   └── validator.py         #   TreeValidator —— 嵌套深度、命令总数、子节点合法性
├── parser/                  # 输入字符串 → Node 树
│   ├── __init__.py          #   parse(text, fmt="auto") 统一入口
│   ├── xml_parser.py        #   XmlParser —— ElementTree
│   └── json_parser.py       #   JsonParser —— 标准库 json
├── registry/                # 命令安全注册表
│   ├── cmd_registry.py      #   CommandRegistry / REGISTRY 单例
│   ├── base_cmd.py          #   SafeCommand 基类（flag 白/黑名单、路径范围、shell 元字符拒绝）
│   └── builtin_cmds/        #   预注册命令模块
│       ├── readonly_cmds.py #     ls / cat / head / tail / grep / find / wc / stat / du / file /
│       │                    #     echo / sort / uniq / cut / tr / pwd / date / which / env / test
│       ├── git_cmds.py      #     git（仅只读子命令）
│       ├── network_cmds.py  #     curl / wget（仅下载/检查，禁止上传）
│       └── process_cmds.py  #     ps / pgrep（kill 不注册）
└── executor/                # 实际执行
    └── tree_executor.py     #   TreeExecutor —— asyncio.subprocess + os.pipe()
```

---

## 安全模型

### 多层防御

1. **结构层 (`TreeValidator`)** — 限制最大嵌套深度（默认 5）和最大命令总数（默认 20）；
   `pipeline` 的步骤只能是 `CmdNode`；`and`/`or` 的子节点不允许是 `PipelineNode`（避免语义歧义）。
2. **命令层 (`CommandRegistry`)** — 每个 `CmdNode.name` 必须在注册表中存在，否则抛出 `UnknownCommandError`。
3. **参数层 (`SafeCommand.validate`)** — 顺序检查：
   - `max_args` 上限
   - **Shell 元字符拒绝**：`|`、`;`、`&&`、`||`、`>`、`<`、`>>`、`&`、`$(`、`` ` ``、`${`、`!`、`\n`、`\r`、`\0`
     （即便 `subprocess_exec` 已无害，也主动拒绝以防止下游命令二次求值）
   - `blocked_flags` 黑名单
   - `allowed_flags` 白名单（非空时生效）
   - 路径范围：路径类参数（含 `/` 或以 `~` 开头）必须落在 per-command `path_scope` 或全局 `path_scope` 之内
4. **执行层 (`TreeExecutor`)** — `asyncio.create_subprocess_exec`，参数以 `argv` 列表传入；
   `~` 在执行前由 `os.path.expanduser` 展开；`$VAR` **不展开**。

### 异常层次

```
SafeBashError                       # 基类
├── ParseError                      # XML/JSON 解析失败或不符 schema
├── StructuralViolationError        # 嵌套过深、pipeline 含非 CmdNode 等
├── UnknownCommandError             # 命令未在注册表中
├── SecurityViolationError          # flag 黑名单 / 路径越界 / shell 元字符
└── ExecutionError                  # 子进程启动失败或超时
```

`SafeBashTool.execute()` 统一捕获 `SafeBashError`，返回 `ToolResult(status=FAILED, error=str(e))`，**不会**把异常冒泡给调用方。

---

## 命令树 AST

### 节点类型（`tree/nodes.py`）

| 节点 | 语义 | 子节点约束 |
|------|------|-----------|
| `CmdNode` | 单条命令 | 叶节点；可选 `stdin` / `stdout` 文件重定向；可选 `cwd` |
| `PipelineNode` | `step[0] \| step[1] \| ... \| step[n]` | `steps` 必须 ≥ 2 个 `CmdNode`，禁止嵌套复合节点 |
| `SeqNode` | `step[0] ; step[1] ; ...`（始终继续，返回最后一个结果）| `steps` 可为任意 `Node` |
| `AndNode` | `left && right`（左成功才执行右）| `left`/`right` 不允许是 `PipelineNode` |
| `OrNode` | `left \|\| right`（左失败才执行右）| 同 `AndNode` |

每个节点都可携带 `cwd` 字段，**节点级 `cwd` 优先于父级和工具级 `working_dir`**。

### XML 与 JSON 等价示例

**XML（管道）**：

```xml
<bash cwd="/tmp">
  <pipeline>
    <cmd name="ls"><arg>-lha</arg><arg>~/Documents</arg></cmd>
    <cmd name="grep"><arg>-E</arg><arg>\.py$</arg></cmd>
  </pipeline>
</bash>
```

**JSON（管道）**：

```json
{
  "op": "pipeline",
  "cwd": "/tmp",
  "steps": [
    {"op": "cmd", "name": "ls",   "args": ["-lha", "~/Documents"]},
    {"op": "cmd", "name": "grep", "args": ["-E", "\\.py$"]}
  ]
}
```

**XML（条件执行 `&&`）**：

```xml
<bash>
  <and>
    <cmd name="test"><arg>-d</arg><arg>/tmp/work</arg></cmd>
    <cmd name="echo"><arg>dir exists</arg></cmd>
  </and>
</bash>
```

**XML（带文件重定向）**：

```xml
<bash>
  <cmd name="cat" cwd="/tmp">
    <arg>input.txt</arg>
    <stdout file="/tmp/copy.txt" append="false"/>
  </cmd>
</bash>
```

> XML 根标签固定为 `<bash>`；若直接子元素仅 1 个，该子元素即为根；多个时自动包装为隐式 `SeqNode`。
> JSON 根对象通过 `"op"` 字段判别节点类型（`"cmd"`/`"pipeline"`/`"seq"`/`"and"`/`"or"`）。

---

## 已注册的内置命令

通过 `REGISTRY.list_commands()` 可在运行时获取完整列表。

| 类别 | 命令 | 关键约束 |
|------|------|---------|
| **只读文件** | `ls`、`cat`、`head`、`tail`、`grep`、`find`、`wc`、`stat`、`du`、`file` | `find` 禁用 `-exec` / `-delete` 等写能力 flag |
| **文本处理** | `echo`、`sort`、`uniq`、`cut`、`tr` | 标准 flag 白名单 |
| **环境查询** | `pwd`、`date`、`which`、`env`、`test` | `env` 禁用 `-i`（清空环境）|
| **Git（只读）** | `git` | 仅允许 `log` / `diff` / `status` / `show` / `branch` / `tag` / `ls-files` / `ls-tree` / `shortlog` / `describe` / `rev-parse` / `rev-list` / `cat-file` / `blame` / `stash` / `remote` / `submodule` / `config`；**禁止**所有写操作子命令 |
| **网络（只下载）** | `curl`、`wget` | 禁用 `-d`/`--data` / `-F`/`--form` / `-T`/`--upload-file` 等上传 flag；wget 禁用 `--post-data` 等 |
| **进程查询** | `ps`、`pgrep` | `kill` 未注册（破坏性，不允许） |

### 添加新命令

```python
from src.tool.safe_bash.registry import REGISTRY, SafeCommand

@REGISTRY.register
class JqCommand(SafeCommand):
    name = "jq"
    allowed_flags = frozenset({"-r", "-c", "-n", "-s", "-R", "--arg"})
    blocked_flags = frozenset()
    max_args = 15
    path_scope = None  # 不限制；如 "/var/data" 则只允许该目录下路径
```

如需自定义验证（例如 git 的子命令白名单），覆盖 `validate(cls, args, global_path_scope)` 即可。

---

## 配置示例

```yaml
# tools.yaml
safe_bash:
  tool_type: "safe_bash"
  tool_name: "safe_bash_executor"
  enabled: true
  timeout: 30                  # 整棵命令树执行超时（秒）
  input_format: "auto"         # "xml" | "json" | "auto"（按首字符 < 或 { 自动判定）
  path_scope: null             # 全局路径范围限制；null 不限制
  working_dir: null            # 默认工作目录；可被节点级 cwd 覆盖
```

---

## 执行流程

```
LLM 调用 SafeBashTool(command_tree="<bash>...</bash>")
    │
    ├─ 1. validate_params(["command_tree"])
    │
    ├─ 2. parse(raw_input, fmt=input_format)      ── ParseError ────┐
    │
    ├─ 3. TreeValidator.validate(tree)             ── StructuralViolationError ─┤
    │                                                                            │
    ├─ 4. REGISTRY.validate_tree(tree, path_scope) ── UnknownCommandError ───────┤
    │                                              ── SecurityViolationError ───┤
    │                                                                            │
    └─ 5. TreeExecutor.execute(tree, cwd)          ── ExecutionError ───────────┤
                │                                                                │
                ├─ CmdNode      → asyncio.create_subprocess_exec                │
                ├─ PipelineNode → os.pipe() 串联，最后进程捕获 stdout            │
                ├─ SeqNode      → 顺序执行，返回最后结果                          │
                ├─ AndNode      → 左成功才执行右                                  │
                └─ OrNode       → 左失败才执行右                                  │
                                                                                 ▼
                                          ToolResult(status=SUCCESS|FAILED, output={
                                            "stdout": "...", "stderr": "...", "return_code": N
                                          })
```

任何阶段（2~5）抛出的 `SafeBashError` 都被工具捕获，并以 `ToolResult(FAILED)` 返回。

---

## Pipeline 实现细节

`TreeExecutor._exec_pipeline` 使用 **OS 级 `os.pipe()`** 在相邻进程间建立管道，
而不是经过 `asyncio` StreamReader 中转：

- `proc[i].stdout` 直连 `proc[i+1].stdin`（内核管道，零拷贝）
- 父进程在子进程启动后**立即关闭已传递的 fd**，确保 EOF 正确传播
- 仅最后一个进程的 stdout 被捕获返回，所有进程的 stderr 并发收集后串接
- 整棵树由顶层 `asyncio.wait_for(timeout)` 包裹，超时后所有子进程被 kill

---

## 依赖关系

### 导入

```python
# safe_bash_tool.py
from ..base_tool import BaseTool, ToolResult, ToolStatus
from ...logging import get_running_log
from .exceptions import SafeBashError
from .executor import ExecResult, TreeExecutor
from .parser import parse
from .registry import REGISTRY
from .tree import TreeValidator
```

### 被外部引用

| 调用方 | 用途 |
|--------|------|
| `src.tool.tool_manager` | 通过 `tool_type: "safe_bash"` 注册并实例化 `SafeBashTool` |
| LLM (Agent) | 在工具描述中看到注册命令清单与 XML/JSON schema，按格式生成 `command_tree` 参数 |

---

## 设计决策摘要

- **不支持 `$VAR` 展开**：消除变量注入攻击面；如需环境变量，由调用方在配置中显式提供。
- **主动拒绝 shell 元字符**：即便 `subprocess_exec` 已无害，也防止参数被下游命令（如 `sh -c`）二次求值。
- **白名单优先**：注册表中未声明的命令一律拒绝；`git` / `curl` / `wget` 进一步用子命令/flag 白名单收紧。
- **AST 而非字符串**：LLM 必须显式表达控制流（`pipeline`/`seq`/`and`/`or`），杜绝歧义和混淆攻击。
- **失败可观测**：所有拒绝原因通过 `ToolResult.error` 字段返回给 LLM，便于自纠错。
