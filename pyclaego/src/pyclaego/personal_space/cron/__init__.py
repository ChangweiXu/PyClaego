"""WidgetCronScheduler 模块（Phase 2.2 / 9）。

提供：
- :class:`WidgetCronScheduler` —— 进程级 APScheduler 单例的薄包装
- :class:`WidgetCronTrigger` —— 单条 cron 触发条目的类型
- :func:`render_prompt` —— 简单 ``str.format(...)`` 模板渲染（避免 jinja2 强依赖）

设计：cron 不直接调 ``Widget.process_message``，而是 **像一个普通 WS 客户端**
那样向 :class:`PSGateway` 注入 chat 消息（``conn_id="cron:<scheduler_uuid>"``，
``request_id="cron:<trigger_id>:<run_id>"``）。
这样 PS 的连接计数 / 自动卸载 / TaskHandler 创建路径完全复用，无新分支。
"""

from __future__ import annotations

from .scheduler import WidgetCronScheduler
from .template import render_prompt
from .trigger import WidgetCronTrigger

__all__ = ["WidgetCronScheduler", "WidgetCronTrigger", "render_prompt"]
