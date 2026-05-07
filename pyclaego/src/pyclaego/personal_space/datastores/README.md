# `datastores/` — WidgetStore

每个 widget 的私有持久化层。两种实现：sqlite 和 jsonl。

## 模块结构

```
datastores/
├── base.py        # WidgetStore Protocol + StoreEvent
├── sqlite.py      # SqliteStore
├── jsonl.py       # JsonlStore
└── factory.py     # build_store(config, workspace_dir, schema_search_paths)
```

## 协议

```python
class WidgetStore(Protocol):
    async def query(self, sql_or_filter: str | dict, params: tuple | None = None) -> list[dict]: ...
    async def write(self, op: str, payload: dict) -> dict: ...
    async def schema(self) -> dict: ...
    def subscribe(self, callback: Callable[[StoreEvent], None]) -> Callable[[], None]: ...
    async def close(self) -> None: ...
```

## `SqliteStore` ([sqlite.py](sqlite.py))

- 后端：`aiosqlite`
- `query(sql, params)` —— 直接 `SELECT`，参数化绑定
- `write(op, payload)`：
  - `op="exec"` —— 执行 `payload["sql"]` + `payload.get("params", ())`
  - `op="insert" | "update" | "delete"` —— 由 payload 描述表/字段
- `schema()` —— `PRAGMA table_info` 汇总
- `schema_file`（来自 `widget_class.json` 的 `defaults.store.schema_file`）首次创建时执行：
  - WidgetClassSpec 已经把它解析为绝对路径，store 不再二次拼接 workspace_dir

## `JsonlStore` ([jsonl.py](jsonl.py))

按行 append 的事件流，适合无结构日志 / 时间序列。`query` 支持简单 dict filter（字段精确匹配 + 可选 limit）。

## `build_store` ([factory.py](factory.py))

```python
store = build_store(
    store_config=resolved_config.get("store", {}),
    workspace_dir=widget_workspace_dir,
)
```

- 根据 `type` 选实现（默认 `sqlite`）
- 把相对路径补成 `workspace_dir / <relative>`
- 不存在的 widget 目录会按需创建

## 与 `widget_db_*` 工具的关系

`widget_tools/` 里的工具直接持有 store 实例，工具签名暴露给 LLM。Hook 也可以通过 `widget.store` 访问。

## 测试

`test/test_widget_store.py`（含 sqlite/jsonl 各场景）+ `test/test_widget_tools.py`。
