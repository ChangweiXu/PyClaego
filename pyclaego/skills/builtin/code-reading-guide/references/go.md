# Go 代码阅读指南

## 项目入口

| 文件 | 作用 |
|------|------|
| `go.mod` | 模块名（`module X`）和 Go 版本；依赖清单 |
| `go.sum` | 依赖版本锁定，通常不需要读 |
| `cmd/*/main.go` | 各可执行程序入口（多程序项目标准布局） |
| `main.go`（根目录） | 单程序项目入口 |
| `internal/` | 只允许本模块内部使用的包（不可被外部引用） |
| `pkg/` | 可被外部引用的公共包 |

**优先读 `go.mod` → `cmd/` 目录结构 → 对应的 `main.go`**。

---

## 核心符号与模式

| 符号/模式 | 含义 |
|-----------|------|
| `type X interface { }` | 接口定义，Go 采用隐式实现（无 `implements`） |
| `func (r *Receiver) Method()` | 方法绑定，`r` 是接收者 |
| `type X struct { }` | 数据结构定义 |
| `type X struct { Y }` | 结构体嵌入（组合复用），等同于继承 |
| `go func() { }()` | goroutine 启动，并发入口 |
| `chan T` / `<-chan T` / `chan<- T` | 通道，追踪数据流向 |
| `select { case ... }` | 多路通道选择，并发核心控制流 |
| `context.Context` | 请求级上下文，传递超时和取消信号 |
| `init()` | 包初始化函数，在 `main()` 前自动执行 |
| `//go:generate` | 代码生成指令（mock、proto、wire） |

**隐式接口实现**：Go 中若类型 `T` 实现了接口 `I` 的所有方法，则 `T` 自动满足 `I`。用 `search_text` 找实现，而非找 `implements`。

---

## 依赖追踪

```
import "github.com/org/pkg"    →  go.mod 中的外部依赖
import "mymodule/internal/x"  →  本模块内 internal/x/ 目录
import "mymodule/pkg/y"       →  本模块内 pkg/y/ 目录
import "fmt"                   →  标准库，不需要追踪
```

**接口实现追踪**（无 `implements` 关键字）：
1. 找接口定义：`type InterfaceName interface`
2. `search_text` 找实现方法：`pattern="func \(.*\) MethodName\("`
3. 确认接收者类型是否实现了所有接口方法

---

## 推荐阅读顺序

```
go.mod（模块名和依赖）
  → cmd/ 目录（了解有哪些可执行程序）
    → cmd/app/main.go（初始化顺序：配置 → 依赖注入 → 启动服务）
      → internal/ 核心接口定义
        → internal/ 具体实现包
          → pkg/（公共工具库）
```

---

## 工具使用技巧

```
# 映射包结构
glob: pattern="**/*.go", base_dir="{project_root}"

# 找所有入口函数
search_text: pattern="^func main\(\)", is_regex=true, recursive=true

# 找所有接口定义
search_text: pattern="type \w+ interface", is_regex=true, recursive=true

# 找某接口的隐式实现（通过方法名）
search_text: pattern="func \(\w+ \*?\w+\) MethodName\(", is_regex=true, recursive=true

# 找结构体嵌入（组合关系）
search_text: pattern="^\t[A-Z]\w+$", is_regex=true, recursive=true

# 找 goroutine 启动点
search_text: pattern="^(\s*)go ", is_regex=true, recursive=true

# 找 init 函数
search_text: pattern="^func init\(\)", is_regex=true, recursive=true

# 找 context 传递链
search_text: pattern="ctx context\.Context", recursive=true, context_lines=2
```
