import asyncio
from types import SimpleNamespace

import pytest

import autodl_mcp.jobs as jobs_module
from autodl_mcp.client import AutoDLError
from autodl_mcp.config import Settings
from autodl_mcp.jobs import Job, Jobs


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
        self.start_mode = "gpu"
        self.charge_type = "payg"
        self.start_requests = []
        self.starts = 0
        self.stops = 0
        self.capacity_failures = 0
        self.start_error = None
        self.status_error = None
        self.hold_start = None
        self.start_entered = asyncio.Event()
        self.auto_running = True

    async def status(self, uuid, *, include_hardware=True):
        assert include_hardware is False
        if self.status_error:
            raise self.status_error
        return {
            "instance_uuid": uuid,
            "status": self.state,
            "start_mode": self.start_mode,
            "charge_type": self.charge_type,
        }

    async def power_on(self, uuid, *, no_gpu=False):
        self.start_requests.append(no_gpu)
        self.starts += 1
        self.start_entered.set()
        if self.hold_start:
            await self.hold_start.wait()
        if self.starts <= self.capacity_failures:
            raise AutoDLError("capacity", "缺货")
        if self.start_error:
            raise self.start_error
        self.start_mode = "non_gpu" if no_gpu else "gpu"
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


@pytest.mark.parametrize("no_gpu", [False, True])
async def test_duplicate_start_and_shutdown_cancel_inflight(no_gpu):
    client = FakeClient()
    client.hold_start = asyncio.Event()
    jobs = Jobs(client)
    job = await jobs.start("target", no_gpu=no_gpu)
    await client.start_entered.wait()
    duplicate = await jobs.start("target", no_gpu=no_gpu)
    assert duplicate["job_id"] == job["job_id"]
    client.state = "starting"
    await jobs.stop("target")
    assert jobs.get(job["job_id"])["phase"] == "cancelled"
    assert client.starts == 1
    assert client.stops == 1
    assert client.state == "shutdown"


@pytest.mark.parametrize("no_gpu", [False, True])
async def test_cancel_before_background_task_runs(no_gpu):
    client = FakeClient()
    jobs = Jobs(client)
    job = await jobs.start("target", no_gpu=no_gpu)
    result = await jobs.cancel(job["job_id"])
    assert result["phase"] == "cancelled"
    assert client.starts == 0
    assert client.stops == 0


async def test_shutdown_is_idempotent():
    client = FakeClient()
    jobs = Jobs(client)
    assert (await jobs.stop("target"))["already_stopped"]
    assert client.stops == 0


@pytest.mark.parametrize("no_gpu", [False, True])
async def test_cancel_does_not_stop_running_instance(no_gpu):
    client = FakeClient()
    jobs = Jobs(client)
    job = await jobs.start("target", no_gpu=no_gpu)
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


@pytest.mark.parametrize("no_gpu", [False, True])
async def test_confirmation_timeout_does_not_resend(no_gpu):
    client = FakeClient()
    client.auto_running = False
    jobs = Jobs(client)
    result = await completed(jobs, await jobs.start("target", no_gpu=no_gpu))
    assert result["phase"] == "failed"
    assert result["error"]["kind"] == "timeout"
    assert client.starts == 1


@pytest.mark.parametrize("no_gpu", [False, True])
async def test_ambiguous_mutation_never_resends(no_gpu):
    client = FakeClient()
    client.start_error = AutoDLError("network", "请求超时", ambiguous=True)
    jobs = Jobs(client)
    result = await completed(jobs, await jobs.start("target", no_gpu=no_gpu))
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


@pytest.mark.parametrize("no_gpu", [False, True])
async def test_shutting_down_blocks_start_after_stop_request(no_gpu):
    client = FakeClient()
    client.state = "running"
    jobs = Jobs(client)
    await jobs.stop("target")
    client.state = "shutting_down"
    with pytest.raises(ValueError, match="关机尚未确认"):
        await jobs.start("target", no_gpu=no_gpu)
    client.state = "shutdown"
    result = await completed(jobs, await jobs.start("target", no_gpu=no_gpu))
    assert result["phase"] == "succeeded"


@pytest.mark.parametrize("no_gpu", [False, True])
async def test_close_cancels_tasks_and_rejects_starts(no_gpu):
    client = FakeClient()
    jobs = Jobs(client)
    job = await jobs.start("target", no_gpu=no_gpu)
    await jobs.close()
    assert jobs.get(job["job_id"])["phase"] == "cancelled"
    with pytest.raises(ValueError, match="关闭"):
        await jobs.start("target", no_gpu=no_gpu)


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

    async def status(uuid, *, include_hardware=True):
        assert include_hardware is False
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

    async def status(uuid, *, include_hardware=True):
        assert include_hardware is False
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

    async def status(uuid, *, include_hardware=True):
        assert include_hardware is False
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

    async def status(uuid, *, include_hardware=True):
        assert include_hardware is False
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


async def test_no_gpu_start_success():
    client = FakeClient()
    jobs = Jobs(client)
    result = await completed(jobs, await jobs.start("target", no_gpu=True))
    assert result["phase"] == "succeeded"
    assert result["no_gpu"] is True
    assert client.start_requests == [True]
    assert client.start_mode == "non_gpu"


@pytest.mark.parametrize("charge_type", ["yearly", None, ""])
async def test_no_gpu_requires_payg_before_mutation(charge_type):
    client = FakeClient()
    client.charge_type = charge_type
    jobs = Jobs(client)
    result = await completed(jobs, await jobs.start("target", no_gpu=True))
    assert result["phase"] == "failed"
    assert result["error"]["kind"] == "state"
    assert "按量计费" in result["error"]["message"]
    assert client.starts == 0


@pytest.mark.parametrize("no_gpu", [False, True])
async def test_different_active_modes_conflict(no_gpu):
    client = FakeClient()
    client.hold_start = asyncio.Event()
    jobs = Jobs(client)
    try:
        job = await jobs.start("target", no_gpu=no_gpu)
        await client.start_entered.wait()
        with pytest.raises(ValueError, match="不同开机模式"):
            await jobs.start("target", no_gpu=not no_gpu)
        assert len(jobs.jobs) == 1
        assert jobs.get(job["job_id"])["no_gpu"] is no_gpu
        assert client.starts == 1
    finally:
        await jobs.close()


@pytest.mark.parametrize("no_gpu", [False, True])
@pytest.mark.parametrize("state", ["running", "starting", "restarting"])
async def test_existing_different_mode_never_mutates(no_gpu, state):
    client = FakeClient()
    client.state = state
    client.start_mode = "gpu" if no_gpu else "non_gpu"
    jobs = Jobs(client)
    result = await completed(jobs, await jobs.start("target", no_gpu=no_gpu))
    assert result["phase"] == "failed"
    assert result["error"]["kind"] == "state"
    assert client.starts == client.stops == 0


@pytest.mark.parametrize("no_gpu", [False, True])
async def test_existing_matching_mode_succeeds_without_mutation(no_gpu):
    client = FakeClient()
    client.state = "running"
    client.start_mode = "non_gpu" if no_gpu else None
    jobs = Jobs(client)
    result = await completed(jobs, await jobs.start("target", no_gpu=no_gpu))
    assert result["phase"] == "succeeded"
    assert client.starts == 0


async def test_no_gpu_unknown_running_mode_never_succeeds_or_mutates(clock):
    client = FakeClient()
    client.state = "running"
    client.start_mode = None
    jobs = Jobs(client)
    result = await completed(jobs, await jobs.start("target", no_gpu=True))
    assert result["phase"] == "failed"
    assert result["error"]["kind"] == "timeout"
    assert client.starts == 0


@pytest.mark.parametrize("mode", [None, "gpu"])
@pytest.mark.parametrize("ambiguous", [False, True])
async def test_no_gpu_confirmation_requires_mode_without_resending(clock, mode, ambiguous):
    client = FakeClient()

    async def start(uuid, *, no_gpu=False):
        assert no_gpu
        client.starts += 1
        client.state = "running"
        client.start_mode = mode
        if ambiguous:
            raise AutoDLError("network", "请求超时", ambiguous=True)

    client.power_on = start
    jobs = Jobs(client)
    result = await completed(jobs, await jobs.start("target", no_gpu=True))
    assert result["phase"] == "failed"
    assert result["error"]["kind"] == ("uncertain" if ambiguous else "timeout")
    assert result["error"]["ambiguous"] is ambiguous
    assert client.starts == 1


@pytest.mark.parametrize("ambiguous", [False, True])
async def test_no_gpu_confirmation_can_wait_for_mode(clock, ambiguous):
    client = FakeClient()
    reads = 0

    async def start(uuid, *, no_gpu=False):
        assert no_gpu
        client.starts += 1
        client.state = "running"
        if ambiguous:
            raise AutoDLError("network", "请求超时", ambiguous=True)

    async def status(uuid, *, include_hardware=True):
        assert include_hardware is False
        nonlocal reads
        reads += 1
        return {
            "status": client.state,
            "charge_type": "payg",
            "start_mode": "non_gpu" if reads >= 4 else None,
        }

    client.power_on = start
    client.status = status
    jobs = Jobs(client)
    result = await completed(jobs, await jobs.start("target", no_gpu=True))
    assert result["phase"] == "succeeded"
    assert reads == 4
    assert client.starts == 1
    assert result["error"] is None


@pytest.mark.parametrize("failed", [False, True])
async def test_wait_returns_terminal_snapshot(failed):
    client = FakeClient()
    client.hold_start = asyncio.Event()
    if failed:
        client.start_error = AutoDLError("business", "拒绝")
    jobs = Jobs(client)
    job = await jobs.start("target")
    await client.start_entered.wait()
    waiter = asyncio.create_task(jobs.wait(job["job_id"]))
    client.hold_start.set()
    result = await asyncio.wait_for(waiter, 1)
    assert result["phase"] == ("failed" if failed else "succeeded")
    assert await jobs.wait(job["job_id"]) == result
    await jobs.close()


async def test_wait_timeout_and_cancelled_waiter_do_not_cancel_job():
    client = FakeClient()
    client.hold_start = asyncio.Event()
    jobs = Jobs(client)
    job = await jobs.start("target")
    await client.start_entered.wait()
    job_id = job["job_id"]
    before = jobs.get(job_id)
    for timeout in (0, 0.001):
        current = await jobs.wait(job_id, timeout)
        assert current.pop("elapsed_seconds") >= before["elapsed_seconds"]
        assert current == {k: v for k, v in before.items() if k != "elapsed_seconds"}
    waiter = asyncio.create_task(jobs.wait(job_id))
    other = asyncio.create_task(jobs.wait(job_id))
    # Schedule a turn so both waiters enter asyncio.wait before cancelling one.
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not jobs.jobs[job_id].task.done()
    assert client.starts == 1
    client.hold_start.set()
    result = await asyncio.wait_for(other, 1)
    assert result["phase"] == "succeeded"
    assert client.starts == 1
    await jobs.close()


@pytest.mark.parametrize("action", ["cancel", "close"])
@pytest.mark.parametrize("before_run", [False, True])
async def test_wait_job_cancellation(action, before_run):
    client = FakeClient()
    client.hold_start = asyncio.Event()
    jobs = Jobs(client)
    job = await jobs.start("target")
    if not before_run:
        await client.start_entered.wait()
    waiter = asyncio.create_task(jobs.wait(job["job_id"]))
    if action == "cancel":
        await jobs.cancel(job["job_id"])
    else:
        await jobs.close()
    result = await asyncio.wait_for(waiter, 1)
    assert result["phase"] == "cancelled"
    assert result["finished_at"] is not None
    await jobs.close()


async def test_wait_retains_job_after_eviction():
    client = FakeClient()
    client.hold_start = asyncio.Event()
    jobs = Jobs(client)
    job = await jobs.start("target")
    await client.start_entered.wait()
    waiter = asyncio.create_task(jobs.wait(job["job_id"]))
    await asyncio.sleep(0)
    # Simulate registry eviction while the waiter holds its reference.
    del jobs.jobs[job["job_id"]]
    client.hold_start.set()
    assert (await asyncio.wait_for(waiter, 1))["phase"] == "succeeded"
    await jobs.close()


@pytest.mark.parametrize(
    "timeout_value",
    [-1, 301, True, False, None, "1", float("nan"), float("inf"), -float("inf"), 10**400],
)
async def test_wait_rejects_invalid_timeout(timeout_value):
    jobs = Jobs(FakeClient())
    with pytest.raises(ValueError, match="timeout_seconds"):
        await jobs.wait("missing", timeout_value)


async def test_wait_missing_id():
    jobs = Jobs(FakeClient())
    with pytest.raises(ValueError, match="当前服务进程"):
        await jobs.wait("missing")


async def test_capacity_notification_wakes_all_waiters_without_cancelling_job():
    client = FakeClient(retry_interval=60, retry_max_interval=60)
    client.capacity_failures = 100
    client.hold_start = asyncio.Event()
    jobs = Jobs(client)
    job = await jobs.start("target")
    await client.start_entered.wait()
    waiters = [asyncio.create_task(jobs.wait(job["job_id"], 120)) for _ in range(3)]
    await asyncio.sleep(0)
    client.hold_start.set()
    results = await asyncio.wait_for(asyncio.gather(*waiters), 1)
    for result in results:
        assert result["phase"] == "waiting_capacity"
        assert result["attempts"] == 1
        assert 0 < result["next_retry_in_seconds"] <= 60
        assert result["elapsed_seconds"] >= 0
    assert client.starts == 1
    assert not jobs.jobs[job["job_id"]].task.done()
    assert (await asyncio.wait_for(jobs.wait(job["job_id"], 120), 1))["phase"] == "waiting_capacity"
    terminal_waiter = asyncio.create_task(jobs.wait(job["job_id"], 120, return_on_capacity=False))
    await asyncio.sleep(0)
    assert not terminal_waiter.done()
    await jobs.cancel(job["job_id"])
    terminal = await terminal_waiter
    assert terminal["phase"] == "cancelled"
    assert await jobs.wait(job["job_id"]) == terminal
    await jobs.close()


@pytest.mark.parametrize("target", ["gpu", "non_gpu"])
async def test_switch_modes_order_and_identity(target):
    client = FakeClient()
    client.state = "running"
    client.start_mode = "non_gpu" if target == "gpu" else "gpu"
    events = []
    status, off, on = client.status, client.power_off, client.power_on

    async def observe(uuid, **kwargs):
        result = await status(uuid, **kwargs)
        events.append(result["status"])
        return result

    async def shutdown(uuid):
        events.append("off")
        await off(uuid)

    async def startup(uuid, **kwargs):
        events.append("on")
        assert "shutdown" in events[events.index("off") + 1 :]
        await on(uuid, **kwargs)

    client.status, client.power_off, client.power_on = observe, shutdown, startup
    jobs = Jobs(client)
    job = await jobs.switch("target", target)
    assert (await jobs.switch("target", target))["job_id"] == job["job_id"]
    with pytest.raises(ValueError):
        await jobs.start("target", no_gpu=target == "non_gpu")
    with pytest.raises(ValueError):
        await jobs.switch("target", "non_gpu" if target == "gpu" else "gpu")
    result = await completed(jobs, job)
    assert result["phase"] == "succeeded"
    assert result["operation"] == "switch"
    assert result["target_mode"] == target
    assert client.stops == client.starts == 1
    assert not jobs.shutdown_pending
    await jobs.close()


@pytest.mark.parametrize("target", ["gpu", "non_gpu"])
@pytest.mark.parametrize("state", ["running", "shutdown", "shutting_down"])
async def test_switch_noop_or_already_stopping(target, state):
    client = FakeClient()
    client.start_mode = target
    client.state = state
    original_status = client.status
    reads = 0

    async def status(uuid, **kwargs):
        nonlocal reads
        reads += 1
        if state == "shutting_down" and reads > 1 and client.starts == 0:
            client.state = "shutdown"
        return await original_status(uuid, **kwargs)

    client.status = status
    jobs = Jobs(client)
    result = await completed(jobs, await jobs.switch("target", target))
    assert result["phase"] == "succeeded"
    assert client.stops == 0
    assert client.starts == (0 if state == "running" else 1)
    await jobs.close()


async def test_switch_rejects_ineligible_or_unknown_state_before_shutdown():
    for state, charge_type in [("running", "subscription"), ("unknown", "payg")]:
        client = FakeClient()
        client.state, client.charge_type = state, charge_type
        jobs = Jobs(client)
        result = await completed(jobs, await jobs.switch("target", "non_gpu"))
        assert result["phase"] == "failed"
        assert result["error"]["kind"] == "state"
        assert client.stops == client.starts == 0
        await jobs.close()
    with pytest.raises(ValueError, match="target_mode"):
        await Jobs(FakeClient()).switch("target", "invalid")


@pytest.mark.parametrize("confirms", [False, True])
async def test_switch_ambiguous_shutdown_observes_without_resending(clock, confirms):
    client = FakeClient(stop_timeout=1, poll_interval=0.1)
    client.state = "running"

    async def power_off(uuid):
        client.stops += 1
        if confirms:
            client.state = "shutdown"
        raise AutoDLError("network", "请求超时", ambiguous=True)

    client.power_off = power_off
    jobs = Jobs(client)
    result = await completed(jobs, await jobs.switch("target", "non_gpu"))
    assert client.stops == 1
    assert client.starts == (1 if confirms else 0)
    assert result["phase"] == ("succeeded" if confirms else "failed")
    if not confirms:
        assert result["error"]["kind"] == "uncertain"
        assert result["error"]["ambiguous"]
        assert "target" in jobs.shutdown_pending
    await jobs.close()


async def test_switch_shutdown_timeout_never_starts(clock):
    client = FakeClient(stop_timeout=1, poll_interval=0.1)
    client.state = "running"

    async def power_off(uuid):
        client.stops += 1
        client.state = "shutting_down"

    client.power_off = power_off
    jobs = Jobs(client)
    result = await completed(jobs, await jobs.switch("target", "non_gpu"))
    assert result["error"]["kind"] == "timeout"
    assert client.stops == 1
    assert client.starts == 0
    with pytest.raises(ValueError, match="关机尚未确认"):
        await jobs.start("target")
    await jobs.close()


@pytest.mark.parametrize("action", ["cancel", "stop", "close"])
async def test_switch_cancel_during_shutdown_prevents_restart(action):
    client = FakeClient()
    client.state = "running"
    entered, release = asyncio.Event(), asyncio.Event()

    async def power_off(uuid):
        client.stops += 1
        client.state = "shutting_down"
        entered.set()
        await release.wait()

    client.power_off = power_off
    jobs = Jobs(client)
    job = await jobs.switch("target", "non_gpu")
    await entered.wait()
    if action == "cancel":
        await jobs.cancel(job["job_id"])
    elif action == "stop":
        await jobs.stop("target")
    else:
        await jobs.close()
    release.set()
    assert jobs.get(job["job_id"])["phase"] == "cancelled"
    assert client.starts == 0
    assert client.stops == 1
    assert "target" in jobs.shutdown_pending
    await jobs.close()


async def test_switch_gpu_capacity_reuses_switch_job():
    client = FakeClient(retry_interval=60, retry_max_interval=60)
    client.state, client.start_mode = "running", "non_gpu"
    client.capacity_failures = 1
    jobs = Jobs(client)
    job = await jobs.switch("target", "gpu")
    result = await asyncio.wait_for(jobs.wait(job["job_id"], 120), 1)
    assert result["phase"] == "waiting_capacity"
    assert result["job_id"] == job["job_id"]
    assert result["operation"] == "switch"
    assert client.stops == client.starts == 1
    await jobs.stop("target")
    assert jobs.get(job["job_id"])["phase"] == "cancelled"
    assert client.starts == 1
    await jobs.close()


@pytest.mark.parametrize("during", ["mutation", "observation"])
async def test_switch_late_ambiguous_shutdown_preserves_uncertain_result(clock, during):
    client = FakeClient(stop_timeout=1)
    client.state = "running"
    original_status = client.status
    reads = 0

    async def status(uuid, **kwargs):
        nonlocal reads
        reads += 1
        if reads > 1 and during == "observation":
            clock.now += 2
            raise AutoDLError("network", "读取超时")
        return await original_status(uuid, **kwargs)

    async def power_off(uuid):
        client.stops += 1
        if during == "mutation":
            clock.now += 2
        raise AutoDLError("network", "关机响应超时", ambiguous=True)

    client.status, client.power_off = status, power_off
    jobs = Jobs(client)
    result = await completed(jobs, await jobs.switch("target", "non_gpu"))
    assert result["error"]["kind"] == "uncertain"
    assert result["error"]["ambiguous"]
    assert client.starts == 0
    assert client.stops == 1
    await jobs.close()


async def test_switch_shutdown_read_retries_honor_retry_after(clock):
    client = FakeClient(stop_timeout=10)
    client.state = "running"
    original_status = client.status
    reads = 0

    async def status(uuid, **kwargs):
        nonlocal reads
        reads += 1
        if reads == 2:
            raise AutoDLError("rate_limit", "限流", retry_after=2)
        return await original_status(uuid, **kwargs)

    client.status = status
    jobs = Jobs(client)
    result = await completed(jobs, await jobs.switch("target", "non_gpu"))
    assert result["phase"] == "succeeded"
    assert clock.sleeps[0] == 2
    assert client.stops == client.starts == 1
    await jobs.close()


@pytest.mark.parametrize("no_gpu", [False, True])
async def test_switch_cancel_before_run_never_mutates(no_gpu):
    client = FakeClient()
    client.state = "running"
    jobs = Jobs(client)
    job = await jobs.switch("target", "non_gpu" if no_gpu else "gpu")
    result = await jobs.cancel(job["job_id"])
    assert result["phase"] == "cancelled"
    assert client.starts == client.stops == 0
    assert result["elapsed_seconds"] >= 0
    await jobs.close()


async def test_switch_stop_during_start_confirmation_prevents_future_start():
    client = FakeClient()
    client.state = "running"
    client.auto_running = False
    jobs = Jobs(client)
    job = await jobs.switch("target", "non_gpu")
    await client.start_entered.wait()
    assert jobs.get(job["job_id"])["phase"] == "confirming"
    await jobs.stop("target")
    assert jobs.get(job["job_id"])["phase"] == "cancelled"
    assert client.starts == 1
    assert client.stops == 2
    assert client.state == "shutdown"
    await jobs.close()


@pytest.mark.parametrize("value", [None, 0, 1, "true"])
async def test_wait_rejects_nonboolean_capacity_flag(value):
    with pytest.raises(ValueError, match="return_on_capacity"):
        await Jobs(FakeClient()).wait("missing", return_on_capacity=value)


@pytest.mark.parametrize("phase", ["waiting_capacity", "waiting_network", "confirming"])
async def test_retry_countdown_uses_monotonic_clock(monkeypatch, phase):
    now = 100.0
    monkeypatch.setattr(jobs_module.time, "monotonic", lambda: now)
    job = Job("target")
    jobs = Jobs(FakeClient())
    assert job.snapshot()["next_retry_in_seconds"] is None

    async def sleep(delay):
        nonlocal now
        assert delay == 2.5  # Clamped to the overall task deadline.
        assert job.snapshot()["next_retry_in_seconds"] == 3
        now += 1.5
        assert job.snapshot()["next_retry_in_seconds"] == 1
        now += 1
        assert job.snapshot()["next_retry_in_seconds"] == 0
        now += 1
        assert job.snapshot()["next_retry_in_seconds"] == 0

    monkeypatch.setattr(jobs_module.asyncio, "sleep", sleep)
    await jobs._wait(job, phase, 10, deadline=102.5)
    assert job.snapshot()["next_retry_in_seconds"] is None


@pytest.mark.parametrize("phase", ["succeeded", "failed", "cancelled"])
def test_finish_clears_retry_countdown(phase):
    jobs = Jobs(FakeClient())
    job = Job("target")
    job.next_retry_monotonic = jobs_module.time.monotonic() + 60
    jobs._finish(job, phase)
    assert job.snapshot()["next_retry_in_seconds"] is None
