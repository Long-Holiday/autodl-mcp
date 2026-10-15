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
        assert set(result) == {
            "instance_uuid",
            "status",
            "sub_status",
            "start_mode",
            "charge_type",
            "observed_at",
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
        assert (await client.status("target"))["status"] == "running"
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
        result = await client.status("target")
        assert result["start_mode"] == expected
        assert result["charge_type"] == expected
    finally:
        await client.close()
