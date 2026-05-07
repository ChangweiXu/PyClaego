# `widget_tools/` — Widget 专属工具

Widget 内可用的、与 widget 私有数据耦合的工具。每个工具实例化时由 `Widget.load` 完成依赖注入：
`ps_id` / `widget_id` / `store` / `emit_fn`。

## 工具列表

| name | 作用 | 启用开关 |
|---|---|---|
| `widget_db_query` | 只读查询当前 widget store | `tools.widget_db_query.enabled` |
| `widget_db_write` | 写入当前 widget store（exec / insert / update / delete） | `tools.widget_db_write.enabled` |
| `widget_emit` | 向当前 widget 发布一条 StoreEvent，触发订阅者 | `tools.widget_emit.enabled` |

## 模块结构

```
widget_tools/
├── base.py            # WidgetTool 基类（DI + LLM schema）
├── builder.py         # build_widget_tools(resolved_config, *, store, emit_fn, ps_id, widget_id)
├── db_query.py        # WidgetDbQueryTool
├── db_write.py        # WidgetDbWriteTool
└── emit.py            # WidgetEmitTool
```

## 启用方式

合并后的 widget config：

```jsonc
{
  "tools": {
    "widget_db_query": { "enabled": true },
    "widget_db_write": { "enabled": true },
    "widget_emit":     { "enabled": false }
  }
}
```

`build_widget_tools` 只会构造 `enabled=true` 的工具；返回标准 `Tool` 列表，被 Agent 注册到 LLM tool schema。

## 设计要点

- **per-widget 注入**：工具不会读全局 PSManager，避免越界访问其他 widget 的 store
- **schema 显式标注**：每个工具的 `parameters` JSON Schema 完整声明给 LLM，方便模型自然推断使用方式
- **不抢全局工具命名空间**：通过 `Widget._build_tool_box` 与全局 ToolBox 合并，`name` 冲突时以 widget 工具优先

## 测试

`test/test_widget_tools.py` 覆盖三种工具的 success / error / 权限路径；与 `test/test_widget_runtime.py` 联动验证 widget 内集成。
