import asyncio
from types import SimpleNamespace

import pytest

import autodl_mcp.jobs as jobs_module
from autodl_mcp.client import AutoDLError
from autodl_mcp.config import Settings
from autodl_mcp.jobs import Jobs


class FakeClient:
    def __init__(self, **settings):
        defaults = {
            "retry_interval": 0.005,
            "retry_max_interval": 0.01,
            "transient_retry_interval": 0.005,
            "transient_retry_max_interval": 0.01,
            "poll_interval": 0.005,
            "start_timeout": 0.1,
        }
        self.settings = Settings(token="test", **(defaults | settings))
        self.state = "shutdown"
        self.starts = 0
        self.stops = 0
        self.capacity_failures = 0
        self.start_error = None
        self.status_error = None
        self.hold_start = None
        self.start_entered = asyncio.Event()
        self.auto_running = True

    async def status(self, uuid):
        if self.status_error:
            raise self.status_error
        return {"instance_uuid": uuid, "status": self.state}

    async def power_on(self, uuid):
        self.starts += 1
        self.start_entered.set()
        if self.hold_start:
            await self.hold_start.wait()
        if self.starts <= self.capacity_failures:
            raise AutoDLError("capacity", "缺货")
        if self.start_error:
            raise self.start_error
        self.state = "running" if self.auto_running else "starting"

    async def power_off(self, uuid):
        self.stops += 1
        self.state = "shutdown"


async def completed(jobs, job):
    await asyncio.wait_for(jobs.jobs[job["job_id"]].task, timeout=1)
    return jobs.get(job["job_id"])


async def test_capacity_retry_then_success():
    client = FakeClient()
    client.capacity_failures = 2
    jobs = Jobs(client)
    result = await completed(jobs, await jobs.start("target"))
    assert result["phase"] == "succeeded"
    assert result["attempts"] == 3
    assert result["last_status"] == "running"
    assert result["error"] is None
    await jobs.close()


async def test_already_running_never_starts():
    client = FakeClient()
    client.state = "running"
    jobs = Jobs(client)
    result = await completed(jobs, await jobs.start("target"))
    assert result["phase"] == "succeeded"
    assert client.starts == 0


async def test_unknown_error_stops():
    client = FakeClient()
    client.start_error = AutoDLError("business", "余额不足")
    jobs = Jobs(client)
    result = await completed(jobs, await jobs.start("target"))
    assert result["phase"] == "failed"
    assert client.starts == 1


async def test_duplicate_start_and_shutdown_cancel_inflight():
    client = FakeClient()
    client.hold_start = asyncio.Event()
    jobs = Jobs(client)
    job = await jobs.start("target")
    await client.start_entered.wait()
    duplicate = await jobs.start("target")
    assert duplicate["job_id"] == job["job_id"]
    client.state = "starting"
    await jobs.stop("target")
    assert jobs.get(job["job_id"])["phase"] == "cancelled"
    assert client.starts == 1
    assert client.stops == 1
    assert client.state == "shutdown"


async def test_cancel_before_background_task_runs():
    client = FakeClient()
    jobs = Jobs(client)
    job = await jobs.start("target")
    result = await jobs.cancel(job["job_id"])
    assert result["phase"] == "cancelled"
    assert client.starts == 0
    assert client.stops == 0


async def test_shutdown_is_idempotent():
    client = FakeClient()
    jobs = Jobs(client)
    assert (await jobs.stop("target"))["already_stopped"]
    assert client.stops == 0


async def test_cancel_does_not_stop_running_instance():
    client = FakeClient()
    jobs = Jobs(client)
    job = await jobs.start("target")
    await completed(jobs, job)
    result = await jobs.cancel(job["job_id"])
    assert result["phase"] == "succeeded"
    assert client.state == "running"
    assert client.stops == 0


async def test_capacity_wait_limit():
    client = FakeClient(max_wait_seconds=0.02)
    client.capacity_failures = 100
    jobs = Jobs(client)
    result = await completed(jobs, await jobs.start("target"))
    assert result["phase"] == "failed"
    assert result["error"]["kind"] == "timeout"
    assert client.starts < 100


async def test_confirmation_timeout_does_not_resend():
    client = FakeClient()
    client.auto_running = False
    jobs = Jobs(client)
    result = await completed(jobs, await jobs.start("target"))
    assert result["phase"] == "failed"
    assert result["error"]["kind"] == "timeout"
    assert client.starts == 1


async def test_ambiguous_mutation_never_resends():
    client = FakeClient()
    client.start_error = AutoDLError("network", "请求超时", ambiguous=True)
    jobs = Jobs(client)
    result = await completed(jobs, await jobs.start("target"))
    assert result["phase"] == "failed"
    assert result["error"]["ambiguous"]
    assert client.starts == 1


async def test_network_queries_obey_configured_wait_limit():
    client = FakeClient(max_wait_seconds=0.03)
    client.status_error = AutoDLError("network", "请求超时")
    jobs = Jobs(client)
    result = await completed(jobs, await jobs.start("target"))
    assert result["phase"] == "failed"
    assert client.starts == 0


async def test_shutting_down_blocks_start_after_stop_request():
    client = FakeClient()
    client.state = "running"
    jobs = Jobs(client)
    await jobs.stop("target")
    client.state = "shutting_down"
    with pytest.raises(ValueError, match="关机尚未确认"):
        await jobs.start("target")
    client.state = "shutdown"
    result = await completed(jobs, await jobs.start("target"))
    assert result["phase"] == "succeeded"


async def test_close_cancels_tasks_and_rejects_starts():
    client = FakeClient()
    jobs = Jobs(client)
    job = await jobs.start("target")
    await jobs.close()
    assert jobs.get(job["job_id"])["phase"] == "cancelled"
    with pytest.raises(ValueError, match="关闭"):
        await jobs.start("target")


@pytest.mark.parametrize("state", ["removing", "unknown", "shutting_down"])
async def test_non_startable_state_never_mutates(state):
    client = FakeClient()
    client.state = state
    jobs = Jobs(client)
    result = await completed(jobs, await jobs.start("target"))
    assert result["phase"] == "failed"
    assert client.starts == 0


async def test_shutdown_and_new_start_are_serialized():
    client = FakeClient()
    client.state = "running"
    entered = asyncio.Event()
    release = asyncio.Event()

    async def delayed_stop(uuid):
        entered.set()
        await release.wait()
        client.stops += 1
        client.state = "shutting_down"

    client.power_off = delayed_stop
    jobs = Jobs(client)
    stop = asyncio.create_task(jobs.stop("target"))
    await entered.wait()
    start = asyncio.create_task(jobs.start("target"))
    release.set()
    await stop
    with pytest.raises(ValueError, match="关机尚未确认"):
        await start
    assert client.starts == 0


async def test_ambiguous_request_can_be_confirmed_without_resending():
    client = FakeClient()

    async def accepted_then_timeout(uuid):
        client.starts += 1
        client.state = "running"
        raise AutoDLError("network", "请求超时", ambiguous=True)

    client.power_on = accepted_then_timeout
    jobs = Jobs(client)
    result = await completed(jobs, await jobs.start("target"))
    assert result["phase"] == "succeeded"
    assert client.starts == 1
    assert result["error"] is None


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=0.0, sleeps=[])

    async def sleep(delay):
        clock.sleeps.append(delay)
        clock.now += delay

    monkeypatch.setattr(jobs_module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(
        jobs_module, "asyncio", SimpleNamespace(**(vars(asyncio) | {"sleep": sleep}))
    )
    monkeypatch.setattr(jobs_module.random, "uniform", lambda lower, upper: upper)
    return clock


async def test_hours_of_capacity_waiting_with_virtual_clock(clock):
    client = FakeClient(retry_interval=60, retry_max_interval=300)
    client.capacity_failures = 60
    jobs = Jobs(client)
    result = await completed(jobs, await jobs.start("target"))
    assert result["phase"] == "succeeded"
    assert result["attempts"] == 61
    assert clock.now > 4 * 3600
    assert clock.sleeps[:3] == [60, 90, 135]
    assert max(clock.sleeps) == 300


async def test_more_than_five_network_failures_can_recover(clock):
    client = FakeClient(transient_retry_interval=60, transient_retry_max_interval=900)
    reads = 0

    async def status(uuid):
        nonlocal reads
        reads += 1
        if reads <= 10:
            raise AutoDLError("network", "暂时不可用")
        return {"status": client.state}

    client.status = status
    jobs = Jobs(client)
    result = await completed(jobs, await jobs.start("target"))
    assert result["phase"] == "succeeded"
    assert client.starts == 1
    assert clock.sleeps[:3] == [60, 90, 135]
    assert max(clock.sleeps) == 900


@pytest.mark.parametrize("sample,expected", [(0.7, 210), (0.85, 255), (1, 300)])
def test_jitter_persists_at_capacity_ceiling(monkeypatch, sample, expected):
    client = FakeClient(retry_interval=60, retry_max_interval=300)
    jobs = Jobs(client)
    monkeypatch.setattr(jobs_module.random, "uniform", lambda lower, upper: sample)
    assert jobs._retry_delay(1000, 300) == expected
    assert jobs._retry_delay(1000, 300, 1200) == 1200


async def test_retry_after_is_minimum_before_next_read(clock):
    client = FakeClient(transient_retry_interval=60, transient_retry_max_interval=900)
    observed = []

    async def status(uuid):
        observed.append(clock.now)
        if len(observed) == 1:
            raise AutoDLError("rate_limit", "限流", retry_after=1800)
        return {"status": client.state}

    client.status = status
    jobs = Jobs(client)
    result = await completed(jobs, await jobs.start("target"))
    assert result["phase"] == "succeeded"
    assert observed[:2] == [0, 1800]


async def test_ambiguous_cooldown_exceeding_deadline_never_queries_or_resends(clock):
    client = FakeClient(
        start_timeout=600, transient_retry_interval=60, transient_retry_max_interval=900
    )
    reads = 0

    async def status(uuid):
        nonlocal reads
        reads += 1
        return {"status": client.state}

    client.status = status
    client.start_error = AutoDLError("rate_limit", "限流", ambiguous=True, retry_after=1800)
    jobs = Jobs(client)
    result = await completed(jobs, await jobs.start("target"))
    assert result["phase"] == "failed"
    assert result["error"]["kind"] == "uncertain"
    assert reads == 1
    assert client.starts == 1
    assert clock.now == 600


async def test_confirmation_reads_back_off_after_rate_limit(clock):
    client = FakeClient(
        start_timeout=600, transient_retry_interval=60, transient_retry_max_interval=900
    )
    observed = []

    async def status(uuid):
        observed.append(clock.now)
        if len(observed) in {2, 3}:
            raise AutoDLError("rate_limit", "限流", retry_after=120)
        return {"status": client.state}

    client.status = status
    jobs = Jobs(client)
    result = await completed(jobs, await jobs.start("target"))
    assert result["phase"] == "succeeded"
    assert observed == [0, 0, 120, 240]
    assert client.starts == 1


@pytest.mark.parametrize("action", ["cancel", "stop"])
async def test_long_sleep_is_interruptible(action):
    client = FakeClient(retry_interval=300, retry_max_interval=300)
    client.capacity_failures = 100
    jobs = Jobs(client)
    job = await jobs.start("target")
    await client.start_entered.wait()
    if action == "cancel":
        await asyncio.wait_for(jobs.cancel(job["job_id"]), timeout=0.2)
    else:
        await asyncio.wait_for(jobs.stop("target"), timeout=0.2)
    assert jobs.get(job["job_id"])["phase"] == "cancelled"
    assert client.starts == 1
