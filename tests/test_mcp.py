import asyncio
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

    def handler(request):
        body = json.loads(request.content)
        if request.url.path == "/api/v1/instance":
            return httpx.Response(
                200,
                json={
                    "code": "Success",
                    "data": {
                        "list": [
                            {"uuid": uuid, "status": status, "root_password": "secret"}
                            for uuid, status in state.items()
                        ],
                        "result_total": 2,
                    },
                },
            )
        uuid = body["instance_uuid"]
        if request.url.path.endswith("power_on"):
            if uuid == "waiting":
                return httpx.Response(200, json={"code": "Error", "msg": "无空闲机器"})
            state[uuid] = "running"
        else:
            state[uuid] = "shutdown"
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
            "power_off",
            "instance_status",
            "job_status",
            "cancel_job",
        }
        assert next(t for t in tools if t.name == "power_off").annotations.destructive_hint
        status = await session.call_tool("instance_status", {"instance_uuid": "target"})
        assert not status.is_error
        assert status.structured_content["status"] == "shutdown"
        assert "secret" not in str(status)
        started = await session.call_tool("power_on", {"instance_uuid": "target"})
        assert not started.is_error
        job_id = started.structured_content["job_id"]
        for _ in range(30):
            job = await session.call_tool("job_status", {"job_id": job_id})
            if job.structured_content["phase"] == "succeeded":
                break
            await asyncio.sleep(0.01)
        assert job.structured_content["phase"] == "succeeded"
        stopped = await session.call_tool("power_off", {"instance_uuid": "target"})
        assert stopped.structured_content["accepted"]
        status = await session.call_tool("instance_status", {"instance_uuid": "target"})
        assert status.structured_content["status"] == "shutdown"
        waiting = await session.call_tool("power_on", {"instance_uuid": "waiting"})
        cancel = await session.call_tool(
            "cancel_job",
            {
                "job_id": waiting.structured_content["job_id"],
            },
        )
        assert cancel.structured_content["phase"] == "cancelled"
        missing = await session.call_tool("job_status", {"job_id": "missing"})
        assert missing.is_error


async def test_real_entrypoint_discovery_without_remote_requests():
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "autodl_mcp"],
        env={"AUTODL_TOKEN": "test-only-do-not-call"},
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        assert len((await session.list_tools()).tools) == 5


if __name__ == "__main__":
    run_mock_server()
