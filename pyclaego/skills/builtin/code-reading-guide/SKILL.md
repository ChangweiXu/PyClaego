---
name: code-reading-guide
description: 多语言代码阅读指南 - 提供 Python、C/C++、Java、JavaScript/TypeScript、Go、Rust 的代码阅读技巧与切入策略。适用于代码探索任务，帮助子 Agent 快速定位入口点、识别核心模式、追踪调用链。
user-invocable: false
metadata:
  version: 1.0.0
  author: PyClaego Team
  tags: [code-exploration, reading, multi-language]
  priority: 9
  enabled: true
---

# 代码阅读指南

## 如何使用本技能

你是一个代码探索子 Agent，当你收到代码探索任务时：

**第一步：识别项目语言**
用 `glob` 工具检测项目中的文件扩展名：
- `**/*.py` → Python
- `**/*.go` → Go
- `**/*.rs` → Rust
- `**/*.java` → Java
- `**/*.ts` 或 `**/*.tsx` → TypeScript
- `**/*.js` 或 `**/*.jsx` → JavaScript
- `**/*.cpp` 或 `**/*.cc` 或 `**/*.c` 或 `**/*.h` → C/C++

一个项目可能同时使用多种语言，按文件数量多少判断主语言。

**第二步：按需读取对应指南**
本技能的 `references/` 目录下包含各语言的阅读指南。用 `read_file` 读取你需要的文件：

| 语言 | 参考文件路径 |
|------|-------------|
| Python | `{skill_path}/references/python.md` |
| C/C++ | `{skill_path}/references/cpp.md` |
| Java | `{skill_path}/references/java.md` |
| JavaScript/TypeScript | `{skill_path}/references/javascript.md` |
| Go | `{skill_path}/references/go.md` |
| Rust | `{skill_path}/references/rust.md` |

> 注意：`{skill_path}` 是本技能所在目录的绝对路径，通常为 `skills/builtin/code-reading-guide`。实际使用时，先用 `glob` 找到本 SKILL.md 的位置，然后推导 references/ 路径。

**第三步：应用阅读策略**
每份语言指南包含：
- **项目入口**：从哪里开始读
- **核心符号**：哪些模式/关键字代表架构决策
- **依赖追踪**：如何跟踪模块间的调用关系
- **阅读顺序**：推荐的文件跳跃路径
- **工具提示**：对应的 glob/search_text 搜索技巧

## 通用原则

无论什么语言，优先：
1. 读配置/构建文件（了解依赖和项目结构）
2. 用 `file_info(outline=true)` 获取大纲而非全文
3. 只在需要理解具体实现时才 `read_file`
4. 所有引用带行号：`file.py:L10-L50`
