# `widget_classes/` — WidgetClass 注册表 + Hook 基类

`WidgetClass` 是 widget 的**模板**：JSON 默认配置 + 可选配置 schema + 可选 Python hook。

## 模块结构

```
widget_classes/
├── spec.py         # WidgetClassSpec dataclass + 资源解析
├── registry.py     # WidgetClassRegistry 单例 + 磁盘扫描
└── hook.py         # WidgetHook 基类
```

## `WidgetClassSpec` ([spec.py](spec.py))

```python
@dataclass
class WidgetClassSpec:
    class_id: str
    title: str
    description: str
    defaults: Dict[str, Any]            # deep-merge 的最低层
    config_schema: Dict[str, Any]       # JSON Schema, 用于前端 RJSF 表单
    asset_dir: Path                     # 模板所在目录
    source: str                         # 'builtin' | 'user'
    schema_file: Optional[str]
    default_viewers_file: Optional[str]
    default_cron_file: Optional[str]
    hook_class: Optional[Type[WidgetHook]]   # 由 registry 装填
    raw: Dict[str, Any]                      # 原始 JSON 备份
```

`from_dict(data, *, asset_dir, source)` 读 JSON 并把 `defaults["store"]["schema_file"]` 等路径**预解析为绝对路径**，
避免后续 `SqliteStore` 等组件按 widget workspace_dir 错误寻址。

`resolve_asset(rel)` 返回 `asset_dir / rel` 的绝对路径。

## `WidgetClassRegistry` ([registry.py](registry.py))

进程级单例。

- 扫描两个根目录：
  - **builtin**：`<repo>/pyclaego/widget_classes/`
  - **user**：`~/pyclaego/widget_classes/`（可选，存在时同名覆盖 builtin 并打 warning）
- `list()` / `has(class_id)` / `get(class_id) -> WidgetClassSpec` / `get_defaults(class_id) -> dict`
- `instance(builtin_root=..., user_root=...)` —— 测试用 `reset_instance()`

每个 class 目录布局：

```
<class_id>/
├── widget_class.json     # 必需
├── widget_class.py       # 可选 — 定义一个 WidgetHook 子类
├── schema.sql            # 可选 — 给 SqliteStore 用
├── prompts/              # 任意资源
├── viewers.json          # 可选 — 默认 viewers
└── cron.json             # 可选 — 默认 cron
```

`widget_class.py` 加载方式：使用 `importlib.util.spec_from_file_location` + 唯一 `sys.modules` 名字
（带 UUID 后缀，避免冲突）；要求文件里导出名为 `WidgetHook` 的类，且必须是 `widget_classes.hook.WidgetHook` 的子类，否则忽略并 warning。

## `WidgetHook` ([hook.py](hook.py))

子类按需重写下列方法，全部**可选 + 默认 no-op + 异常被 widget 吸收**：

```python
class WidgetHook:
    def __init__(self, widget): self.widget = widget
    async def on_create(self): ...
    async def on_chat(self, message, response=None): ...
    async def on_cron(self, trigger_id, message, response=None): ...
    async def on_destroy(self): ...
    def compute_highlight(self) -> dict: return {}
```

`compute_highlight()` 同步返回 dict，被 `/api/v2/.../highlight` 与 Dashboard 卡片直接读取。

## 已注册的 builtin

| class_id | 用途 | 路径 |
|---|---|---|
| `chat` | 默认裸 agent + 全局 context；新 PS bootstrap 时自动创建 | `pyclaego/widget_classes/chat/` |
| `notes` | sqlite 持久化笔记本，开启 `widget_db_*` + `widget_emit` 工具 | `pyclaego/widget_classes/notes/` |

## 测试

`test/test_widget_class_full.py`（12 用例）覆盖 spec 解析、notes class 加载、hook 发现与 Widget 生命周期联动。
