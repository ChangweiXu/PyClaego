"""Cron slash-command handler — /cron, /cron run, /cron pause, /cron resume."""

from __future__ import annotations

from typing import Any


async def handle_cron(args: list[str]) -> str:
    """入口函数，由 CommandDispatcher 的 GLOBAL_COMMAND_REGISTRY 调用。

    子命令：
      /cron                 列出所有 cron 条目（含已禁用）
      /cron run <id>        立即触发一次（不影响调度）
      /cron pause <id>      暂停（写入 widget.json + 移除 APScheduler job）
      /cron resume <id>     恢复（写入 widget.json + 重新注册 APScheduler job）
    """
    from ..personal_space.cron.scheduler import WidgetCronScheduler

    scheduler = WidgetCronScheduler.get_instance()
    if scheduler is None:
        return "❌ Cron 调度器未初始化（服务器尚未完成启动）"

    if not args:
        return _cmd_list(scheduler)

    sub = args[0].lower()
    id_arg = args[1] if len(args) > 1 else ""

    if sub == "run":
        if not id_arg:
            return "❌ 用法: /cron run <id>"
        return _cmd_run(scheduler, id_arg)

    if sub == "pause":
        if not id_arg:
            return "❌ 用法: /cron pause <id>"
        return _cmd_pause(scheduler, id_arg)

    if sub == "resume":
        if not id_arg:
            return "❌ 用法: /cron resume <id>"
        return _cmd_resume(scheduler, id_arg)

    return (
        "❌ 未知子命令。可用子命令：\n"
        "  /cron                 — 列出所有 cron 任务\n"
        "  /cron run <id>        — 立即触发一次\n"
        "  /cron pause <id>      — 暂停（更新 widget.json）\n"
        "  /cron resume <id>     — 恢复（更新 widget.json）\n"
        "\n"
        "id 格式：短 tid（全局唯一时）、ps/widget/tid、或 ps__widget__tid"
    )


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------

def _cmd_list(scheduler: Any) -> str:
    entries = scheduler.list_all_disk_triggers()
    if not entries:
        return "（当前无 cron 触发条目）"

    lines: list[str] = [f"共 {len(entries)} 个 cron 触发条目：\n"]
    for e in entries:
        trig = e["trig"]
        enabled_mark = "✓" if trig.enabled else "✗"
        schedule_str = trig.schedule or f"每 {trig.interval_seconds}s"

        nrt = e.get("next_run_time")
        if trig.enabled and nrt is not None:
            next_str = nrt.strftime("%Y-%m-%d %H:%M:%S %Z") if hasattr(nrt, "strftime") else str(nrt)
        else:
            next_str = "—"

        prompt_preview = trig.prompt[:60] + ("…" if len(trig.prompt) > 60 else "")
        lines.append(
            f"[{enabled_mark}] {e['ps_id']}/{e['widget_id']}/{trig.id}\n"
            f"     schedule: {schedule_str}\n"
            f"     next_run: {next_str}\n"
            f"     prompt:   {prompt_preview}"
        )

    return "\n".join(lines)


def _cmd_run(scheduler: Any, id_or_path: str) -> str:
    try:
        ps_id, widget_id, trig = scheduler.find_trigger(id_or_path)
    except ValueError as exc:
        return f"❌ {exc}"
    try:
        run_id = scheduler.run_once(ps_id, widget_id, trig)
        return f"✓ 已触发 {ps_id}/{widget_id}/{trig.id}（run_id={run_id}）"
    except Exception as exc:
        return f"❌ 触发失败: {exc}"


def _cmd_pause(scheduler: Any, id_or_path: str) -> str:
    try:
        ps_id, widget_id, trig = scheduler.find_trigger(id_or_path)
    except ValueError as exc:
        return f"❌ {exc}"
    if not trig.enabled:
        return f"⚠️ {ps_id}/{widget_id}/{trig.id} 已处于暂停状态"
    try:
        scheduler.pause_trigger(ps_id, widget_id, trig.id)
        return f"✓ 已暂停 {ps_id}/{widget_id}/{trig.id}"
    except Exception as exc:
        return f"❌ 暂停失败: {exc}"


def _cmd_resume(scheduler: Any, id_or_path: str) -> str:
    try:
        ps_id, widget_id, trig = scheduler.find_trigger(id_or_path)
    except ValueError as exc:
        return f"❌ {exc}"
    if trig.enabled:
        return f"⚠️ {ps_id}/{widget_id}/{trig.id} 已处于运行状态"
    try:
        scheduler.resume_trigger(ps_id, widget_id, trig.id)
        return f"✓ 已恢复 {ps_id}/{widget_id}/{trig.id}"
    except Exception as exc:
        return f"❌ 恢复失败: {exc}"


__all__ = ["handle_cron"]
