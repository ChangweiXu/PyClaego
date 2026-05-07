"""WidgetCronTrigger —— widget.json 里 ``cron[]`` 条目的运行时投影。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class WidgetCronTrigger:
    """单条 cron 触发条目。

    schedule 形式（取其一即可）：
    - ``schedule: "0 8 * * *"``      —— 标准 5 段 cron（分时日月周）
    - ``interval_seconds: 60``       —— 每 N 秒触发一次（用于调试 / 轻量轮询）

    其他字段：
    - ``id``：trigger 内唯一 id；缺省时由 cron 模块自动生成 ``cr_<short_uuid>``
    - ``prompt``：触发时发给 widget 的 chat 内容（必填）
    - ``user_id``：日志 / TaskHandler 归属（默认 ``"cron"``）
    - ``enabled``：``False`` 时跳过注册
    - ``timezone``：APScheduler 时区字符串（可选）
    - ``params``：透传给 prompt 模板的变量字典（``str.format`` 替换）
    """

    id: str
    prompt: str
    schedule: str | None = None
    interval_seconds: int | None = None
    user_id: str = "cron"
    enabled: bool = True
    timezone: str | None = None
    params: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, fallback_id: str) -> WidgetCronTrigger:
        if not isinstance(raw, dict):
            raise TypeError(f"cron 触发条目必须是 dict，收到 {type(raw)}")
        prompt = raw.get("prompt") or raw.get("message") or ""
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(f"cron 触发条目缺少 prompt: {raw}")
        schedule = raw.get("schedule") or raw.get("cron")
        interval = raw.get("interval_seconds") or raw.get("every_seconds")
        if schedule is None and interval is None:
            raise ValueError(
                f"cron 触发条目需要 schedule 或 interval_seconds 之一: {raw}"
            )
        try:
            interval_int = int(interval) if interval is not None else None
        except (TypeError, ValueError):
            raise ValueError(
                f"cron interval_seconds 必须是整数: {interval!r}"
            ) from None
        return cls(
            id=str(raw.get("id") or fallback_id),
            prompt=prompt,
            schedule=schedule if isinstance(schedule, str) else None,
            interval_seconds=interval_int,
            user_id=str(raw.get("user_id") or "cron"),
            enabled=bool(raw.get("enabled", True)),
            timezone=raw.get("timezone") if isinstance(raw.get("timezone"), str) else None,
            params=dict(raw.get("params") or {}),
        )


__all__ = ["WidgetCronTrigger"]
