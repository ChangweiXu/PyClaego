# Rust 代码阅读指南

## 项目入口

| 文件 | 作用 |
|------|------|
| `Cargo.toml` | crate 名称、版本、依赖；`[[bin]]`/`[lib]` 定义构建目标 |
| `Cargo.lock` | 依赖版本锁定，通常不需要读 |
| `src/main.rs` | 二进制 crate 入口 |
| `src/lib.rs` | 库 crate 根模块，公开 API 从这里开始 |
| `src/bin/*.rs` | 多二进制 crate 入口 |

**优先读 `Cargo.toml` → `src/lib.rs`（或 `src/main.rs`）→ `mod` 声明**。

---

## 核心符号与模式

| 符号/模式 | 含义 |
|-----------|------|
| `trait X { }` | 行为抽象，类似接口，找所有 `impl X for T` |
| `impl X for T` | 为类型 `T` 实现 trait `X` |
| `impl T { }` | 为类型 `T` 定义固有方法（不依赖 trait） |
| `enum X { A, B(T), C { field: T } }` | 代数数据类型，常包含数据 |
| `struct X { field: T }` | 数据结构定义 |
| `pub` / `pub(crate)` / `pub(super)` | 可见性控制，`pub` = 公开，`pub(crate)` = crate 内可见 |
| `mod X;` | 引入子模块（找 `X.rs` 或 `X/mod.rs`） |
| `use crate::X;` | 引入 crate 内部符号 |
| `Result<T, E>` / `Option<T>` | 显式错误处理，避免异常 |
| `'a` (生命周期) | 借用关系约束，通常在函数签名中出现 |
| `Box<dyn Trait>` | 动态分发，运行时多态 |
| `Arc<T>` / `Mutex<T>` | 跨线程共享所有权 / 互斥锁 |
| `async fn` / `.await` | 异步函数（通常配合 `tokio` / `async-std`） |
| `#[derive(...)]` | 自动派生 trait（如 `Debug`, `Clone`, `Serialize`） |
| `#[cfg(...)]` | 条件编译（`#[cfg(test)]` = 仅在测试中编译） |

---

## 模块系统

Rust 模块系统通过 `mod` 声明构建树形结构：

```
src/lib.rs 中声明  mod agent;
  →  找 src/agent.rs  （单文件模块）
  →  或  src/agent/mod.rs  （目录模块）
     → 其中可以继续 mod submodule;
```

**可见性规则**：
- `pub` 字段/方法才能被父模块之外访问
- `pub(crate)` 只在同一 crate 内可见
- 未标注 `pub` = 私有，只在当前模块内可见

---

## 依赖追踪

```
use crate::agent::Agent        →  crate 内部，找 src/agent.rs 中的 Agent
use super::utils::helper       →  父模块相对路径
use external_crate::Config     →  Cargo.toml 中的 [dependencies]
```

**Trait 实现追踪**：
1. 找 trait 定义：`trait TraitName`
2. `search_text` 找所有实现：`pattern="impl TraitName for "`
3. 对于 `Box<dyn TraitName>`，这是动态分发，任何实现该 trait 的类型都可能被使用

---

## 推荐阅读顺序

```
Cargo.toml（依赖和 feature flags）
  → src/lib.rs 或 src/main.rs（模块树根）
    → pub use / pub mod（找公开 API）
      → 核心 trait 定义
        → 关键 struct/enum 定义
          → impl 块（方法实现）
            → 异步运行时入口（#[tokio::main]）
```

---

## 工具使用技巧

```
# 找所有 Rust 源文件
glob: pattern="src/**/*.rs"

# 获取模块大纲
file_info: path="src/lib.rs", outline=true

# 找所有 trait 定义
search_text: pattern="^pub trait |^trait ", is_regex=true, recursive=true

# 找 trait 的所有实现
search_text: pattern="impl TraitName for ", recursive=true, context_lines=3

# 找模块声明（跟踪模块树）
search_text: pattern="^mod |^pub mod ", is_regex=true, recursive=true

# 找公开 API（pub 函数和类型）
search_text: pattern="^pub fn |^pub struct |^pub enum |^pub type ", is_regex=true, path="src/lib.rs"

# 找异步入口点
search_text: pattern="#\[tokio::main\]|#\[async_std::main\]", is_regex=true, recursive=true

# 找错误类型定义
search_text: pattern="enum.*Error|struct.*Error", is_regex=true, recursive=true

# 找 derive 宏使用
search_text: pattern="#\[derive\(", is_regex=true, recursive=true, context_lines=1
```
