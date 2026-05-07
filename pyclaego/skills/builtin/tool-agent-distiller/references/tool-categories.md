# 工具分类速查

> 用于 tool-agent-distiller Step 2：盘点工具使用，确定 `allowed_tools` 白名单。

---

## 分类总览

| 类别 | 工具名 | 场景 |
|------|--------|------|
| 只读文件 | `read_file`, `glob`, `list_directory`, `search_text`, `find_line`, `file_info` | 代码探索、文档阅读 |
| 写文件 | `write_file`, `file_edit`, `file_delete`, `copy_move`, `mkdir` | 生成报告、修改文件 |
| 代码执行 | `bash`, `safe_bash`, `python_exec`, `safe_python` | 数据处理、测试运行 |
| 网络 | `web_search`, `web_fetch`, `download_file` | 抓取网页、下载资源 |
| 媒体读取 | `read_image_base64`, `read_pdf` | 图片/PDF 内容提取 |
| 用户交互 | `query_user` | 需要用户确认或补充信息 |
| 子 Agent | `spawn_subagent` | 递归分解任务（builtin Agent 不应包含，防止无限递归） |

---

## 各分类详解

### 只读文件类

适合：代码探索、文档分析、不需要修改任何文件的任务。

| 工具 | 描述 |
|------|------|
| `read_file` | 读取文件内容（支持行范围） |
| `glob` | 按 glob 模式匹配文件路径 |
| `list_directory` | 列举目录结构 |
| `search_text` | 文本内容搜索（关键词/正则） |
| `find_line` | 精确行查找 |
| `file_info` | 获取文件元信息（大小、修改时间等） |

典型 `allowed_tools` 组合（只读探索 + 工作目录内写报告）：
```json
["file_info", "find_line", "glob", "list_directory", "read_file", "search_text", "mkdir", "write_file"]
```

---

### 写文件类

适合：代码生成、文档更新、数据输出任务。

| 工具 | 描述 |
|------|------|
| `write_file` | 写入文件内容（覆盖或追加） |
| `file_edit` | 原地编辑（search & replace） |
| `file_delete` | 删除文件 |
| `copy_move` | 复制或移动文件/目录 |
| `mkdir` | 创建目录 |

> **安全提示**：若 Agent 需要修改源代码，应在 system_prompt 中明确限定允许修改的路径范围，防止意外覆盖。

---

### 代码执行类

适合：数据处理、自动化脚本、测试运行、需要计算的任务。

| 工具 | 描述 | 沙箱隔离 |
|------|------|---------|
| `bash` | 执行 shell 命令（完整权限） | 无 |
| `safe_bash` | 执行 shell 命令（受限环境） | 有 |
| `python_exec` | 执行 Python 代码（完整权限） | 无 |
| `safe_python` | 执行 Python 代码（受限环境） | 有 |

选择建议：
- 生产环境中优先使用 `safe_bash` / `safe_python`
- 需要访问文件系统或网络时用无沙箱版本
- 纯数值计算/格式转换可用 `safe_python`

---

### 网络工具类

适合：爬虫、API 调用、外部资源获取。

| 工具 | 描述 | 注意 |
|------|------|------|
| `web_search` | 搜索引擎查询 | 返回摘要列表 |
| `web_fetch` | 抓取单个 URL 内容 | 对 Svelte SPA 可能失效，优先 `download_file` |
| `download_file` | 下载文件到本地 | 适合 HTML / PDF / 数据文件 |

> **提示**：HuggingFace、arXiv 等 SPA 网站需要用 `download_file` 而非 `web_fetch`。

---

### 媒体读取类

| 工具 | 描述 |
|------|------|
| `read_image_base64` | 将图片编码为 base64（用于多模态 LLM） |
| `read_pdf` | 提取 PDF 文本内容 |

---

### 用户交互类

| 工具 | 描述 | 注意 |
|------|------|------|
| `query_user` | 向用户提问，等待回复 | 谨慎使用；多数任务应避免打断用户 |

---

## 快速决策：选哪种模板？

根据任务所需工具的复杂程度，对标三档内置模板：

### 对标 `echo`（无工具）

任务特征：
- 纯文本转换（翻译、改写、格式化、摘要）
- 不需要读取任何外部文件
- 单次 LLM 调用即可完成

配置：`"allowed_tools": [], "max_tool_rounds": 1, "context_strategy": "none"`

---

### 对标 `code_explorer`（只读文件）

任务特征：
- 需要读取代码库或文档
- 不需要执行代码或访问网络
- 输出是分析报告（写入工作目录）

配置：
```json
"allowed_tools": ["file_info", "find_line", "glob", "list_directory", "mkdir", "read_file", "search_text", "write_file"],
"max_tool_rounds": 15,
"context_strategy": "summarizing"
```

---

### 对标 `pipeline`（全工具）

任务特征：
- 需要代码执行（bash/python）
- 需要网络访问（web_fetch/download_file）
- 多阶段复杂流程，需要多种工具配合

配置：`"allowed_tools": ["*"], "max_tool_rounds": 55, "context_strategy": "summarizing"`

---

## 常见任务 → 推荐工具组合

| 任务类型 | 推荐工具 | 模板参考 |
|---------|---------|---------|
| 代码库分析 / 模块探索 | `glob` + `read_file` + `search_text` + `write_file` | code_explorer |
| PR / diff 审查 | `bash` + `read_file` + `write_file` | code_explorer + bash |
| HTML 页面提取 | `download_file` + `python_exec` + `write_file` | pipeline |
| API 数据采集 | `web_fetch` + `python_exec` + `write_file` | pipeline |
| 文档格式转换 | `read_file` / `read_pdf` + `python_exec` + `write_file` | pipeline |
| 代码生成 / 重构 | `read_file` + `file_edit` + `write_file` | code_explorer 变体 |
| 测试运行 + 报告 | `bash` / `safe_bash` + `write_file` | pipeline |
| 纯文本改写 / 翻译 | （无工具） | echo |
