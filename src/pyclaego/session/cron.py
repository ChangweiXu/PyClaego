"""Session 级定时任务调度器（基于 APScheduler v3 AsyncIOScheduler）

每个 Session 拥有一个独立的 SessionCronScheduler。Cron 触发时合成一条 user_message
入队到 Session 的 _message_queue，复用现有的串行处理路径。
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from ..logging import get_running_log

if TYPE_CHECKING:
    from .session import Session

_rlog = get_running_log()

# 降低 APScheduler 自身日志噪声
logging.getLogger("apscheduler").setLevel(logging.WARNING)


_SLUG_RE = re.compile(r"[^a-z0-9_-]+")


def slugify(name: str) -> str:
    """将 cron 任务名称转换为文件系统安全的 slug

    规则：lowercase → 非 [a-z0-9_-] 替换为 _ → 折叠重复 _ → 去除首尾 _-
    """
    s = name.strip().lower()
    s = _SLUG_RE.sub("_", s)
    s = re.sub(r"_+", "_", s)
    s = s.strip("_-")
    return s or "unnamed"


@dataclass
class CronJob:
    """单个 cron 任务的运行时记录"""

    name: str
    schedule: str
    prompt: str
    enabled: bool = True
    save_to_file: bool = True
    broadcast: bool = True
    paused: bool = False  # 内存态，由 /cron pause/resume 控制
    metadata: Dict[str, Any] = field(default_factory=dict)


class SessionCronScheduler:
    """每 Session 一个的 cron 调度器

    封装 AsyncIOScheduler，提供：
    - start() / shutdown()
    - list_jobs() / pause() / resume() / run_now()
    - 触发回调统一调用 session._enqueue_cron(job)
    """

    def __init__(
        self,
        session: "Session",
        cron_cfg: Dict[str, Any],
    ):
        """初始化调度器

        Args:
            session: 所属 Session 实例
            cron_cfg: 该 session 的 cron 配置 dict（已确保 enabled=True 才会构造）

        Raises:
            ValueError: 任务名称重复或配置非法
            ImportError: 未安装 apscheduler
        """
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from apscheduler.triggers.cron import CronTrigger
        except ImportError as e:
            raise ImportError(
                "apscheduler 未安装，无法启用 cron 功能。"
                "请运行: pip install 'apscheduler>=3.10,<4'"
            ) from e

        self._AsyncIOScheduler = AsyncIOScheduler
        self._CronTrigger = CronTrigger

        self.session = session
        self.session_id = session.session_id
        self.timezone: str = cron_cfg.get("timezone") or "UTC"

        jobs_cfg: List[Dict[str, Any]] = cron_cfg.get("jobs") or []
        self.jobs: Dict[str, CronJob] = {}
        self._build_jobs(jobs_cfg)

        # 创建 AsyncIOScheduler，但延迟到 start() 才注册任务（绑定 running loop）
        self._scheduler = AsyncIOScheduler(timezone=self.timezone)
        self._started = False
        self._cron_cfg = cron_cfg

    # ------------------------------------------------------------------
    # 构造与生命周期
    # ------------------------------------------------------------------

    def _build_jobs(self, jobs_cfg: List[Dict[str, Any]]) -> None:
        """从配置构造 CronJob 列表，校验唯一性"""
        seen: set = set()
        for idx, j in enumerate(jobs_cfg):
            name = j.get("name")
            schedule = j.get("schedule")
            prompt = j.get("prompt")
            if not name or not schedule or prompt is None:
                raise ValueError(
                    f"[cron] jobs[{idx}] 缺少必填字段 name/schedule/prompt: {j}"
                )
            if name in seen:
                raise ValueError(f"[cron] 任务名称重复: '{name}'")
            seen.add(name)

            output = j.get("output") or {}
            self.jobs[name] = CronJob(
                name=name,
                schedule=schedule,
                prompt=prompt,
                enabled=bool(j.get("enabled", True)),
                save_to_file=bool(output.get("save_to_file", True)),
                broadcast=bool(output.get("broadcast", True)),
                metadata=j.get("metadata") or {},
            )

    def start(self) -> None:
        """启动调度器，注册所有启用的任务

        必须在 asyncio 事件循环内调用。
        """
        if self._started:
            return

        for name, job in self.jobs.items():
            if not job.enabled:
                _rlog.info(
                    f"session_{self.session_id}",
                    f"[CronScheduler] 跳过未启用任务: {name}",
                )
                continue
            self._add_aps_job(job)

        self._scheduler.start()
        self._started = True
        _rlog.info(
            f"session_{self.session_id}",
            f"[CronScheduler] 已启动，时区={self.timezone}，任务数={len(self.jobs)}",
        )

    def _add_aps_job(self, job: CronJob) -> None:
        """向 APScheduler 注册一个任务"""
        try:
            trigger = self._CronTrigger.from_crontab(job.schedule, timezone=self.timezone)
        except Exception as e:
            _rlog.error(
                f"session_{self.session_id}",
                f"[CronScheduler] 任务 '{job.name}' 解析 cron 表达式失败: {e}",
            )
            raise

        self._scheduler.add_job(
            self._on_fire,
            trigger=trigger,
            id=job.name,
            name=job.name,
            args=[job.name],
            coalesce=True,
            max_instances=1,
            misfire_grace_time=300,
            replace_existing=True,
        )

    def shutdown(self, wait: bool = False) -> None:
        """关闭调度器

        Args:
            wait: 是否等待正在执行的回调结束（一般传 False，已入队的任务由队列处理）
        """
        if not self._started:
            return
        try:
            self._scheduler.shutdown(wait=wait)
        except Exception as e:
            _rlog.warning(
                f"session_{self.session_id}",
                f"[CronScheduler] shutdown 异常: {e}",
            )
        self._started = False
        _rlog.info(f"session_{self.session_id}", "[CronScheduler] 已关闭")

    # ------------------------------------------------------------------
    # 触发回调
    # ------------------------------------------------------------------

    async def _on_fire(self, name: str) -> None:
        """APScheduler 回调：将任务投递到 session 队列

        APScheduler 在事件循环里 schedule 这个 coroutine。
        """
        job = self.jobs.get(name)
        if job is None:
            _rlog.warning(
                f"session_{self.session_id}",
                f"[CronScheduler] 触发未知任务: {name}",
            )
            return

        if job.paused:
            _rlog.info(
                f"session_{self.session_id}",
                f"[CronScheduler] 任务已暂停，跳过本次触发: {name}",
            )
            return

        try:
            await self.session._enqueue_cron(job)
        except Exception as e:
            import traceback
            _rlog.error(
                f"session_{self.session_id}",
                f"[CronScheduler] 任务 '{name}' 入队失败: {e}\n{traceback.format_exc()}",
            )

    # ------------------------------------------------------------------
    # /cron 命令支持
    # ------------------------------------------------------------------

    def list_jobs(self) -> List[Dict[str, Any]]:
        """返回所有任务的运行时状态"""
        out: List[Dict[str, Any]] = []
        for name, job in self.jobs.items():
            aps_job = self._scheduler.get_job(name) if self._started else None
            next_fire = (
                aps_job.next_run_time.isoformat()
                if aps_job and aps_job.next_run_time
                else None
            )
            out.append({
                "name": name,
                "schedule": job.schedule,
                "enabled": job.enabled,
                "paused": job.paused,
                "next_fire_time": next_fire,
                "prompt_preview": job.prompt[:60] + ("…" if len(job.prompt) > 60 else ""),
            })
        return out

    def pause(self, name: str) -> bool:
        """暂停任务（仅内存态，重启后恢复）"""
        job = self.jobs.get(name)
        if job is None:
            return False
        job.paused = True
        return True

    def resume(self, name: str) -> bool:
        """恢复已暂停的任务"""
        job = self.jobs.get(name)
        if job is None:
            return False
        job.paused = False
        return True

    async def run_now(self, name: str) -> bool:
        """立即触发一次任务（绕过 paused 检查由调用方决定；这里直接入队）"""
        job = self.jobs.get(name)
        if job is None:
            return False
        await self.session._enqueue_cron(job)
        return True
