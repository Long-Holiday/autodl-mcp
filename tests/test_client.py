import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx2 as httpx
import pytest

from autodl_mcp.client import AutoDLClient, AutoDLError, parse_retry_after
from autodl_mcp.config import Settings


async def test_pagination_and_secret_redaction():
    seen = []

    def handler(request):
        if request.method == "GET":
            assert request.url.path == "/api/v1/instance/snapshot"
            assert dict(request.url.params) == {"instance_uuid": "target"}
            return httpx.Response(
                200,
                json={
                    "code": "Success",
                    "data": {
                        "snapshot_gpu_alias_name": "RTX 3090",
                        "machine_info_snapshot": {
                            "gpu_type": {"memory": 24 * 1024**3},
                            "cpu_used": 14,
                        },
                        "root_password": "sensitive",
                        "ssh_command": "secret ssh",
                    },
                },
            )
        body = json.loads(request.content)
        seen.append(body)
        assert request.headers["Authorization"] == "secret-token"
        rows = (
            [{"uuid": "other", "status": "running"}] * 100
            if body["page_index"] == 1
            else [
                {
                    "uuid": "target",
                    "status": "shutdown",
                    "start_mode": "non_gpu",
                    "charge_type": "payg",
                    "root_password": "sensitive",
                    "ssh_command": "secret ssh",
                    "name": "private account",
                }
            ]
        )
        return httpx.Response(
            200,
            json={
                "code": "Success",
                "data": {
                    "list": rows,
                    "result_total": 101,
                },
            },
        )

    client = AutoDLClient(Settings(token="secret-token"), httpx.MockTransport(handler))
    try:
        result = await client.status("target")
        assert result["status"] == "shutdown"
        assert len(seen) == 2
        assert seen[0]["status"] == []
        assert result["start_mode"] == "non_gpu"
        assert result["charge_type"] == "payg"
        assert result["gpu_name"] == "RTX 3090"
        assert result["gpu_memory_gb"] == 24
        assert result["cpu_cores"] == 14
        assert set(result) == {
            "instance_uuid",
            "status",
            "sub_status",
            "start_mode",
            "charge_type",
            "observed_at",
            "gpu_name",
            "gpu_memory_gb",
            "cpu_cores",
        }
        assert "sensitive" not in str(result)
    finally:
        await client.close()


@pytest.mark.parametrize(
    "operation,path",
    [
        ("power_on", "/api/v1/instance/power_on"),
        ("power_off", "/api/v1/instance/power_off"),
    ],
)
async def test_mutation_contract(operation, path):
    def handler(request):
        assert request.method == "POST"
        assert request.url.path == path
        assert json.loads(request.content) == {"instance_uuid": "abc-123"}
        return httpx.Response(200, json={"code": "Success", "data": None})

    client = AutoDLClient(Settings(token="test"), httpx.MockTransport(handler))
    try:
        await getattr(client, operation)("abc-123")
    finally:
        await client.close()


@pytest.mark.parametrize(
    "code,message,kind",
    [
        ("Error", "当前无空闲机器，请稍后重试", "capacity"),
        ("Error", "空闲 GPU 不足", "capacity"),
        ("ConfirmedNoGPU", "", "capacity"),
        ("AuthorizeFailed", "无空闲机器", "auth"),
        ("TORecharge", "余额不足", "business"),
        ("TORealName", "需实名", "business"),
        ("Error", "资源错误 secret-token", "business"),
    ],
)
async def test_capacity_is_narrow_and_terminal_errors_win(code, message, kind):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"code": code, "msg": message},
        )
    )
    client = AutoDLClient(
        Settings(token="secret-token", capacity_codes=frozenset({"ConfirmedNoGPU"})),
        transport,
    )
    try:
        with pytest.raises(AutoDLError) as exc:
            await client.power_on("test")
        assert exc.value.kind == kind
        assert "secret-token" not in str(exc.value)
    finally:
        await client.close()


@pytest.mark.parametrize(
    "status,kind,ambiguous",
    [
        (401, "auth", False),
        (403, "auth", False),
        (429, "rate_limit", True),
        (500, "network", True),
        (302, "http", True),
    ],
)
@pytest.mark.parametrize("no_gpu", [False, True])
async def test_http_failures(status, kind, ambiguous, no_gpu):
    client = AutoDLClient(
        Settings(token="test"),
        httpx.MockTransport(
            lambda request: httpx.Response(status),
        ),
    )
    try:
        with pytest.raises(AutoDLError) as exc:
            await client.power_on("test", no_gpu=no_gpu)
        assert exc.value.kind == kind
        assert exc.value.ambiguous == ambiguous
    finally:
        await client.close()


@pytest.mark.parametrize("no_gpu", [False, True])
async def test_network_mutation_is_ambiguous(no_gpu):
    def handler(request):
        raise httpx.ReadTimeout("credential must not leak", request=request)

    client = AutoDLClient(Settings(token="test"), httpx.MockTransport(handler))
    try:
        with pytest.raises(AutoDLError) as exc:
            await client.power_on("test", no_gpu=no_gpu)
        assert exc.value.ambiguous
        assert "credential" not in str(exc.value)
    finally:
        await client.close()


@pytest.mark.parametrize(
    "data", [None, {}, {"list": [], "result_total": "1"}, {"list": [{}], "result_total": -1}]
)
async def test_bad_list(data):
    client = AutoDLClient(
        Settings(token="test"),
        httpx.MockTransport(
            lambda request: httpx.Response(200, json={"code": "Success", "data": data}),
        ),
    )
    try:
        with pytest.raises(AutoDLError, match="结构"):
            await client.status("target")
    finally:
        await client.close()


async def test_not_found():
    client = AutoDLClient(
        Settings(token="test"),
        httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "code": "Success",
                    "data": {
                        "list": [],
                        "result_total": 0,
                    },
                },
            ),
        ),
    )
    try:
        with pytest.raises(AutoDLError) as exc:
            await client.status("missing")
        assert exc.value.kind == "not_found"
    finally:
        await client.close()


async def test_server_can_cap_requested_page_size():
    def handler(request):
        page = json.loads(request.content)["page_index"]
        rows = [{"uuid": "other" if page == 1 else "target", "status": "running"}]
        return httpx.Response(
            200,
            json={
                "code": "Success",
                "data": {
                    "list": rows,
                    "result_total": 2,
                },
            },
        )

    client = AutoDLClient(Settings(token="test"), httpx.MockTransport(handler))
    try:
        assert (await client.status("target", include_hardware=False))["status"] == "running"
    finally:
        await client.close()


@pytest.mark.parametrize("payload", ["not json", "[]", '{"data": null}'])
@pytest.mark.parametrize("no_gpu", [False, True])
async def test_invalid_response_is_ambiguous_for_mutations(payload, no_gpu):
    client = AutoDLClient(
        Settings(token="test"),
        httpx.MockTransport(
            lambda request: httpx.Response(200, text=payload),
        ),
    )
    try:
        with pytest.raises(AutoDLError) as exc:
            await client.power_on("target", no_gpu=no_gpu)
        assert exc.value.kind == "protocol"
        assert exc.value.ambiguous
    finally:
        await client.close()


@pytest.mark.parametrize(
    "value,expected",
    [
        ("120", 120),
        (" 0 ", 0),
        (None, None),
        ("", None),
        ("bad header", None),
        ("-1", None),
        ("nan", None),
        ("inf", None),
        ("1.5", None),
        ("9" * 400, None),
    ],
)
def test_parse_retry_after_seconds(value, expected):
    assert parse_retry_after(value) == expected


def test_parse_retry_after_http_date():
    future = format_datetime(datetime.now(UTC) + timedelta(seconds=120), usegmt=True)
    assert 118 <= parse_retry_after(future) <= 120
    past = format_datetime(datetime.now(UTC) - timedelta(seconds=120), usegmt=True)
    assert parse_retry_after(past) == 0


@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize("no_gpu", [False, True])
async def test_retry_after_is_exposed_safely(status, no_gpu):
    client = AutoDLClient(
        Settings(token="test"),
        httpx.MockTransport(
            lambda request: httpx.Response(status, headers={"Retry-After": "1200"}),
        ),
    )
    try:
        with pytest.raises(AutoDLError) as exc:
            await client.power_on("target", no_gpu=no_gpu)
        assert exc.value.retry_after == 1200
        assert exc.value.as_dict()["retry_after"] == 1200
        assert exc.value.ambiguous
    finally:
        await client.close()


async def test_no_gpu_mutation_contract():
    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/api/v1/instance/power_on"
        assert json.loads(request.content) == {"instance_uuid": "abc-123", "payload": "non_gpu"}
        return httpx.Response(200, json={"code": "Success", "data": None})

    client = AutoDLClient(Settings(token="test"), httpx.MockTransport(handler))
    try:
        await client.power_on("abc-123", no_gpu=True)
    finally:
        await client.close()


@pytest.mark.parametrize("code", ["Error", "ConfirmedNoGPU", "WrongStartMode"])
async def test_no_gpu_never_retries_gpu_capacity_errors(code):
    client = AutoDLClient(
        Settings(token="test", capacity_codes=frozenset({"ConfirmedNoGPU"})),
        httpx.MockTransport(
            lambda request: httpx.Response(200, json={"code": code, "msg": "无空闲GPU secret"})
        ),
    )
    try:
        with pytest.raises(AutoDLError) as exc:
            await client.power_on("target", no_gpu=True)
        assert exc.value.kind == "business"
        assert not exc.value.ambiguous
        assert "secret" not in str(exc.value)
    finally:
        await client.close()


@pytest.mark.parametrize("value,expected", [(None, None), (123, None), ({}, None), ("gpu", "gpu")])
async def test_status_mode_fields_are_optional_strings(value, expected):
    client = AutoDLClient(
        Settings(token="test"),
        httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "code": "Success",
                    "data": {
                        "list": [
                            {
                                "uuid": "target",
                                "status": "running",
                                "start_mode": value,
                                "charge_type": value,
                            }
                        ],
                        "result_total": 1,
                    },
                },
            )
        ),
    )
    try:
        result = await client.status("target", include_hardware=False)
        assert result["start_mode"] == expected
        assert result["charge_type"] == expected
    finally:
        await client.close()


@pytest.mark.parametrize(
    "name,memory,cores,expected",
    [
        ("A100", 80 * 1024**3, 32, ("A100", 80, 32)),
        ("RTX 3090", 24 * 1024**3, 14, ("RTX 3090", 24, 14)),
        (None, None, None, (None, None, None)),
        ({}, "24GB", "14", (None, None, None)),
        (" ", True, True, (None, None, None)),
        (123, -1, -1, (None, None, None)),
        ("A100", 0, 0, ("A100", 0, 0)),
        ("A100", 1024**3, 1.5, ("A100", 1, None)),
    ],
)
async def test_status_hardware_projection(name, memory, cores, expected):
    seen = []

    def handler(request):
        seen.append(request.method)
        if request.method == "POST":
            data = {"list": [{"uuid": "target", "status": "shutdown"}], "result_total": 1}
        else:
            data = {
                "snapshot_gpu_alias_name": name,
                "machine_info_snapshot": {"gpu_type": {"memory": memory}, "cpu_used": cores},
                "root_password": "private-password",
                "jupyter_token": "private-token",
            }
        return httpx.Response(200, json={"code": "Success", "data": data})

    client = AutoDLClient(Settings(token="test"), httpx.MockTransport(handler))
    try:
        result = await client.status("target")
        assert tuple(result[k] for k in ("gpu_name", "gpu_memory_gb", "cpu_cores")) == expected
        assert "private-" not in str(result)
        assert seen == ["POST", "GET"]
        seen.clear()
        result = await client.status("target", include_hardware=False)
        assert "gpu_name" not in result
        assert seen == ["POST"]
    finally:
        await client.close()


@pytest.mark.parametrize(
    "data", [{}, {"machine_info_snapshot": None}, {"machine_info_snapshot": {"gpu_type": []}}]
)
async def test_missing_hardware_fields(data):
    client = AutoDLClient(
        Settings(token="test"),
        httpx.MockTransport(
            lambda request: httpx.Response(200, json={"code": "Success", "data": data})
        ),
    )
    try:
        assert await client._hardware("target") == {
            "gpu_name": None,
            "gpu_memory_gb": None,
            "cpu_cores": None,
        }
    finally:
        await client.close()


@pytest.mark.parametrize(
    "status,kind",
    [(401, "auth"), (403, "auth"), (429, "rate_limit"), (503, "network"), (302, "http")],
)
async def test_snapshot_http_failures_are_safe(status, kind):
    client = AutoDLClient(
        Settings(token="test"),
        httpx.MockTransport(lambda request: httpx.Response(status, text="private-credential")),
    )
    try:
        with pytest.raises(AutoDLError) as exc:
            await client._hardware("target")
        assert exc.value.kind == kind
        assert not exc.value.ambiguous
        assert "private-credential" not in str(exc.value)
    finally:
        await client.close()


@pytest.mark.parametrize(
    "payload,kind",
    [
        ("not-json", "protocol"),
        ("[]", "protocol"),
        ('{"code":"Success","data":null}', "protocol"),
        ('{"code":"Error","msg":"private-credential"}', "business"),
        ('{"code":"AuthorizeFailed","msg":"private-credential"}', "auth"),
    ],
)
async def test_snapshot_protocol_and_business_errors_are_safe(payload, kind):
    client = AutoDLClient(
        Settings(token="test"),
        httpx.MockTransport(lambda request: httpx.Response(200, text=payload)),
    )
    try:
        with pytest.raises(AutoDLError) as exc:
            await client._hardware("target")
        assert exc.value.kind == kind
        assert not exc.value.ambiguous
        assert "private-credential" not in str(exc.value)
    finally:
        await client.close()


@pytest.mark.parametrize("memory", [float("nan"), float("inf"), -float("inf"), 10**400])
async def test_invalid_numeric_hardware(memory, monkeypatch):
    client = AutoDLClient(Settings(token="test"))

    async def request(*args, **kwargs):
        return {"machine_info_snapshot": {"gpu_type": {"memory": memory}}}

    monkeypatch.setattr(client, "_request", request)
    try:
        assert (await client._hardware("target"))["gpu_memory_gb"] is None
    finally:
        await client.close()


async def test_snapshot_transport_error_is_safe():
    def handler(request):
        raise httpx.ReadTimeout("private-credential", request=request)

    client = AutoDLClient(Settings(token="test"), httpx.MockTransport(handler))
    try:
        with pytest.raises(AutoDLError) as exc:
            await client._hardware("target")
        assert exc.value.kind == "network"
        assert not exc.value.ambiguous
        assert "private-credential" not in str(exc.value)
    finally:
        await client.close()


async def test_batch_shared_pagination_order_partial_errors_and_redaction():
    pages, snapshots = [], []

    def handler(request):
        if request.method == "GET":
            uuid = request.url.params["instance_uuid"]
            snapshots.append(uuid)
            if uuid == "bad-hardware":
                return httpx.Response(200, json={"code": "Error", "msg": "private-secret"})
            return httpx.Response(
                200,
                json={
                    "code": "Success",
                    "data": {
                        "snapshot_gpu_alias_name": "RTX 3090",
                        "machine_info_snapshot": {
                            "gpu_type": {"memory": 24 * 1024**3},
                            "cpu_used": 8,
                        },
                        "root_password": "private-secret",
                    },
                },
            )
        page = json.loads(request.content)["page_index"]
        pages.append(page)
        # Server caps pages at one row, irrespective of requested page_size.
        uuid = ["first", "other", "bad-hardware"][page - 1]
        return httpx.Response(
            200,
            json={
                "code": "Success",
                "data": {
                    "list": [
                        {
                            "uuid": uuid,
                            "status": "running",
                            "price": "unverified",
                            "ssh_command": "private-secret",
                        }
                    ],
                    "result_total": 3,
                },
            },
        )

    client = AutoDLClient(Settings(token="test"), httpx.MockTransport(handler))
    try:
        result = await client.list_instances(["bad-hardware", "first", "first", "missing"])
        assert pages == [1, 2, 3]
        assert snapshots == ["bad-hardware", "first"]
        assert [r["instance_uuid"] for r in result["instances"]] == ["bad-hardware", "first"]
        assert [e["error"]["kind"] for e in result["errors"]] == ["business", "not_found"]
        assert result["instances"][0]["status"] == "running"
        assert result["instances"][0]["gpu_name"] is None
        assert result["instances"][1]["gpu_memory_gb"] == 24
        assert all(r["price"] is None for r in result["instances"])
        assert all(r["price_unavailable_reason"] for r in result["instances"])
        assert "private-secret" not in str(result)
        assert "unverified" not in str(result)
    finally:
        await client.close()


@pytest.mark.parametrize(
    "uuids",
    [[], ["allowed", "other"], ["allowed", "../bad"], ["allowed", 1], ["allowed"] * 101, "allowed"],
)
async def test_batch_prevalidation_never_accesses_network(uuids):
    def handler(request):
        pytest.fail("Invalid batch must not access network")

    client = AutoDLClient(
        Settings(token="test", instance_allowlist=frozenset({"allowed"})),
        httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ValueError):
            await client.list_instances(uuids)
    finally:
        await client.close()


async def test_batch_hardware_concurrency_is_bounded():
    import asyncio

    active, peak = 0, 0
    release = asyncio.Event()
    full = asyncio.Event()
    uuids = [f"target-{n}" for n in range(12)]

    async def handler(request):
        nonlocal active, peak
        if request.method != "GET":
            return httpx.Response(
                200,
                json={
                    "code": "Success",
                    "data": {
                        "list": [{"uuid": u, "status": "shutdown"} for u in uuids],
                        "result_total": len(uuids),
                    },
                },
            )
        active += 1
        peak = max(peak, active)
        if active == 5:
            full.set()
        await release.wait()
        active -= 1
        return httpx.Response(200, json={"code": "Success", "data": {}})

    client = AutoDLClient(Settings(token="test"), httpx.MockTransport(handler))
    try:
        task = asyncio.create_task(client.list_instances(uuids))
        await asyncio.wait_for(full.wait(), 1)
        assert peak == 5
        release.set()
        result = await task
        assert len(result["instances"]) == 12
        assert peak == 5
    finally:
        release.set()
        await client.close()


async def test_batch_does_not_return_allowlist_excluded_rows():
    def handler(request):
        if request.method == "GET":
            assert request.url.params["instance_uuid"] == "allowed"
            return httpx.Response(200, json={"code": "Success", "data": {}})
        return httpx.Response(
            200,
            json={
                "code": "Success",
                "data": {
                    "list": [
                        {"uuid": "excluded", "status": "running"},
                        {"uuid": ["malformed"], "status": "running"},
                        {"uuid": "allowed", "status": "shutdown"},
                    ],
                    "result_total": 3,
                },
            },
        )

    client = AutoDLClient(
        Settings(token="test", instance_allowlist=frozenset({"allowed"})),
        httpx.MockTransport(handler),
    )
    try:
        result = await client.list_instances(["allowed"])
        assert len(result["instances"]) == 1
        assert "excluded" not in str(result)
    finally:
        await client.close()
