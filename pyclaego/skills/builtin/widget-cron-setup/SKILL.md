---
name: widget-cron-setup
description: Configure, add, edit, enable, or disable scheduled (cron) tasks for a widget by modifying its widget.json manifest. Use when the user asks to set up a scheduled task, automatic reminder, timed trigger, recurring job, or daily/hourly notification for a widget. Also use when the user wants to pause, resume, or immediately run an existing cron task, or when asked to review or list current cron configurations.
---

# Widget 定时任务设置指南

在 `widget.json` 的 `cron[]` 数组中声明定时任务，服务启动时由 `WidgetCronScheduler` 扫描并注册到 APScheduler。触发时向指定 widget 的 agent 投递一条 `chat` 消息。

## 文件路径

```
<personal_space.root_path>/<ps_id>/widgets/<widget_id>/widget.json
```

默认 `personal_space.root_path` = `<pyclaego.root_path>/personal_spaces/`。

**查找目标文件的步骤：**
1. 确认 ps_id（PersonalSpace 名称）和 widget_id（如 `w_chat_default`）。
2. 拼接路径，用 `read_file` 读取现有 `widget.json`。
3. 修改 `cron[]` 数组后写回。

## cron 条目字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | 推荐 | 全局唯一标识符，用于 `/cron` 命令；缺省自动生成 `cr_00`…（不推荐） |
| `schedule` | string | 二选一 | 标准 5 段 cron 表达式：`分 时 日 月 周` |
| `interval_seconds` | int | 二选一 | 固定间隔秒数（调试/轮询用），与 `schedule` 互斥 |
| `prompt` | string | 是 | 触发时发给 widget agent 的消息，支持 `{key}` 占位符 |
| `enabled` | bool | 否 | `false` 时跳过注册（默认 `true`）；可用 `/cron resume <id>` 动态开启 |
| `timezone` | string | 否 | IANA 时区，如 `Asia/Shanghai`、`UTC`；缺省使用系统时区 |
| `user_id` | string | 否 | 触发消息的归属用户标识（默认 `"cron"`） |
| `params` | object | 否 | 变量字典，替换 `prompt` 中的 `{key}` 占位符 |

## 操作工作流

### 新增 cron 任务

1. **读取** 目标 `widget.json`
2. **确认** `cron[]` 数组存在（不存在则添加空数组）
3. **追加** 新条目，id 需在当前所有 widget 中全局唯一
4. **写回** 修改后的 `widget.json`
5. **通知用户**：修改在**下次后端重启后**生效，或引导用户在 widget 聊天框输入 `/cron resume <id>` 立即注册

### 修改已有 cron 任务

- 直接编辑 `widget.json` 中对应条目的字段
- `schedule` / `prompt` / `params` 等修改同样需要重启，或先 `/cron pause <id>` 再 `/cron resume <id>` 使其重新注册

### 禁用 / 启用任务

- **立即禁用（不重启）**：让用户在 widget 输入 `/cron pause <id>`
- **立即启用（不重启）**：让用户在 widget 输入 `/cron resume <id>`
- 也可直接编辑 `widget.json` 中的 `enabled` 字段，下次重启生效

## /cron 命令（在 widget 聊天框中输入）

| 命令 | 效果 |
|------|------|
| `/cron` | 列出所有 cron 条目（含已禁用），显示下次执行时间 |
| `/cron run <id>` | 立即触发一次（不影响后续调度） |
| `/cron pause <id>` | 暂停：写入 `widget.json`（`enabled: false`）并移除 APScheduler job |
| `/cron resume <id>` | 恢复：写入 `widget.json`（`enabled: true`）并重新注册 job |

**id 格式**（任一均可）：
- 短 id：`morning_brief`（全局唯一时有效）
- 路径格式：`alice/w_chat_default/morning_brief`
- 双下划线格式：`alice__w_chat_default__morning_brief`

## 常用 schedule 模板

```
0 8 * * *          每天 08:00
0 8 * * 1-5        工作日 08:00
0 9-18 * * 1-5     工作日 09:00–18:00 每整点
0 8-22/2 * * *     每天 08:00–22:00 每 2 小时
0 20 * * *         每天 20:00
*/30 * * * *       每 30 分钟
0 */4 * * *        每 4 小时
0 9 * * 1          每周一 09:00
0 0 1 * *          每月 1 日 00:00
```

> 参考：[cron-syntax.md](references/cron-syntax.md)

## 典型示例

### 每日早报（固定时间）
```json
{
  "id": "morning_brief",
  "schedule": "0 8 * * *",
  "timezone": "Asia/Shanghai",
  "prompt": "抓取昨天的 HuggingFace Daily Papers，生成简短摘要。",
  "enabled": true
}
```

### 带参数模板（城市可配置）
```json
{
  "id": "weather_morning",
  "schedule": "0 8 * * *",
  "timezone": "Asia/Shanghai",
  "prompt": "请汇报今天 {city} 的天气情况，并给出穿衣建议。",
  "params": { "city": "北京" },
  "enabled": true
}
```

### 调试心跳（interval 模式）
```json
{
  "id": "debug_heartbeat",
  "interval_seconds": 300,
  "prompt": "回复：heartbeat OK",
  "enabled": false
}
```

## 注意事项

- `cron[]` 中注释字段用 `"//"` 键（JSON 不支持原生注释），写法与示例保持一致
- `schedule` 和 `interval_seconds` **二选一**，同时存在时 `schedule` 优先
- 修改 `widget.json` 后，**新增条目和 schedule 变更**需重启后端或 `/cron pause`+`/cron resume` 才能生效；`prompt` / `params` 的变更在下次触发时自动读取最新值
- id 全局唯一性由使用者保证，重复 id 会导致 `/cron` 命令无法精确定位
