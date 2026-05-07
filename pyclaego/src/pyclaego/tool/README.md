# tool 模块 — 工具系统

## 概述

`tool` 模块提供工具（Tool）的定义、注册、管理和执行能力。所有工具都继承自 `BaseTool` 抽象基类，由 `ToolManager`（单例）统一管理。模块还提供 `ToolCallParser` 用于解析 LLM 输出中的工具调用文本标签。

### 文件结构

```
tool/
├── __init__.py          # 导出符号 + 注册 22 种内置工具 + get_tool_manager()
├── base_tool.py         # BaseTool 抽象基类、ToolResult、ToolStatus
├── tool_manager.py      # ToolManager 单例（注册/创建/执行工具）
├── tool_call_parser.py  # ToolCallParser（解析 LLM 文本中的工具调用）
├── safe_bash/           # SafeBashTool 子包（结构化命令树 + 安全注册表）
│   └── README.md             #   详见子包 README
├── safe_python/         # SafePythonTool 子包（AST 验证 + 沙盒执行）
│   ├── safe_python_tool.py   #   SafePythonTool 主类
│   ├── validator/            #   ASTValidator（结构检查）
│   ├── policy/               #   REGISTRY + 内置安全策略（模块白名单等）
│   ├── executor/             #   SandboxExecutor（spawn 子进程）
│   └── exceptions.py         #   SafePythonError 异常层级
├── file_system/         # 文件系统工具子包
│   ├── fs_base_tool.py           # FileSystemBaseTool 公共基类
│   ├── read_file_tool.py         # read_file         - 读取文件内容
│   ├── write_file_tool.py        # write_file        - 写入文件内容
│   ├── file_edit_tool.py         # file_edit         - 字符串替换精确编辑文件
│   ├── delete_file_tool.py       # file_delete       - 安全删除文件或目录
│   ├── copy_move_tool.py         # copy_move         - 复制或移动文件/目录
│   ├── file_info_tool.py         # file_info         - 获取文件元信息及代码大纲
│   ├── find_line_tool.py         # find_line         - 在单文件中精确定位关键词行列
│   ├── list_directory_tool.py    # list_directory    - 列出目录
│   ├── mkdir_tool.py             # mkdir             - 创建目录
│   ├── glob_tool.py              # glob              - glob 模式文件匹配
│   ├── search_text_tool.py       # search_text       - 文本/代码搜索
│   ├── read_image_base64_tool.py # read_image_base64 - 读取图片返回多模态 ImagePart
│   └── read_pdf_tool.py          # read_pdf          - 读取 PDF 返回多模态 DocumentPart
└── tools/
    ├── __init__.py
    ├── bash_tool.py              # bash              - 执行 Shell 命令
    ├── python_exec_tool.py       # python_exec       - 执行 Python 文件或代码字符串
    ├── query_user_tool.py        # query_user        - Agent 发起用户确认（挂起等待回复）
    ├── download_file_tool.py     # download_file     - HTTP/HTTPS 文件下载
    ├── web_search_tool.py        # web_search        - 网络搜索
    ├── web_fetch_tool.py         # WebFetchTool      - 旧版，未注册
    ├── web_fetch_tool_v2.py      # WebFetchToolV2    - V2，已弃用
    ├── web_fetch_tool_v3.py      # web_fetch         - 当前注册版本（HTML→MD + 大纲提取）
    └── web_fetch_cache/          # 本地缓存存储目录
```

---

## 核心类

### `ToolStatus`（枚举）

| 值 | 含义 |
|----|------|
| `SUCCESS` | 执行成功 |
| `FAILED` | 执行失败 |
| `TIMEOUT` | 执行超时 |
| `DISABLED` | 工具已禁用 |

### `ToolResult`

工具执行结果对象。

```python
result = ToolResult(
    status=ToolStatus.SUCCESS,
    output="command output here",
    error=None,
    metadata={"exit_code": 0}
)

result.is_success()  # → True
result.to_dict()     # → {"status": "success", "output": ..., "error": None, "metadata": ...}
```

**多模态工具结果：** 工具可通过 `content_parts` 字段返回图片、文档等视觉内容，由 Agent 层传递给 LLM 客户端：

```python
from src.llm.types import ImagePart, DocumentPart

# 图片工具结果
result = ToolResult(
    status=ToolStatus.SUCCESS,
    output="图片已读取: logo.png (image/png, 12345 bytes)",
    content_parts=[ImagePart(source_type="base64", data="<base64>", media_type="image/png")],
)

# PDF 工具结果
result = ToolResult(
    status=ToolStatus.SUCCESS,
    output="PDF 文件: report.pdf (50000 bytes, 10 页)",
    content_parts=[DocumentPart(source_type="base64", data="<base64>", media_type="application/pdf")],
)
```

`content_parts` 为 `None` 时行为与之前完全一致（纯文本结果）。

### `BaseTool`（抽象基类）

所有工具实现必须继承此类，并实现三个抽象方法：

```python
class MyTool(BaseTool):
    IS_READONLY: bool = True         # 只读工具（不修改文件系统）
    IS_PARALLELIZABLE: bool = True   # 可并发调用

    async def execute(self, **kwargs) -> ToolResult:
        """执行工具核心逻辑"""
        ...

    def get_description(self) -> Dict[str, Any]:
        """返回工具描述（用于生成 LLM 工具提示词）"""
        return {
            "name": "my_tool",
            "description": "工具功能描述",
            "parameters": {
                "param1": {
                    "type": "string",
                    "required": True,
                    "description": "参数描述"
                }
            }
        }

    def mask_output(self, raw_output: Any, path_mask_map: Dict[str, str]) -> Any:
        """将输出中的真实路径替换为占位符（路径脱敏）"""
        return self._mask_string(raw_output, path_mask_map)
```

#### 类常量

| 常量 | 类型 | 说明 |
|------|------|------|
| `IS_READONLY` | `bool` | 工具是否为只读（不产生副作用），默认 `False` |
| `IS_PARALLELIZABLE` | `bool` | 是否可并发调用，默认 `False` |

这两个常量**不可通过配置覆盖**，需由子类显式声明。

#### 内置工具方法

| 方法 | 说明 |
|------|------|
| `is_enabled()` | 检查工具是否启用（来自配置 `enabled` 字段）|
| `is_readonly_tool()` | 是否只读（返回 `IS_READONLY`）|
| `is_parallelizable_tool()` | 是否可并发（返回 `IS_PARALLELIZABLE`）|
| `get_info()` | 返回工具基本信息字典 |
| `validate_params(required_params, **kwargs)` | 验证必需参数是否存在 |
| `_mask_string(text, path_mask_map)` | 字符串级路径脱敏（静态方法）|
| `_coerce_bool(value, default)` | 将 str/int/bool 统一转换为 bool（静态方法）|
| `_coerce_int(value, default)` | 将 str/float/bool 统一转换为 int（静态方法）|

---

### `ToolManager`（`tool_manager.py`）

工具管理器，单例模式。

#### 获取实例

```python
from src.tool import get_tool_manager

manager = get_tool_manager()
# 等价于
manager = ToolManager.get_instance()
```

初始化时从 `config.yaml` 的 `tools.*` 加载已启用的工具：
```yaml
tools:
  bash:
    enabled: true
    tool_name: "bash"
    timeout: 30
  read_file:
    enabled: true
    tool_name: "read_file"
```

#### 注册与创建

```python
# 注册工具类型（__init__.py 导入时自动完成）
ToolManager.register_tool("bash", BashTool)

# 按配置创建工具实例（通常由 _load_tools_from_config 内部调用）
tool = ToolManager.create_tool({"tool_type": "bash", "tool_name": "bash", "enabled": True})
```

#### 查询

```python
# 获取工具实例
tool = manager.get_tool("bash")  # → BashTool | None

# 列出已加载的工具名称
names = manager.list_loaded_tools()  # → ["bash", "read_file", ...]

# 列出所有已注册的工具类型（不一定已加载）
types = ToolManager.list_available_tools()

# 获取所有工具信息
info = manager.get_all_tools_info()  # → {name: {tool_type, enabled, ...}}

# 获取只读且可并发的工具（供 SubAgent 使用）
parallelizable = manager.get_readonly_parallelizable_tools()
```

#### 执行工具

```python
result = await manager.execute_tool("bash", command="ls -la", timeout=30)
# → ToolResult(status=SUCCESS, output="...", error=None)
```

执行流程：检查工具存在 → 检查 `is_enabled()` → 调用 `tool.execute(**kwargs)` → 捕获异常

#### 路径脱敏

```python
# 将工具输出中的真实路径替换为占位符（在 SecurityHandler 中调用）
masked = manager.mask_tool_output(
    tool_name="bash",
    raw_output="/home/user/workspaces/sess_abc/result.txt",
    path_mask_map={"/home/user/workspaces/sess_abc": "{{WORKSPACE}}"}
)
# → "{{WORKSPACE}}/result.txt"
```

---

## 22 种内置工具

| 工具名 | 类名 | IS_READONLY | IS_PARALLELIZABLE | 说明 |
|--------|------|:-----------:|:-----------------:|------|
| `bash` | `BashTool` | False | False | 执行 Shell 命令（支持超时控制、工作目录）|
| `safe_bash` | `SafeBashTool` | False | False | 以 XML/JSON 命令树执行 Shell（`subprocess_exec`、注册表白名单、无 shell 中间层）— 详见 [safe_bash/README.md](safe_bash/README.md) |
| `safe_python` | `SafePythonTool` | False | False | AST 验证 + spawn 子进程沙盒执行 Python 代码（四阶段验证，详见下文）|
| `python_exec` | `PythonExecTool` | False | False | 执行 Python 文件或代码字符串（可配置解释器路径、超时、工作目录）|
| `query_user` | `QueryUserTool` | True | False | Agent 发起用户确认：挂起执行，等待用户通过 chat 回复选项 |
| `read_file` | `ReadFileTool` | True | True | 读取文件内容（支持行范围、编码）|
| `write_file` | `WriteFileTool` | False | False | 写入/追加/覆盖文件内容 |
| `file_edit` | `FileEditTool` | False | False | 字符串替换精确修改文件（默认要求匹配唯一；`count=0` 替换全部）|
| `file_delete` | `DeleteFileTool` | False | False | 安全删除文件或目录（递归删除需显式 `recursive=true`）|
| `copy_move` | `CopyMoveTool` | False | False | 复制（文件/目录树）或移动文件/目录（支持跨设备）|
| `file_info` | `FileInfoTool` | True | True | 获取文件元信息（行数、token 估算、MIME 类型）及代码结构大纲 |
| `find_line` | `FindLineTool` | True | True | 在单个文件中精确定位关键词行号和列号（配合 `file_edit` 使用）|
| `list_directory` | `ListDirectoryTool` | True | True | 列出目录内容（支持递归、过滤）|
| `mkdir` | `MkdirTool` | False | False | 安全创建目录（支持递归创建父目录）|
| `glob` | `GlobTool` | True | True | glob 模式匹配文件/目录，返回相对路径列表 |
| `search_text` | `SearchTextTool` | True | True | 文本/代码全文搜索（支持正则）|
| `download_file` | `DownloadFileTool` | False | True | 从 HTTP/HTTPS URL 下载文件到本地 |
| `read_image_base64` | `ReadImageBase64Tool` | True | True | 读取本地图片，通过 `content_parts` 返回 `ImagePart` 多模态内容 |
| `read_pdf` | `ReadPdfTool` | True | True | 读取本地 PDF，通过 `content_parts` 返回 `DocumentPart` 多模态内容 |
| `web_search` | `WebSearchTool` | True | True | 调用搜索引擎（返回摘要列表）|
| `web_fetch` | `WebFetchToolV3` | True | True | 抓取网页并转换为 Markdown（HTML→MD 一站式 + 大纲提取 + 本地缓存）|

### 工具参数示例

```yaml
# bash 工具
bash:
  command: "ls -la {{WORKSPACE}}"  # 支持路径占位符
  timeout: 30                       # 可选，默认工具配置值

# safe_bash 工具（结构化命令树）
safe_bash:
  command_tree: |
    <bash cwd="/tmp">
      <pipeline>
        <cmd name="ls"><arg>-lha</arg><arg>~/Documents</arg></cmd>
        <cmd name="grep"><arg>-E</arg><arg>\.py$</arg></cmd>
      </pipeline>
    </bash>
  # 详细说明见 safe_bash/README.md；允许的命令 / 参数由注册表控制

# python_exec 工具
python_exec:
  file_path: "{{WORKSPACE}}/scripts/run.py"   # file_path 与 code 二选一；两者均传时优先用 file_path
  args: ["--verbose", "input.csv"]              # 可选，传递给脚本的命令行参数
  # 或：
  # code: "print('hello')"                       # 写入临时文件后执行

# read_file 工具
read_file:
  path: "{{WORKSPACE}}/output.txt"
  start_line: 1       # 可选
  end_line: 50        # 可选
  encoding: "utf-8"   # 可选

# write_file 工具
write_file:
  path: "{{WORKSPACE}}/result.md"
  content: "# 结果\n..."
  mode: "write"  # "write"（覆盖）/ "append"（追加）

# list_directory 工具
list_directory:
  path: "{{WORKSPACE}}"
  recursive: false   # 可选，默认 false
  pattern: "*.py"    # 可选，glob 过滤

# mkdir 工具
mkdir:
  path: "{{WORKSPACE}}/output/results"   # 目标目录路径
  parents: true   # 可选，默认 true（递归创建父目录）
  exist_ok: true  # 可选，默认 true（目录已存在时不报错）

# glob 工具
glob:
  pattern: "**/*.py"             # 必填，glob 模式
  base_dir: "{{WORKSPACE}}"      # 可选，搜索基准目录
  max_results: 1000              # 可选，最大返回数量

# search_text 工具
search_text:
  path: "{{WORKSPACE}}"
  pattern: "def main"   # 搜索关键词或正则
  file_pattern: "*.py"  # 可选，文件名过滤
  use_regex: false      # 可选

# download_file 工具
download_file:
  url: "https://example.com/data.csv"    # HTTP/HTTPS 文件 URL
  dest: "{{WORKSPACE}}/downloads/data.csv"  # 本地保存路径
  overwrite: false   # 可选，默认 false
  timeout: 30        # 可选，超时秒数

# read_image_base64 工具
read_image_base64:
  path: "{{WORKSPACE}}/assets/logo.png"  # 支持 PNG/JPEG/GIF/WebP/BMP/SVG
  # 返回: output 为文本描述，content_parts 包含 ImagePart

# read_pdf 工具
read_pdf:
  path: "{{WORKSPACE}}/docs/report.pdf"  # 支持 PDF 文件
  # 返回: output 为文本摘要（依赖 PyPDF2），content_parts 包含 DocumentPart

# file_edit 工具（字符串替换，配合 find_line 定位唯一上下文）
file_edit:
  path: "{{WORKSPACE}}/src/main.py"
  old_str: "def old_function():"   # 必须与文件内容完全匹配（含空格换行）
  new_str: "def new_function():"
  encoding: "utf-8"   # 可选，默认 utf-8
  count: 1             # 可选：1=唯一匹配（默认）；0=替换全部

# file_delete 工具
file_delete:
  path: "{{WORKSPACE}}/tmp/old_file.txt"
  recursive: false   # 可选，默认 false；删除非空目录必须设为 true

# copy_move 工具
copy_move:
  source: "{{WORKSPACE}}/src/config.yaml"
  destination: "{{WORKSPACE}}/backup/config.yaml"
  action: "copy"      # "copy" 或 "move"
  overwrite: false    # 可选，默认 false

# file_info 工具
file_info:
  path: "{{WORKSPACE}}/src/main.py"
  outline: true      # 可选，默认 false；提取 .py/.md/.js/.ts 结构大纲
  encoding: "utf-8"  # 可选

# find_line 工具（配合 file_edit 精确定位）
find_line:
  path: "{{WORKSPACE}}/src/main.py"
  keyword: "def process"   # 搜索关键词或正则
  is_regex: false           # 可选，默认 false
  encoding: "utf-8"         # 可选

# web_search 工具
web_search:
  query: "python asyncio tutorial"
  max_results: 5   # 可选

# web_fetch 工具（WebFetchToolV3）
web_fetch:
  url: "https://example.com/docs"  # 支持自动识别 arxiv.org 等特殊站点
  use_cache: true        # 可选，默认 true；false 时强制重新抓取
  output_format: "md"   # 可选，"md"（默认）或 "text"
  extract_outline: true  # 可选，提取 Markdown 章节大纲
  preview_length: 500    # 可选，返回正文前 N 字符的预览片段
  mode: "auto"           # 可选，"auto"（自动推断）/ "generic" / "arxiv"
  # 返回：output 为 {outline, preview, output_file, url, cached, ...}
  # 正文完整内容已写入 output_file，使用 read_file 工具读取

# safe_python 工具（SafePythonTool）
safe_python:
  code: "import math\nprint(math.sqrt(2))"  # code 与 file_path 二选一
  # 或：
  # file_path: "{{WORKSPACE}}/scripts/analyze.py"
  # 执行流程：语法解析 → ASTValidator → PolicyRegistry → SandboxExecutor(spawn)

# query_user 工具（QueryUserTool）
# 须通过 SecurityHandler.request_tool_call 执行，不可直接调 ToolManager
query_user:
  prompt: "是否继续删除 50 个文件？"
  choices:
    - {value: "yes", label: "确认删除"}
    - {value: "no", label: "取消"}
  default: "no"     # 可选，超时后自动选择的值
  timeout_s: 60     # 可选，0 表示永久等待
```

---

### `SafePythonTool` — 四阶段 AST 安全执行

`safe_python` 工具（位于 `safe_python/` 子包）在执行前对代码进行四阶段验证：

```
1. ast.parse()          — 语法解析，SyntaxError 立即返回 FAILED
2. ASTValidator         — 结构检查（max_depth / max_stmts / max_lines）
3. REGISTRY.validate_ast() — 安全策略（模块白名单 / 全局黑名单 / 禁止 dunder 访问）
4. SandboxExecutor      — spawn 子进程执行，受限 __builtins__，进程级超时保证
```

与 `python_exec` 的区别：
- `python_exec` 直接调用系统 Python 解释器，适合受信环境
- `safe_python` 有 AST 验证 + 进程隔离，适合执行不完全可信的代码

**配置参数：**

```yaml
safe_python:
  enabled: true
  timeout: 30           # 执行超时（秒），超时后子进程被强杀
  max_memory_mb: 256    # 子进程内存上限（MiB，Unix 有效）
  max_depth: 8          # 代码最大嵌套深度
  max_stmts: 200        # 代码最大语句数
  max_lines: 500        # 代码最大行数
```

**异常层级（`safe_python.exceptions`）：**

```
SafePythonError
├── SecurityViolationError   ← 策略检查失败（禁止模块、dunder 访问等）
├── StructuralViolationError ← 结构检查失败（嵌套过深、语句过多等）
├── SandboxTimeoutError      ← 超时
└── ExecutionError           ← 子进程运行时异常
```

---

### `QueryUserTool` — Agent 发起用户确认

`query_user` 工具允许 Agent 在执行关键操作前向用户提问并等待回复。

**运行机制：**

1. `execute()` 向 `QueryService` 注入 `PendingQuery` 并 `await future`
2. 用户通过正常 chat 消息回复选项 `value`
3. `PSGateway._dispatch_chat` 检测到 `QueryService.has_pending` → 调用 `try_resolve`
4. Future 被 resolve，`execute()` 返回用户选择的 `value`

**重要限制：** 必须通过 `SecurityHandler.request_tool_call` 执行（注入 `_session_id`），不可直接调 `ToolManager.execute_tool`。

**`choices` 结构：**

```python
choices = [
    {"value": "yes", "label": "确认", "description": "继续执行"},
    {"value": "no",  "label": "取消"},
]
```

用户回复特殊值 `/stop` 时，`try_resolve` 返回 `ResolveKind.STOPPED`，Agent 任务被取消。

---

### `ToolCallParser`（`tool_call_parser.py`，可选兼容能力）

`ToolCallParser` 用于解析 LLM 输出文本中的工具调用标签，主要作为兼容能力保留（例如历史文本协议或自定义场景）。当前主流程中，`SimpleAgent` / `ThinkAgent` / `SpawnAgent` 均通过原生 API 工具调用协议（`ToolDefinition` + `ToolCall`）完成工具调用，不依赖该解析器。

#### 推荐格式（XML 参数体）

```xml
<tool name="bash">
  <arg name="command"><![CDATA[cat "a\"b.txt" && echo done]]></arg>
  <arg name="timeout" type="int">30</arg>
</tool>
```

支持 `type` 属性：`string`（默认）、`int`、`float`、`bool`、`json`。  
支持 `<![CDATA[...]]>` 包裹复杂字符串（避免特殊字符转义问题）。

#### 兼容格式（旧版 JSON 参数体）

```xml
<tool name="bash">
{"command": "ls -la", "timeout": 30}
</tool>
```

#### 使用方式

```python
from src.tool import ToolCallParser, TOOL_CALL_PROMPT

# 在系统提示词中注入工具调用说明
system_prompt = TOOL_CALL_PROMPT + "\n".join(tool_descriptions)

# 解析 LLM 回复
text = '...<tool name="bash"><arg name="command">ls</arg></tool>...'

# 检查是否含工具调用
if ToolCallParser.has_tool_calls(text):
    tool_calls = ToolCallParser.parse_tool_calls(text)
    # → [{"tool_name": "bash", "tool_args": {"command": "ls"}}]

# 提取纯文本（去掉工具标签）
pure_text = ToolCallParser.remove_tool_tags(text)
```

#### `TOOL_CALL_PROMPT`

预设的工具调用说明模板常量，内容为：
```
# 可用工具
当遇到需要工具协助的任务时，使用以下格式调用工具：

<tool name="工具名称">
  <arg name="参数名">参数值</arg>
</tool>

支持的工具如下：
```

---

## 依赖关系

### 内部依赖

```python
from ..config import get_config        # 读取 tools.* 配置（延迟导入）
from ..logging import get_running_log  # 工具执行日志记录
```

| 导入内容 | 来源 | 用途 |
|---------|------|------|
| `get_config()` | `src.config` | `_load_tools_from_config()` 中读取工具启用状态和配置参数 |
| `get_running_log()` | `src.logging` | 记录工具注册、创建、执行成功/失败事件 |

### 被其他模块引用

```python
from ..tool import get_tool_manager          # 获取 ToolManager 单例
from ..tool import BaseTool, ToolResult, ToolStatus  # 扩展自定义工具时使用
from ..tool import ToolCallParser, TOOL_CALL_PROMPT  # 可选：文本协议工具调用解析
```

| 调用方模块 | 导入内容 | 用途 |
|-----------|---------|------|
| `src/security_executor/handler.py` | `get_tool_manager` | 执行工具调用；调用 `mask_tool_output()` 进行输出脱敏 |
| `src/context/simple_context.py` | `get_tool_manager` | 生成工具描述列表，组装工具 schema 供 LLM 使用 |
| `src/context/soul_context_v*.py` | `get_tool_manager` | 同上 |
