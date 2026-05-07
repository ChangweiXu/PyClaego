# skill 模块 — 技能管理系统

## 概述

`skill` 模块提供技能（Skill）的加载、检索和内容组装功能。技能是存储在文件系统中的 Markdown 知识文档，遵循 SKILL 通用标准（含 YAML frontmatter）。模块以单例模式运行，支持全局共享技能和 Session 独有技能两级管理。

### 文件结构

```
skill/
├── __init__.py      # 导出符号 + get_skill_manager() 便捷函数
├── manager.py       # SkillManager - 技能管理器（单例）
├── skill.py         # Skill - 单个技能对象
├── parser.py        # SKILL.md 解析器（frontmatter + 章节）
└── exceptions.py    # 异常类定义
```

---

## SKILL 文件规范

每个技能是一个目录，其中必须包含 `SKILL.md` 文件：

```
skills/
└── python_best_practices/     ← 技能目录名（同时作为默认 name）
    ├── SKILL.md               ← 技能主文件（必须）
    └── ...                    ← 其他资源文件（可选）
```

`SKILL.md` 遵循 SKILL 通用标准，顶层支持以下 frontmatter 字段：

```yaml
---
name: python_best_practices          # 技能名称（覆盖目录名）
description: Python 编码规范与最佳实践  # 简短描述（必填）
user-invocable: true                  # 是否可由用户直接调用
disable-model-invocation: false       # 是否禁止模型调用
compatibility: ""                     # 兼容性信息
argument-hint: ""                     # 参数提示
license: MIT                          # 许可证
metadata:                             # 扩展元数据（自定义字段推荐放这里）
  version: "1.0.0"
  tags: ["python", "coding"]
  priority: 10                        # 同名技能的优先级（数字越大越优先）
  author: "team"
---

# Python 最佳实践

## 代码风格
...
## 错误处理
...
```

> **注意**：非标字段（如 `version`, `tags`, `priority`）如果直接写在顶层，会被自动合并到 `skill.metadata`，保持向后兼容。

---

## 核心类

### `Skill`（`skill.py`）

表示单个技能对象，由 `SkillManager` 负责创建。

#### 属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `name` | `str` | 技能名称（来自 frontmatter `name` 或目录名）|
| `path` | `Path` | 技能目录路径 |
| `description` | `str` | 简短描述 |
| `user_invocable` | `bool` | 是否可由用户直接调用，默认 True |
| `disable_model_invocation` | `bool` | 是否禁止模型调用，默认 False |
| `metadata` | `Dict` | 扩展元数据（含 version, tags, priority 等）|
| `tags` | `List[str]` | 来自 `metadata.tags` 的标签列表 |

#### 内容读取方法（懒加载）

```python
skill.get_summary()                    # 返回 description 文本
skill.get_full_content()               # 返回完整 SKILL.md 内容（不含 frontmatter）
skill.get_section("代码风格")           # 返回指定二级标题的章节内容
skill.get_sections(["代码风格", "错误处理"])  # 返回多个章节的拼接内容
skill.list_sections()                  # 返回所有章节名称列表
skill.validate()                       # 返回 (is_valid: bool, error_msg: str)
```

---

### `SkillManager`（`manager.py`）

技能管理器，单例模式，通过 `get_skill_manager()` 获取。

#### 获取实例

```python
from src.skill import get_skill_manager

manager = get_skill_manager()
# 等价于
manager = SkillManager.get_instance()
```

初始化时从 `config.yaml` 的 `skill.*` 读取配置：
- `skill.directories` — 全局技能搜索目录列表
- `skill.cache_enabled` — 是否启用内容缓存（默认 `true`）
- `skill.default_enabled` — 默认是否启用技能（默认 `true`）

#### 全局技能管理

```python
# 加载所有全局技能（返回 (成功数, 失败数)）
success, failed = manager.load_skills()

# 重新加载（先清空后重新扫描）
manager.reload_skills()

# 检查是否存在
manager.has_skill("python_best_practices")  # → bool

# 获取技能对象
skill = manager.get_skill("python_best_practices")  # → Skill | None

# 列出所有技能名称（支持按 tags 过滤）
names = manager.list_skills(tags=["python"])  # → List[str]

# 获取简介文本
summary = manager.get_skill_summary("python_best_practices")  # → str

# 获取完整/指定章节内容（支持内容缓存）
content = manager.get_skill_content("python_best_practices")
content = manager.get_skill_content("python_best_practices", sections=["代码风格"])
```

#### Session 独有技能管理

每个 Session 可以在其工作目录 `{workspace}/{session_id}/skills/` 下存放专属技能。

```python
# 加载 Session 独有技能
manager.load_session_skills("sess_abc123")

# 重新加载（Path 中新增技能后调用）
manager.reload_session_skills("sess_abc123")
```

**查询时 Session 技能优先于全局技能（同名时 Session 技能覆盖全局）**：

```python
# 带 session_id 查询时，自动合并两级技能
skill = manager.get_skill("my_tool", session_id="sess_abc123")
names = manager.list_skills(session_id="sess_abc123")
```

#### 内容组装（供 Context Handler 使用）

```python
# 格式化技能列表为字符串（供 LLM 参考）
text = manager.format_skills_list(include_description=True, session_id="sess_abc")
# 输出：
# Available Skills:
# 1. python_best_practices (v1.0.0) - Python 最佳实践指南
# 2. code_review (v1.2.0) - 代码审查清单

# 组装多个技能上下文
context = manager.assemble_context(
    skill_names=["python_best_practices", "code_review"],
    include_full_content=False,   # False: 仅简介；True: 完整内容
    session_id="sess_abc"
)

# 精细控制：指定每个技能的章节
context = manager.assemble_context(
    skill_names=["python_best_practices"],
    sections_map={"python_best_practices": ["代码风格", "错误处理"]},
    session_id="sess_abc"
)

# 搜索技能（在 name/description/tags/content 中搜索）
results = manager.search_skills("python", search_in="name,description,tags")
```

#### 诊断方法

```python
# 验证所有技能
results = manager.validate_all_skills()  # → {name: (is_valid, error_msg)}

# 统计信息
stats = manager.get_statistics()
# → {"total_skills": N, "session_count": M, "by_tags": {...}, "by_directory": {...}}

# 清空内容缓存
manager.clear_cache()
```

---

### `parser.py` — SKILL.md 解析器

工具函数，由 `Skill` 内部调用，通常不需要直接使用：

| 函数 | 说明 |
|------|------|
| `parse_skill_file(path)` | 解析完整 SKILL.md 文件，返回 `(frontmatter_dict, content_str)` |
| `parse_frontmatter(content)` | 仅解析 YAML frontmatter 部分 |
| `parse_sections(content)` | 解析 Markdown 二级标题，返回 `{section_name: content}` |
| `extract_summary(content, max_lines)` | 从正文提取摘要（当 description 缺失时使用）|
| `validate_metadata(metadata)` | 验证 frontmatter 合法性，返回 `(is_valid, error_msg)` |

---

### 异常类（`exceptions.py`）

| 异常类 | 触发场景 |
|--------|---------|
| `SkillError` | 所有技能相关异常的基类 |
| `SkillNotFoundError` | 技能名称不存在时 |
| `SkillInvalidError` | `SKILL.md` 格式不合法时 |
| `SkillLoadError` | 文件读取/解析失败时 |
| `SectionNotFoundError` | 请求的章节不存在时 |

---

## 配置项（`config.yaml`）

```yaml
skill:
  directories:
    - !abs_path "~/pyclaego/skills"     # 全局共享技能目录
    - !abs_path "./skills"              # 项目本地技能目录
  cache_enabled: true                   # 是否缓存内容（节省重复 IO）
  default_enabled: true                 # 默认是否启用技能
```

技能目录下每个子目录若包含 `SKILL.md` 则被识别为一个技能：
```
~/pyclaego/skills/
├── python_best_practices/SKILL.md
├── code_review/SKILL.md
└── docker_guide/SKILL.md
```

---

## 优先级规则

当多个来源有同名技能时：
1. **Session 独有技能** 优先于全局技能
2. **全局技能**之间按 `metadata.priority` 降序；相同 priority 时，后扫描的目录覆盖先扫描的

---

## 依赖关系

### 内部依赖

```python
from ..config import get_config       # 读取 skill.* 和 session.* 配置
from ..logging import get_running_log # 记录技能加载/查询日志
```

| 导入内容 | 来源 | 用途 |
|---------|------|------|
| `get_config()` | `src.config` | 读取技能目录、Session 工作目录映射 |
| `get_running_log()` | `src.logging` | 记录技能加载成功/失败、缓存命中等事件 |

### 被其他模块引用

```python
from ..skill import get_skill_manager
from ..skill import SkillManager
```

| 调用方模块 | 导入内容 | 用途 |
|-----------|---------|------|
| `src/security_executor/handler.py` | `get_skill_manager` | 初始化时加载技能；路径占位符解析时查询技能路径 |
| `src/security_executor/path_resolver.py` | （间接）`skill_path_getter` 回调 | 将技能名映射为文件系统路径 |
| `src/context/soul_context_v*.py` | `get_skill_manager` | 组装技能上下文，注入 LLM 系统提示词 |
