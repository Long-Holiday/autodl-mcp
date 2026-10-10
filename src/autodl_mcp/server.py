import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2 as httpx
from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from autodl_mcp.client import AutoDLClient
from autodl_mcp.config import Settings
from autodl_mcp.jobs import Jobs


def create_server(
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
) -> MCPServer:
    manager: Jobs | None = None

    @asynccontextmanager
    async def lifespan(server: MCPServer) -> AsyncIterator[None]:
        nonlocal manager
        client = AutoDLClient(settings, transport)
        manager = Jobs(client)
        try:
            yield
        finally:
            await manager.close()
            await client.close()
            manager = None

    server = MCPServer(
        "autodl",
        lifespan=lifespan,
        instructions=(
            "管理普通 AutoDL 容器实例，不适用于 Pro。开机会计费，关机会中断正在运行的任务。"
            "开机任务在后台等待空闲 GPU，通过 job_status 查询、cancel_job 取消。"
            "取消或 MCP 服务退出不会自动关机；任务不跨服务重启恢复。"
        ),
    )

    def jobs() -> Jobs:
        if manager is None:
            raise ValueError("服务尚未启动")
        return manager

    @server.tool(
        description="为指定 UUID 开机（会计费）；缺货后台持续重试，立即返回任务 ID。",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
    )
    async def power_on(instance_uuid: str) -> dict[str, Any]:
        return await jobs().start(instance_uuid)

    @server.tool(
        description="关机（中断实例上的运行任务）；先取消开机重试，返回接受结果而非最终状态。",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True),
    )
    async def power_off(instance_uuid: str) -> dict[str, Any]:
        return await jobs().stop(instance_uuid)

    @server.tool(
        description="查询指定 UUID 的实例当前状态，不返回密码或 SSH 凭证。",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )
    async def instance_status(instance_uuid: str) -> dict[str, Any]:
        return await jobs().client.status(instance_uuid)

    @server.tool(
        description="查询当前进程中的开机任务、尝试次数、重试时间和错误。",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )
    async def job_status(job_id: str) -> dict[str, Any]:
        return jobs().get(job_id)

    @server.tool(
        description="取消开机任务的后续尝试，不撤销已接受的开机请求，不自动关机。",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
    )
    async def cancel_job(job_id: str) -> dict[str, Any]:
        return await jobs().cancel(job_id)

    return server


def main() -> None:
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        raise SystemExit(2) from None
    create_server(settings).run(transport="stdio")
