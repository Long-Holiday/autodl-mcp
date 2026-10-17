import asyncio
import math
import random
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from autodl_mcp.client import AutoDLClient, AutoDLError

STARTABLE = {
    "shutdown",
    "shutdown_by_starting_error",
    "shutdown_by_running_error",
    "oss_merged",
    "created",
    "created_clone_success",
}
STOPPED = {"shutdown", "shutdown_by_starting_error", "shutdown_by_running_error"}
ACTIVE = {
    "queued",
    "checking",
    "waiting_capacity",
    "waiting_network",
    "starting",
    "confirming",
    "stopping",
}
# 这些阶段的下一次动作是重新发起开机请求，建议拉取时间要跨过两次尝试窗口。
RETRY_PHASES = {"waiting_capacity", "waiting_network"}


@dataclass
class Job:
    instance_uuid: str
    no_gpu: bool = False
    operation: str = "start"
    job_id: str = field(default_factory=lambda: uuid4().hex)
    phase: str = "queued"
    attempts: int = 0
    last_status: str | None = None
    error: dict[str, Any] | None = None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    finished_at: str | None = None
    task: asyncio.Task[None] | None = field(default=None, repr=False)

    started_monotonic: float = field(default_factory=lambda: time.monotonic(), repr=False)
    elapsed: float | None = field(default=None, repr=False)
    next_poll_monotonic: float | None = field(default=None, repr=False)
    previous_attempt_monotonic: float | None = field(default=None, repr=False)
    last_attempt_monotonic: float | None = field(default=None, repr=False)
    capacity_ready: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        remaining = (
            max(0, math.ceil(self.next_poll_monotonic - now))
            if self.next_poll_monotonic is not None
            else None
        )
        if remaining is None:
            suggested = None
        elif (
            self.phase in RETRY_PHASES
            and self.previous_attempt_monotonic is not None
            and self.last_attempt_monotonic is not None
        ):
            last_interval = max(
                0, math.ceil(self.last_attempt_monotonic - self.previous_attempt_monotonic)
            )
            suggested = remaining + last_interval
        else:
            suggested = remaining
        return {
            "job_id": self.job_id,
            "instance_uuid": self.instance_uuid,
            "no_gpu": self.no_gpu,
            "operation": self.operation,
            "target_mode": "non_gpu" if self.no_gpu else "gpu",
            "elapsed_seconds": self.elapsed
            if self.elapsed is not None
            else max(0.0, now - self.started_monotonic),
            "phase": self.phase,
            "attempts": self.attempts,
            "last_status": self.last_status,
            "error": self.error,
            "suggested_next_poll_seconds": suggested,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


class Jobs:
    def __init__(self, client: AutoDLClient):
        self.client = client
        self.settings = client.settings
        self.jobs: dict[str, Job] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.shutdown_pending: set[str] = set()
        self.closed = False

    def _lock(self, instance_uuid: str) -> asyncio.Lock:
        return self.locks.setdefault(instance_uuid, asyncio.Lock())

    def _active(self, instance_uuid: str) -> Job | None:
        return next(
            (
                j
                for j in self.jobs.values()
                if j.instance_uuid == instance_uuid and j.phase in ACTIVE
            ),
            None,
        )

    async def start(
        self, instance_uuid: str, *, no_gpu: bool = False, operation: str = "start"
    ) -> dict[str, Any]:
        self.settings.validate_uuid(instance_uuid)
        async with self._lock(instance_uuid):
            if self.closed:
                raise ValueError("MCP 服务正在关闭")
            if instance_uuid in self.shutdown_pending and operation == "start":
                status = await self.client.status(instance_uuid, include_hardware=False)
                if status["status"] not in STOPPED:
                    raise ValueError("该实例的关机尚未确认，请稍后查询状态")
                self.shutdown_pending.remove(instance_uuid)
            if job := self._active(instance_uuid):
                if job.no_gpu != no_gpu or job.operation != operation:
                    raise ValueError("该实例已有不同开机模式的活动任务，请先取消并确认实例状态")
                return job.snapshot()
            if len(self.jobs) >= 1000:
                completed = next(
                    (key for key, j in self.jobs.items() if j.phase not in ACTIVE), None
                )
                if completed is None:
                    raise ValueError("活动任务已达上限，请取消部分任务")
                del self.jobs[completed]
            job = Job(instance_uuid, no_gpu=no_gpu, operation=operation)
            self.jobs[job.job_id] = job
            job.task = asyncio.create_task(self._run(job), name=f"autodl-{operation}-{job.job_id}")
            return job.snapshot()

    def get(self, job_id: str) -> dict[str, Any]:
        if job_id not in self.jobs:
            raise ValueError("任务不存在；任务仅保存在当前服务进程中")
        return self.jobs[job_id].snapshot()

    async def switch(self, instance_uuid: str, target_mode: str = "gpu") -> dict[str, Any]:
        if target_mode not in {"gpu", "non_gpu"}:
            raise ValueError("target_mode 必须是 gpu 或 non_gpu")
        return await self.start(instance_uuid, no_gpu=target_mode == "non_gpu", operation="switch")

    async def wait(
        self, job_id: str, timeout_seconds: float = 15, *, return_on_capacity: bool = True
    ) -> dict[str, Any]:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0 <= timeout_seconds <= 300
            or not math.isfinite(timeout_seconds)
        ):
            raise ValueError("timeout_seconds 必须是 0 到 300 之间的有限秒数")
        if not isinstance(return_on_capacity, bool):
            raise ValueError("return_on_capacity 必须是布尔值")
        self.get(job_id)
        job = self.jobs[job_id]
        if job.phase not in ACTIVE or job.task is None or timeout_seconds == 0:
            return job.snapshot()
        if return_on_capacity and job.phase == "waiting_capacity":
            return job.snapshot()
        notification = (
            asyncio.create_task(job.capacity_ready.wait()) if return_on_capacity else None
        )
        try:
            tasks = {job.task}
            if notification is not None:
                tasks.add(notification)
            # Waiting never cancels the business task, including on caller disconnection.
            await asyncio.wait(tasks, timeout=timeout_seconds, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if notification is not None:
                notification.cancel()
                await asyncio.gather(notification, return_exceptions=True)
        return job.snapshot()

    async def _cancel(self, job: Job) -> None:
        if job.task and not job.task.done():
            job.task.add_done_callback(
                lambda _: self._finish(job, "cancelled") if job.phase in ACTIVE else None
            )
            job.task.cancel()
            await asyncio.gather(job.task, return_exceptions=True)
            if job.phase in ACTIVE:
                self._finish(job, "cancelled")

    async def cancel(self, job_id: str) -> dict[str, Any]:
        self.get(job_id)
        job = self.jobs[job_id]
        async with self._lock(job.instance_uuid):
            await self._cancel(job)
            result = {
                **job.snapshot(),
                "resulting_status": None,
                "resulting_start_mode": None,
                "resulting_observed_at": None,
                "resulting_status_error": None,
            }
            note = "只取消后续步骤，不撤销已接受的电源请求，不自动恢复或关机；"
            try:
                observation = await self.client.status(job.instance_uuid, include_hardware=False)
            except AutoDLError as exc:
                result["resulting_status_error"] = exc.as_dict()
                note += "取消后的物理状态查询失败，请调用 instance_status 确认后再决定"
            else:
                result["resulting_status"] = observation.get("status")
                result["resulting_start_mode"] = observation.get("start_mode")
                result["resulting_observed_at"] = observation.get("observed_at")
                note += (
                    "resulting_status 是取消后立即查询到的物理状态，"
                    "已接受的电源请求仍可能继续改变它"
                )
            result["note"] = note
            return result

    async def stop(self, instance_uuid: str) -> dict[str, Any]:
        self.settings.validate_uuid(instance_uuid)
        async with self._lock(instance_uuid):
            self.shutdown_pending.add(instance_uuid)
            if job := self._active(instance_uuid):
                await self._cancel(job)
            status = await self.client.status(instance_uuid, include_hardware=False)
            if status["status"] in STOPPED:
                self.shutdown_pending.discard(instance_uuid)
                return {**status, "accepted": False, "already_stopped": True}
            if status["status"] == "shutting_down":
                return {**status, "accepted": True, "already_stopping": True}
            await self.client.power_off(instance_uuid)
            return {
                "instance_uuid": instance_uuid,
                "accepted": True,
                "note": "关机请求已接受，请通过 instance_status 确认最终状态",
            }

    def _finish(self, job: Job, phase: str) -> None:
        job.elapsed = max(0.0, time.monotonic() - job.started_monotonic)
        job.phase = phase
        job.next_poll_monotonic = None
        job.finished_at = datetime.now(UTC).isoformat()

    async def _wait(self, job: Job, phase: str, delay: float, deadline: float | None) -> None:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AutoDLError("timeout", "已达到开机最大等待时间")
            delay = min(delay, remaining)
        job.phase = phase
        job.next_poll_monotonic = time.monotonic() + delay
        if phase == "waiting_capacity":
            job.capacity_ready.set()
        else:
            job.capacity_ready.clear()
        await asyncio.sleep(delay)
        job.capacity_ready.clear()
        job.next_poll_monotonic = None

    async def _observe(self, job: Job) -> dict[str, Any]:
        observation = await self.client.status(job.instance_uuid, include_hardware=False)
        job.last_status = observation["status"]
        return observation

    def _running_matches(self, job: Job, observation: dict[str, Any]) -> bool:
        if observation["status"] != "running":
            return False
        if job.no_gpu:
            return observation.get("start_mode") == "non_gpu"
        return observation.get("start_mode") != "non_gpu"

    def _retry_delay(self, base: float, ceiling: float, retry_after: float | None = None) -> float:
        delay = min(base, ceiling) * random.uniform(1 - self.settings.retry_jitter, 1)
        return max(delay, retry_after or 0)

    async def _confirm(self, job: Job, *, initial_error: AutoDLError | None = None) -> None:
        deadline = time.monotonic() + self.settings.start_timeout
        ambiguous = initial_error is not None
        transient_delay = self.settings.transient_retry_interval
        job.phase = "confirming"
        if initial_error is not None:
            await self._wait(
                job,
                "confirming",
                self._retry_delay(
                    transient_delay,
                    self.settings.transient_retry_max_interval,
                    initial_error.retry_after,
                ),
                deadline,
            )
            transient_delay = min(transient_delay * 1.5, self.settings.transient_retry_max_interval)
        while time.monotonic() < deadline:
            try:
                observation = await self._observe(job)
                status = observation["status"]
            except AutoDLError as exc:
                if exc.kind not in {"network", "rate_limit"}:
                    raise
                job.error = exc.as_dict()
                delay = self._retry_delay(
                    transient_delay,
                    self.settings.transient_retry_max_interval,
                    exc.retry_after,
                )
                transient_delay = min(
                    transient_delay * 1.5,
                    self.settings.transient_retry_max_interval,
                )
            else:
                transient_delay = self.settings.transient_retry_interval
                job.error = None
                if self._running_matches(job, observation):
                    self._finish(job, "succeeded")
                    return
                if not ambiguous and status not in STARTABLE | {
                    "starting",
                    "restarting",
                    "running",
                }:
                    raise AutoDLError("state", "开机确认时实例进入非预期状态，请查看控制台")
                if not ambiguous and status in {
                    "shutdown_by_starting_error",
                    "shutdown_by_running_error",
                }:
                    raise AutoDLError("state", "实例启动失败，请查看控制台")
                delay = self._retry_delay(self.settings.poll_interval, self.settings.poll_interval)
            await self._wait(job, "confirming", delay, deadline)
        raise AutoDLError(
            "uncertain" if ambiguous else "timeout",
            "开机结果未确认；为避免重复操作已停止，请查询实例状态",
            ambiguous=ambiguous,
        )

    async def _switch_off(self, job: Job) -> None:
        observation = await self._observe(job)
        if job.no_gpu and observation.get("charge_type") != "payg":
            raise AutoDLError("state", "无卡模式开机仅支持按量计费实例")
        if self._running_matches(job, observation):
            return
        status = observation["status"]
        if status in STARTABLE:
            self.shutdown_pending.discard(job.instance_uuid)
            return
        if status not in {"running", "starting", "restarting", "shutting_down"}:
            raise AutoDLError("state", "实例当前状态不允许切换模式，请查看控制台")
        self.shutdown_pending.add(job.instance_uuid)
        job.phase = "stopping"
        deadline = time.monotonic() + self.settings.stop_timeout
        ambiguous = False
        transient_delay = self.settings.transient_retry_interval
        if status != "shutting_down":
            try:
                await self.client.power_off(job.instance_uuid)
            except AutoDLError as exc:
                if not exc.ambiguous:
                    raise
                ambiguous = True
                job.error = exc.as_dict()
                if time.monotonic() < deadline:
                    await self._wait(
                        job,
                        "stopping",
                        self._retry_delay(
                            transient_delay,
                            self.settings.transient_retry_max_interval,
                            exc.retry_after,
                        ),
                        deadline,
                    )
        while time.monotonic() < deadline:
            try:
                observation = await self._observe(job)
            except AutoDLError as exc:
                if exc.kind not in {"network", "rate_limit"}:
                    raise
                job.error = exc.as_dict()
                delay = self._retry_delay(
                    transient_delay, self.settings.transient_retry_max_interval, exc.retry_after
                )
                transient_delay = min(
                    transient_delay * 1.5, self.settings.transient_retry_max_interval
                )
            else:
                job.error = None
                if observation["status"] in STOPPED:
                    self.shutdown_pending.discard(job.instance_uuid)
                    return
                if observation["status"] not in {
                    "running",
                    "starting",
                    "restarting",
                    "shutting_down",
                }:
                    raise AutoDLError("state", "关机确认时实例进入非预期状态，请查看控制台")
                transient_delay = self.settings.transient_retry_interval
                delay = self._retry_delay(self.settings.poll_interval, self.settings.poll_interval)
            if time.monotonic() >= deadline:
                break
            await self._wait(job, "stopping", delay, deadline)
        raise AutoDLError(
            "uncertain" if ambiguous else "timeout",
            "关机结果未确认；已停止切换，不会自动开机，请查询实例状态",
            ambiguous=ambiguous,
        )

    async def _run_start(self, job: Job) -> None:
        deadline = (
            time.monotonic() + self.settings.max_wait_seconds
            if self.settings.max_wait_seconds
            else None
        )
        capacity_delay = self.settings.retry_interval
        transient_delay = self.settings.transient_retry_interval
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                raise AutoDLError("timeout", "已达到开机最大等待时间")
            job.phase = "checking"
            try:
                observation = await self._observe(job)
                status = observation["status"]
                transient_delay = self.settings.transient_retry_interval
                if status in {"running", "starting", "restarting"}:
                    mode = observation.get("start_mode")
                    if (mode == "non_gpu" and not job.no_gpu) or (
                        job.no_gpu and mode and mode != "non_gpu"
                    ):
                        raise AutoDLError(
                            "state", "实例已以其他模式运行或启动，请先关机后再选择开机模式"
                        )
                if self._running_matches(job, observation):
                    job.error = None
                    self._finish(job, "succeeded")
                    return
                if status in {"starting", "restarting", "running"}:
                    await self._confirm(job)
                    return
                if status not in STARTABLE:
                    raise AutoDLError("state", "实例当前状态不允许开机，请查看控制台")
                if job.no_gpu and observation.get("charge_type") != "payg":
                    raise AutoDLError("state", "无卡模式开机仅支持按量计费实例")
                job.phase = "starting"
                job.previous_attempt_monotonic = job.last_attempt_monotonic
                job.last_attempt_monotonic = time.monotonic()
                job.attempts += 1
                if job.no_gpu:
                    await self.client.power_on(job.instance_uuid, no_gpu=True)
                else:
                    await self.client.power_on(job.instance_uuid)
            except AutoDLError as exc:
                job.error = exc.as_dict()
                if exc.ambiguous:
                    await self._confirm(job, initial_error=exc)
                    return
                if exc.kind == "capacity":
                    phase = "waiting_capacity"
                    delay = self._retry_delay(capacity_delay, self.settings.retry_max_interval)
                    capacity_delay = min(capacity_delay * 1.5, self.settings.retry_max_interval)
                elif exc.kind in {"network", "rate_limit"}:
                    phase = "waiting_network"
                    delay = self._retry_delay(
                        transient_delay,
                        self.settings.transient_retry_max_interval,
                        exc.retry_after,
                    )
                    transient_delay = min(
                        transient_delay * 1.5,
                        self.settings.transient_retry_max_interval,
                    )
                else:
                    raise
                await self._wait(job, phase, delay, deadline)
                continue
            await self._confirm(job)
            return

    async def _run(self, job: Job) -> None:
        try:
            if job.operation == "switch":
                await self._switch_off(job)
            await self._run_start(job)
        except asyncio.CancelledError:
            self._finish(job, "cancelled")
            raise
        except AutoDLError as exc:
            job.error = exc.as_dict()
            self._finish(job, "failed")
        except Exception:
            job.error = {"kind": "internal", "message": "后台任务发生内部错误", "ambiguous": False}
            self._finish(job, "failed")

    async def close(self) -> None:
        self.closed = True
        for job in list(self.jobs.values()):
            await self._cancel(job)
