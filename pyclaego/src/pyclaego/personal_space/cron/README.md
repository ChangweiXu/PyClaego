# `cron/` — Widget 定时任务

为 widget 提供 cron / interval 触发的能力。基于 [APScheduler](https://apscheduler.readthedocs.io/) 的 `AsyncIOScheduler`。

## 模块结构

```
cron/
├── trigger.py     # WidgetCronTrigger + render_prompt
└── scheduler.py   # WidgetCronScheduler
```

## `WidgetCronTrigger` ([trigger.py](trigger.py))

`widget.json` 中的每条 cron entry：

```json
{
  "id": "daily_summary",
  "schedule": "0 9 * * *",
  "prompt": "请总结昨天 ({yesterday}) 的事项",
  "params": { "yesterday": "..." }
}
```

或 interval 模式：

```json
{ "id": "tick", "interval_seconds": 60, "prompt": "now={now}" }
```

`WidgetCronTrigger.from_dict(data)` 解析并校验：必须二选一 `schedule | interval_seconds`，且 `prompt` 非空。

`render_prompt(template, params, *, now=None)` 用 `string.Formatter` + 自定义 `_SafeDict`：
- 缺失的占位符不抛异常，原样保留 `{key}`，方便容错；
- 自动注入 `now / today / year / month / day` 等常用变量（基于本地时区 `datetime.now()`）；
- `params` 中的字段覆盖自动注入。

## `WidgetCronScheduler` ([scheduler.py](scheduler.py))

封装一个进程内的 `AsyncIOScheduler`，按 widget 维度注册 job。

```python
sched = WidgetCronScheduler(gateway=ps_gateway, ps_root=Path("~/pyclaego/personal_spaces").expanduser())
await sched.start()
await sched.scan_and_register()        # 走遍 ps_root 下所有 widget.json
await sched.shutdown()
```

主要方法：

- `scan_and_register()` —— 扫 `<ps_root>/<ps>/widgets/<wid>/widget.json`，把每条 `cron[]` 注册成 APScheduler job
- `register(ps_id, widget_id, trigger)` —— 单个注册，job_id = `f"{ps_id}::{widget_id}::{trigger.id}"`
- `unregister(ps_id, widget_id, trigger_id?)` —— 删 job 或某 widget 全部 job
- `_fire(ps_id, widget_id, trigger)` —— 触发时调用：
  1. 渲染 prompt → message
  2. 生成虚拟 `conn_id = f"cron:{sched_uuid}:{run_id}"`
  3. 依次 `gateway.handle_inbound(open / chat / close)`，复用 PSGateway 的全部路由 / 鉴权 / TaskHandler 装配逻辑

cron 触发产生的 task 通过 `TaskBelonging.source="cron"` 与正常聊天区分；Widget 在响应后会调用 `hook.on_cron(trigger_id, ...)`。

## 设计权衡

- **不直接调 `Widget.process_message`**：复用 PSGateway 才能拿到 task tracking、错误处理、配置热重载语义；
- **scan 不加锁**：cron 列表变更场景以"重启 / 重 scan"为主，避免增量同步复杂度；
- **interval 用 `IntervalTrigger`、cron 表达式用 `CronTrigger.from_crontab`**：原生 APScheduler 行为一致，调度异常落到 APScheduler 默认日志。

## 测试

`test/test_widget_cron.py`（7 用例）覆盖 trigger 解析、模板渲染、scan_and_register 与 _fire 的端到端。
