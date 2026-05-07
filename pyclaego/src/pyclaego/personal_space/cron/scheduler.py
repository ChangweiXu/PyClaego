"""WidgetCronScheduler —— 把 widget.json 的 ``cron[]`` 注册到 APScheduler。

启动顺序：
1. 扫描 ``personal_spaces/<ps_id>/widgets/<widget_id>/widget.json`` 找出所有
   ``cron[]`` 条目。**不预加载 PS 实例**。
2. 为每条触发条目向 ``AsyncIOScheduler`` 注册 job：
   - ``schedule`` (cron 字符串) → :class:`CronTrigger`
   - ``interval_seconds``       → :class:`IntervalTrigger`
3. 触发时执行 :meth:`_fire`：
   - 在自己的 conn_id (``cron:<scheduler_uuid>:<run_id>``) 上调用
     ``gateway.handle_inbound`` 投递一条 ``open``，紧接一条 ``chat``，
     最后一条 ``close``。
   - PS 因为新连接被引导/加载；处理完后引用计数归零，PSManager 在 idle
     超时后自动卸载。

设计要点：
- 不直接持有 ``PersonalSpace`` 引用，**只通过 PSGateway 投递消息** —— 与 WS
  客户端走完全相同的代码路径，避免新增并发分支。
- ``publish_fn`` 可选：cron 不需要把 reply 发回任何 socket；提供 noop 即可。
- ``shutdown()`` 幂等。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ...logging import get_running_log
from .template import render_prompt
from .trigger import WidgetCronTrigger

_rlog = get_running_log()


# 类型签名：PSGateway.handle_inbound(conn_id, msg) -> Awaitable[None]
HandleInboundFn = Callable[[str, dict[str, Any]], Awaitable[None]]

# 进程内单例 —— 由 __init__ 设置，由 get_instance() 读取
_INSTANCE: WidgetCronScheduler | None = None


class WidgetCronScheduler:
    def __init__(
        self,
        *,
        ps_root: Path,
        handle_inbound: HandleInboundFn,
        timezone: str | None = None,
    ) -> None:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        self.ps_root: Path = Path(ps_root).expanduser().resolve()
        self._handle_inbound: HandleInboundFn = handle_inbound
        self._scheduler_id: str = uuid.uuid4().hex[:8]
        self._scheduler = AsyncIOScheduler(timezone=timezone) if timezone else AsyncIOScheduler()
        self._registered: list[tuple[str, str, WidgetCronTrigger]] = []  # (ps, widget, trigger)
        self._started: bool = False

        global _INSTANCE
        _INSTANCE = self

    # ------------------------------------------------------------------
    # 单例访问
    # ------------------------------------------------------------------

    @classmethod
    def get_instance(cls) -> WidgetCronScheduler | None:
        """返回当前进程内唯一的 WidgetCronScheduler 实例，未初始化时为 None。"""
        return _INSTANCE

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        """扫描磁盘 → 注册所有 cron job → 启动 scheduler。幂等。"""
        if self._started:
            return
        self.scan_and_register()
        self._scheduler.start()
        self._started = True
        _rlog.info(
            "core_service",
            f"[WidgetCronScheduler:{self._scheduler_id}] 启动，已注册 {len(self._registered)} 个 trigger",
        )

    def shutdown(self, *, wait: bool = False) -> None:
        if not self._started:
            return
        try:
            self._scheduler.shutdown(wait=wait)
        except Exception:
            _rlog.exception(
                "core_service",
                f"[WidgetCronScheduler:{self._scheduler_id}] shutdown 异常",
            )
        self._started = False

    # ------------------------------------------------------------------
    # 扫描
    # ------------------------------------------------------------------

    def scan_and_register(self) -> int:
        """扫描 ``ps_root/<ps_id>/widgets/<widget_id>/widget.json``。

        返回新注册（已计入 ``self._registered``）的 trigger 数量。
        重复调用是安全的，但目前不会做去重 —— 调用方应在 start 前只调一次，
        或先调 :meth:`clear`。
        """
        added = 0
        if not self.ps_root.exists():
            _rlog.warning("core_service", f"[WidgetCronScheduler] ps_root 不存在: {self.ps_root}")
            return 0
        for ps_dir in sorted(self.ps_root.iterdir()):
            if not ps_dir.is_dir():
                continue
            ps_id = ps_dir.name
            widgets_root = ps_dir / "widgets"
            if not widgets_root.exists():
                continue
            for widget_dir in sorted(widgets_root.iterdir()):
                if not widget_dir.is_dir():
                    continue
                widget_id = widget_dir.name
                wjson = widget_dir / "widget.json"
                if not wjson.exists():
                    continue
                try:
                    raw = json.loads(wjson.read_text(encoding="utf-8"))
                except Exception:
                    _rlog.exception("core_service", f"[WidgetCronScheduler] 解析 {wjson} 失败，跳过")
                    continue
                cron_list = raw.get("cron") or []
                if not isinstance(cron_list, list):
                    _rlog.warning("core_service", f"[WidgetCronScheduler] {wjson} 的 cron 字段必须是数组，跳过")
                    continue
                for idx, item in enumerate(cron_list):
                    fb_id = f"cr_{idx:02d}"
                    try:
                        trig = WidgetCronTrigger.from_dict(item, fallback_id=fb_id)
                    except Exception:
                        _rlog.exception("core_service", f"[WidgetCronScheduler] {wjson} cron[{idx}] 解析失败")
                        continue
                    if not trig.enabled:
                        continue
                    try:
                        self._register(ps_id, widget_id, trig)
                        added += 1
                    except Exception:
                        _rlog.exception("core_service", f"[WidgetCronScheduler] 注册 {ps_id}/{widget_id}/{trig.id} 失败")
        return added

    # ------------------------------------------------------------------
    # 注册单条 trigger
    # ------------------------------------------------------------------

    def _register(self, ps_id: str, widget_id: str, trig: WidgetCronTrigger) -> None:
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger

        if trig.schedule:
            try:
                ap_trigger = CronTrigger.from_crontab(
                    trig.schedule, timezone=trig.timezone
                )
            except Exception as e:
                raise ValueError(
                    f"非法 cron schedule {trig.schedule!r}: {e}"
                ) from e
        elif trig.interval_seconds and trig.interval_seconds > 0:
            ap_trigger = IntervalTrigger(seconds=trig.interval_seconds)
        else:
            raise ValueError(f"trigger {trig.id} 既无 schedule 也无 interval_seconds")

        job_id = f"{ps_id}__{widget_id}__{trig.id}"
        self._scheduler.add_job(
            self._fire,
            trigger=ap_trigger,
            id=job_id,
            kwargs={"ps_id": ps_id, "widget_id": widget_id, "trig": trig},
            replace_existing=True,
            misfire_grace_time=60,
            coalesce=True,
            max_instances=1,
        )
        self._registered.append((ps_id, widget_id, trig))
        _rlog.info(
            "core_service",
            f"[WidgetCronScheduler:{self._scheduler_id}] 已注册 {job_id} (next={getattr(ap_trigger, 'fields', None) or trig.interval_seconds})",
        )

    # ------------------------------------------------------------------
    # 触发执行
    # ------------------------------------------------------------------

    async def _fire(self, *, ps_id: str, widget_id: str, trig: WidgetCronTrigger) -> None:
        run_id = uuid.uuid4().hex[:8]
        conn_id = f"cron:{self._scheduler_id}:{run_id}"
        request_id = f"cron:{trig.id}:{run_id}:{int(time.time())}"
        prompt = render_prompt(trig.prompt, trig.params)
        _rlog.info(
            "core_service",
            f"[WidgetCronScheduler] 触发 {ps_id}/{widget_id}/{trig.id} conn={conn_id}",
        )

        # open → chat → close（三段式，与 WS 客户端协议一致）
        try:
            await self._handle_inbound(conn_id, {
                "type": "open",
                "ps_id": ps_id,
                "request_id": request_id + ":open",
            })
            await self._handle_inbound(conn_id, {
                "type": "chat",
                "ps_id": ps_id,
                "widget_id": widget_id,
                "request_id": request_id,
                "user_id": trig.user_id,
                "content": prompt,
                "source": "cron",
                "trigger_id": trig.id,
            })
        except Exception:
            _rlog.exception("core_service", f"[WidgetCronScheduler] {ps_id}/{widget_id}/{trig.id} 投递失败")
            return
        finally:
            # close 即使前一段失败也要发，避免连接计数泄漏
            try:
                await self._handle_inbound(conn_id, {
                    "type": "close",
                    "ps_id": ps_id,
                    "request_id": request_id + ":close",
                })
            except Exception:
                _rlog.exception("core_service", f"[WidgetCronScheduler] {ps_id}/{widget_id}/{trig.id} close 失败")

    # ------------------------------------------------------------------
    # 自省
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._started

    def list_jobs(self) -> list[dict[str, Any]]:
        return [
            {
                "ps_id": ps,
                "widget_id": w,
                "trigger_id": t.id,
                "schedule": t.schedule,
                "interval_seconds": t.interval_seconds,
                "user_id": t.user_id,
            }
            for (ps, w, t) in self._registered
        ]

    def clear(self) -> None:
        try:
            self._scheduler.remove_all_jobs()
        except Exception:
            pass
        self._registered.clear()

    # ------------------------------------------------------------------
    # 命令接口：查询 / 暂停 / 恢复 / 立即触发
    # ------------------------------------------------------------------

    def list_all_disk_triggers(self) -> list[dict[str, Any]]:
        """重新扫描磁盘返回 *所有* cron 条目（含 enabled=False）。

        每个条目是 dict：
        - ps_id, widget_id, trig: WidgetCronTrigger
        - job_id: APScheduler job id
        - next_run_time: datetime | None（未启动或已暂停时为 None）
        """
        result: list[dict[str, Any]] = []
        if not self.ps_root.exists():
            return result
        for ps_dir in sorted(self.ps_root.iterdir()):
            if not ps_dir.is_dir():
                continue
            ps_id = ps_dir.name
            widgets_root = ps_dir / "widgets"
            if not widgets_root.exists():
                continue
            for widget_dir in sorted(widgets_root.iterdir()):
                if not widget_dir.is_dir():
                    continue
                widget_id = widget_dir.name
                wjson = widget_dir / "widget.json"
                if not wjson.exists():
                    continue
                try:
                    raw = json.loads(wjson.read_text(encoding="utf-8"))
                except Exception:
                    continue
                cron_list = raw.get("cron") or []
                if not isinstance(cron_list, list):
                    continue
                for idx, item in enumerate(cron_list):
                    if not isinstance(item, dict):
                        continue
                    fb_id = f"cr_{idx:02d}"
                    try:
                        trig = WidgetCronTrigger.from_dict(item, fallback_id=fb_id)
                    except Exception:
                        continue
                    job_id = f"{ps_id}__{widget_id}__{trig.id}"
                    next_run_time = None
                    if self._started:
                        job = self._scheduler.get_job(job_id)
                        if job is not None:
                            next_run_time = job.next_run_time
                    result.append({
                        "ps_id": ps_id,
                        "widget_id": widget_id,
                        "trig": trig,
                        "job_id": job_id,
                        "next_run_time": next_run_time,
                    })
        return result

    def _find_by_path(
        self, ps_id: str, widget_id: str, tid: str
    ) -> tuple[str, str, WidgetCronTrigger]:
        """从磁盘读取并返回 (ps_id, widget_id, trig)，不要求 enabled=True。"""
        wjson = self.ps_root / ps_id / "widgets" / widget_id / "widget.json"
        if not wjson.exists():
            raise ValueError(f"widget.json 不存在: {wjson}")
        try:
            raw = json.loads(wjson.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"解析 {wjson} 失败: {exc}") from exc
        cron_list = raw.get("cron") or []
        for idx, item in enumerate(cron_list):
            if not isinstance(item, dict):
                continue
            fb_id = f"cr_{idx:02d}"
            item_id = str(item.get("id") or fb_id)
            if item_id == tid:
                trig = WidgetCronTrigger.from_dict(item, fallback_id=fb_id)
                return ps_id, widget_id, trig
        raise ValueError(f"未找到 trigger {tid!r} in {ps_id}/{widget_id}")

    def find_trigger(
        self, id_or_path: str
    ) -> tuple[str, str, WidgetCronTrigger]:
        """灵活解析 trigger 标识，返回 (ps_id, widget_id, trig)。

        接受三种格式：
        - ``tid``              短 id，要求在所有 widget 内唯一
        - ``ps/widget/tid``   完整斜线路径
        - ``ps__widget__tid`` APScheduler job_id 格式（双下划线分隔）
        """
        if "/" in id_or_path:
            parts = id_or_path.split("/")
            if len(parts) != 3:
                raise ValueError(
                    f"非法路径 {id_or_path!r}，格式应为 ps/widget/tid"
                )
            return self._find_by_path(*parts)

        if "__" in id_or_path:
            # APScheduler job_id: ps__widget__tid （tid 本身可能含下划线）
            parts = id_or_path.split("__", 2)
            if len(parts) == 3:
                return self._find_by_path(*parts)

        # 短 id：全量扫描，要求全局唯一
        all_entries = self.list_all_disk_triggers()
        matches = [
            (e["ps_id"], e["widget_id"], e["trig"])
            for e in all_entries
            if e["trig"].id == id_or_path
        ]
        if not matches:
            raise ValueError(f"未找到 trigger: {id_or_path!r}")
        if len(matches) > 1:
            paths = [f"{ps}/{w}/{t.id}" for ps, w, t in matches]
            raise ValueError(
                f"trigger id {id_or_path!r} 在多处重复，请用完整路径: {paths}"
            )
        return matches[0]

    def _patch_widget_json(
        self, ps_id: str, widget_id: str, tid: str, *, enabled: bool
    ) -> None:
        """原子写入：将 widget.json 中匹配 tid 的 cron 条目的 enabled 字段改为 *enabled*。"""
        wjson = self.ps_root / ps_id / "widgets" / widget_id / "widget.json"
        raw = json.loads(wjson.read_text(encoding="utf-8"))
        cron_list = raw.get("cron") or []
        patched = False
        for idx, item in enumerate(cron_list):
            if not isinstance(item, dict):
                continue
            fb_id = f"cr_{idx:02d}"
            item_id = str(item.get("id") or fb_id)
            if item_id == tid:
                item["enabled"] = enabled
                patched = True
                break
        if not patched:
            raise ValueError(f"未找到 trigger {tid!r} in {ps_id}/{widget_id}")
        tmp = wjson.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(wjson)

    def pause_trigger(self, ps_id: str, widget_id: str, tid: str) -> None:
        """暂停 trigger：写 enabled=false + 从 APScheduler 和 _registered 移除。"""
        self._patch_widget_json(ps_id, widget_id, tid, enabled=False)
        job_id = f"{ps_id}__{widget_id}__{tid}"
        try:
            self._scheduler.remove_job(job_id)
        except Exception:
            pass  # 已经不在 scheduler 内也无妨
        self._registered = [
            (ps, w, t)
            for (ps, w, t) in self._registered
            if not (ps == ps_id and w == widget_id and t.id == tid)
        ]
        _rlog.info(
            "core_service",
            f"[WidgetCronScheduler:{self._scheduler_id}] 已暂停 {ps_id}/{widget_id}/{tid}",
        )

    def resume_trigger(self, ps_id: str, widget_id: str, tid: str) -> None:
        """恢复 trigger：写 enabled=true + 重新注册到 APScheduler。"""
        self._patch_widget_json(ps_id, widget_id, tid, enabled=True)
        # 从 _registered 先摘除（避免重复），再重新读磁盘注册
        self._registered = [
            (ps, w, t)
            for (ps, w, t) in self._registered
            if not (ps == ps_id and w == widget_id and t.id == tid)
        ]
        _, _, trig = self._find_by_path(ps_id, widget_id, tid)
        self._register(ps_id, widget_id, trig)
        _rlog.info(
            "core_service",
            f"[WidgetCronScheduler:{self._scheduler_id}] 已恢复 {ps_id}/{widget_id}/{tid}",
        )

    def run_once(
        self, ps_id: str, widget_id: str, trig: WidgetCronTrigger
    ) -> str:
        """立即触发一次（fire-and-forget）。返回 run_id。

        即使 trigger 处于暂停状态也可调用。
        """
        run_id = uuid.uuid4().hex[:8]
        asyncio.create_task(
            self._fire(ps_id=ps_id, widget_id=widget_id, trig=trig)
        )
        _rlog.info(
            "core_service",
            f"[WidgetCronScheduler:{self._scheduler_id}] 立即触发 {ps_id}/{widget_id}/{trig.id} run_id={run_id}",
        )
        return run_id

    # ------------------------------------------------------------------
    # 热更新：替换单个 widget 的全部 cron 条目
    # ------------------------------------------------------------------

    def reload_widget_crons(self, ps_id: str, widget_id: str) -> int:
        """移除 (ps_id, widget_id) 的所有已注册 job，重新从磁盘读取 widget.json 注册。

        由 REST API 在 PUT /cron 写入磁盘后调用，使 APScheduler 立即 pick up 新配置。
        返回新注册的 trigger 数量。
        """
        # 1. 找出所有属于该 widget 的 job id 并从 scheduler 移除
        to_remove = [
            (ps, w, t)
            for (ps, w, t) in self._registered
            if ps == ps_id and w == widget_id
        ]
        for ps, w, t in to_remove:
            job_id = f"{ps}__{w}__{t.id}"
            try:
                self._scheduler.remove_job(job_id)
            except Exception:
                pass  # 已不在 scheduler 内也无妨

        # 2. 从 _registered 清除
        self._registered = [
            (ps, w, t)
            for (ps, w, t) in self._registered
            if not (ps == ps_id and w == widget_id)
        ]

        # 3. 重新从磁盘读取并注册
        wjson = self.ps_root / ps_id / "widgets" / widget_id / "widget.json"
        if not wjson.exists():
            _rlog.warning(
                "core_service",
                f"[WidgetCronScheduler:{self._scheduler_id}] reload: widget.json 不存在 {wjson}",
            )
            return 0

        try:
            raw = json.loads(wjson.read_text(encoding="utf-8"))
        except Exception:
            _rlog.exception(
                "core_service",
                f"[WidgetCronScheduler:{self._scheduler_id}] reload: 解析 {wjson} 失败",
            )
            return 0

        cron_list = raw.get("cron") or []
        if not isinstance(cron_list, list):
            return 0

        added = 0
        for idx, item in enumerate(cron_list):
            fb_id = f"cr_{idx:02d}"
            try:
                trig = WidgetCronTrigger.from_dict(item, fallback_id=fb_id)
            except Exception:
                _rlog.exception(
                    "core_service",
                    f"[WidgetCronScheduler:{self._scheduler_id}] reload: cron[{idx}] 解析失败 {wjson}",
                )
                continue
            if not trig.enabled:
                continue
            try:
                self._register(ps_id, widget_id, trig)
                added += 1
            except Exception:
                _rlog.exception(
                    "core_service",
                    f"[WidgetCronScheduler:{self._scheduler_id}] reload: 注册 {ps_id}/{widget_id}/{trig.id} 失败",
                )

        _rlog.info(
            "core_service",
            f"[WidgetCronScheduler:{self._scheduler_id}] reload {ps_id}/{widget_id} 完成，注册 {added} 个 trigger",
        )
        return added


__all__ = ["WidgetCronScheduler"]
