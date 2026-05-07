# Python 代码阅读指南

## 项目入口

| 文件 | 作用 |
|------|------|
| `pyproject.toml` / `setup.py` | 包名、依赖、入口脚本（`[project.scripts]`） |
| `__init__.py`（顶层包） | 公开 API 的汇总点，决定 `from pkg import X` 能拿到什么 |
| `__main__.py` | `python -m pkg` 的入口 |
| `main.py` / `app.py` / `cli.py` | 独立脚本或 CLI 入口 |

**优先读 `pyproject.toml` → 顶层 `__init__.py`**，两分钟内即可了解包的整体结构。

---

## 核心符号与模式

| 符号/模式 | 含义 |
|-----------|------|
| `class X(ABC)` / `class X(Protocol)` | 抽象接口或结构性子类型，是架构分层的边界 |
| `@dataclass` / `@dataclass(frozen=True)` | 数据模型；`frozen=True` 表示不可变值对象 |
| `__slots__` | 高性能类，字段固定，不支持动态属性 |
| `@staticmethod` / `@classmethod` | 工厂方法或工具函数，通常是调用链的起点 |
| `__init_subclass__` / `__class_getitem__` | 元编程 hook，注册子类行为 |
| `@property` | 封装字段访问，背后可能有懒加载逻辑 |
| `yield` / `async def` + `await` | 生成器 / 协程，注意异步框架（asyncio/trio） |
| `__enter__` / `__exit__` | 上下文管理器，管理资源生命周期 |

---

## 依赖追踪

```
import X            →  找 X.py 或 X/__init__.py
from X import Y     →  找 X 包中 Y 的定义（search_text "def Y\|class Y"）
from . import Y     →  同包相对导入
from ..utils import Z  →  父包相对导入
```

**追踪调用链的步骤**：
1. `find_line` 找到函数/类定义所在行
2. `search_text` 搜索调用者：`pattern="函数名\("`, `recursive=True`
3. 对命名模糊的符号，先确认所在模块再搜索

---

## 推荐阅读顺序

```
pyproject.toml
  → src/<package>/__init__.py        # 包的公开接口
    → 核心数据模型（models.py / types.py / schema.py）
      → 业务逻辑层（service.py / handler.py / agent.py）
        → 入口点（main.py / cli.py / server.py）
```

如果是框架项目（FastAPI / Django / Flask）：
- FastAPI：`main.py` → `routers/` → `dependencies.py` → `models.py`
- Django：`settings.py` → `urls.py` → `views.py` → `models.py`

---

## 工具使用技巧

```
# 映射包结构
glob: pattern="**/__init__.py"

# 获取模块大纲（所有 class/def）
file_info: path="src/pkg/agent.py", outline=true

# 找所有抽象基类
search_text: pattern="class \w+.*ABC\|class \w+.*Protocol", is_regex=true, recursive=true

# 追踪某函数的调用者
search_text: pattern="process_request\(", recursive=true, context_lines=2

# 找装饰器使用
search_text: pattern="^@", is_regex=true, path="src/pkg/"
```
