# C/C++ 代码阅读指南

## 项目入口

| 文件 | 作用 |
|------|------|
| `CMakeLists.txt` | 构建配置，列出所有 target 和源文件集合 |
| `Makefile` | 传统构建，`all:` 目标是编译起点 |
| `conanfile.txt` / `vcpkg.json` | 包管理，列出外部依赖 |
| `main.cpp` / `main.c` | 程序入口，`int main(...)` |
| `include/` 或 `*.h` / `*.hpp` | **公开 API**，先读头文件了解接口 |

**优先读 `CMakeLists.txt` → `include/` 下的头文件**，头文件即合约，实现在 `.cpp` 中。

---

## 核心符号与模式

| 符号/模式 | 含义 |
|-----------|------|
| `class X { virtual void f() = 0; }` | 纯虚函数 = 接口，找所有 `class Y : public X` |
| `class X : public Y` | 继承关系，Y 是基类 |
| `template<typename T>` | 泛型，关注特化版本 `template<> class X<int>` |
| `#define` / `#pragma once` | 宏定义和头文件保护 |
| `namespace X { }` | 命名空间隔离模块 |
| `std::shared_ptr` / `std::unique_ptr` | 所有权语义，`unique_ptr` = 唯一所有者 |
| `explicit` | 禁止隐式转换的构造函数 |
| `const` / `constexpr` | 编译期常量或不可变接口 |
| `PIMPL idiom`（`class Impl;`） | 实现隐藏，`.cpp` 中定义 `Impl` 类 |

---

## 依赖追踪

```
#include "X.h"      →  找项目内的 X.h（接口声明）
#include <X>        →  标准库或第三方库
#include "X.hpp"    →  C++ 头文件（含类声明）
```

**追踪实现步骤**：
1. 在 `.h` 中找到函数声明
2. `search_text` 在 `.cpp` 文件中找同名函数的定义：`pattern="ClassName::methodName"`
3. 虚函数追多态：`search_text` 找所有继承该基类的子类

---

## 推荐阅读顺序

```
CMakeLists.txt（或 Makefile）
  → include/ 目录（公开头文件 = API 契约）
    → 核心抽象类/接口头文件
      → main.cpp（入口，确认初始化顺序）
        → 核心 .cpp 实现文件
```

如果是大型项目（如 LLVM/Chromium 风格）：
- 先看 `README` 或 `ARCHITECTURE.md`
- 按模块目录逐个击破，不要从 `main()` 一路跟下去

---

## 工具使用技巧

```
# 找所有头文件（API 表面）
glob: pattern="**/*.h", base_dir="{project_root}"
glob: pattern="**/*.hpp", base_dir="{project_root}"

# 获取头文件大纲（类和函数声明）
file_info: path="include/core.h", outline=true

# 找所有纯虚类（接口）
search_text: pattern="= 0;", recursive=true, context_lines=3

# 找某接口的所有实现
search_text: pattern="class \w+ : public InterfaceName", is_regex=true, recursive=true

# 找函数实现（从声明跳到定义）
search_text: pattern="ClassName::methodName", recursive=true

# 找宏定义
search_text: pattern="^#define", is_regex=true, recursive=true
```
