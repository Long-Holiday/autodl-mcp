import json
import sys
from pathlib import Path

import httpx2 as httpx
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from autodl_mcp.config import Settings
from autodl_mcp.server import create_server

MOCK_SERVER_PATH = str(Path(__file__).resolve())


def run_mock_server():
    state = {"target": "shutdown", "waiting": "shutdown"}
    modes = {}

    def handler(request):
        if request.url.path == "/api/v1/instance/snapshot":
            assert request.method == "GET"
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
                        "root_password": "secret",
                    },
                },
            )
        body = json.loads(request.content)
        if request.url.path == "/api/v1/instance":
            return httpx.Response(
                200,
                json={
                    "code": "Success",
                    "data": {
                        "list": [
                            {
                                "uuid": uuid,
                                "status": status,
                                "root_password": "secret",
                                "charge_type": "payg",
                                "start_mode": modes.get(uuid),
                            }
                            for uuid, status in state.items()
                        ],
                        "result_total": 2,
                    },
                },
            )
        uuid = body["instance_uuid"]
        if request.url.path == "/api/v1/instance/power_on":
            if uuid == "waiting":
                return httpx.Response(200, json={"code": "Error", "msg": "无空闲机器"})
            state[uuid] = "running"
            modes[uuid] = "non_gpu" if body.get("payload") == "non_gpu" else "gpu"
        elif request.url.path == "/api/v1/instance/power_off":
            state[uuid] = "shutdown"
        else:
            raise AssertionError(f"Unexpected request: {request.url.path}")
        return httpx.Response(200, json={"code": "Success", "data": None})

    create_server(
        Settings(token="mock-only", retry_interval=1, retry_max_interval=1),
        httpx.MockTransport(handler),
    ).run(transport="stdio")


async def test_stdio_protocol_and_all_tools():
    params = StdioServerParameters(command=sys.executable, args=[MOCK_SERVER_PATH])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        assert init.server_info.name == "autodl"
        tools = (await session.list_tools()).tools
        assert {t.name for t in tools} == {
            "power_on",
            "power_on_no_gpu",
            "power_off",
            "instance_status",
            "list_instances",
            "switch_mode",
            "job_status",
            "wait_for_job",
            "cancel_job",
        }
        assert next(t for t in tools if t.name == "power_off").annotations.destructive_hint
        wait_tool = next(t for t in tools if t.name == "wait_for_job")
        assert wait_tool.annotations.read_only_hint
        assert not wait_tool.annotations.destructive_hint
        timeout_schema = wait_tool.input_schema["properties"]["timeout_seconds"]
        assert timeout_schema["default"] == 15
        assert timeout_schema["minimum"] == 0
        assert timeout_schema["maximum"] == 300
        status = await session.call_tool("instance_status", {"instance_uuid": "target"})
        assert not status.is_error
        assert status.structured_content["status"] == "shutdown"
        assert status.structured_content["gpu_name"] == "RTX 3090"
        assert status.structured_content["gpu_memory_gb"] == 24
        assert status.structured_content["cpu_cores"] == 14
        assert "secret" not in str(status)
        batch_tool = next(t for t in tools if t.name == "list_instances")
        assert batch_tool.annotations.read_only_hint
        switch_tool = next(t for t in tools if t.name == "switch_mode")
        assert switch_tool.annotations.destructive_hint
        assert not switch_tool.annotations.read_only_hint
        assert wait_tool.input_schema["properties"]["return_on_capacity"]["default"] is True
        batch = await session.call_tool("list_instances", {"instance_uuids": ["target", "waiting"]})
        assert not batch.is_error
        assert len(batch.structured_content["instances"]) == 2
        assert not batch.structured_content["errors"]
        assert "secret" not in str(batch)
        started = await session.call_tool("power_on", {"instance_uuid": "target"})
        assert not started.is_error
        job_id = started.structured_content["job_id"]
        job = await session.call_tool("wait_for_job", {"job_id": job_id})
        assert job.structured_content["phase"] == "succeeded"
        stopped = await session.call_tool("power_off", {"instance_uuid": "target"})
        assert stopped.structured_content["accepted"]
        status = await session.call_tool("instance_status", {"instance_uuid": "target"})
        assert status.structured_content["status"] == "shutdown"
        no_gpu = await session.call_tool("power_on_no_gpu", {"instance_uuid": "target"})
        assert not no_gpu.is_error
        assert no_gpu.structured_content["no_gpu"] is True
        job = await session.call_tool(
            "wait_for_job", {"job_id": no_gpu.structured_content["job_id"]}
        )
        assert job.structured_content["phase"] == "succeeded"
        status = await session.call_tool("instance_status", {"instance_uuid": "target"})
        assert status.structured_content["start_mode"] == "non_gpu"
        assert "secret" not in str(status)
        switched = await session.call_tool("switch_mode", {"instance_uuid": "target"})
        assert not switched.is_error
        switched_job = await session.call_tool(
            "wait_for_job", {"job_id": switched.structured_content["job_id"]}
        )
        assert switched_job.structured_content["phase"] == "succeeded"
        assert switched_job.structured_content["operation"] == "switch"
        assert switched_job.structured_content["last_status"] == "running"
        stopped = await session.call_tool("power_off", {"instance_uuid": "target"})
        assert stopped.structured_content["accepted"]
        waiting = await session.call_tool("power_on", {"instance_uuid": "waiting"})
        pending = await session.call_tool(
            "wait_for_job",
            {
                "job_id": waiting.structured_content["job_id"],
                "timeout_seconds": 120,
            },
        )
        assert not pending.is_error
        assert pending.structured_content["phase"] == "waiting_capacity"
        remaining = pending.structured_content["suggested_next_poll_seconds"]
        assert type(remaining) is int
        assert 0 <= remaining <= 1
        immediate = await session.call_tool(
            "job_status",
            {
                "job_id": waiting.structured_content["job_id"],
            },
        )
        assert (
            immediate.structured_content["elapsed_seconds"]
            >= pending.structured_content["elapsed_seconds"]
        )
        assert immediate.structured_content["attempts"] == pending.structured_content["attempts"]
        for invalid in (-1, 301, True, "1"):
            invalid_result = await session.call_tool(
                "wait_for_job",
                {
                    "job_id": waiting.structured_content["job_id"],
                    "timeout_seconds": invalid,
                },
            )
            assert invalid_result.is_error
        cancel = await session.call_tool(
            "cancel_job",
            {
                "job_id": waiting.structured_content["job_id"],
            },
        )
        assert cancel.structured_content["phase"] == "cancelled"
        assert cancel.structured_content["suggested_next_poll_seconds"] is None
        missing = await session.call_tool("job_status", {"job_id": "missing"})
        assert missing.is_error
        missing = await session.call_tool("wait_for_job", {"job_id": "missing"})
        assert missing.is_error


async def test_real_entrypoint_discovery_without_remote_requests():
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "autodl_mcp"],
        env={"AUTODL_TOKEN": "test-only-do-not-call"},
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        assert len((await session.list_tools()).tools) == 9


if __name__ == "__main__":
    run_mock_server()
