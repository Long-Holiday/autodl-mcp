import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import httpx2 as httpx
from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from autodl_mcp.client import AutoDLClient
from autodl_mcp.config import Settings, config_source
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
            "普通开机任务在后台等待空闲 GPU，无卡模式开机不申请 GPU，仅支持按量计费实例。"
            "通过 wait_for_job 等待任务、job_status 即时查询、cancel_job 取消。"
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
        description=(
            "无卡模式开机（仍会计费，无 GPU）；仅支持按量计费实例，立即返回后台任务 ID。"
            "不会自动将已运行的 GPU 实例切换为无卡模式，请先关机并确认状态。"
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
    )
    async def power_on_no_gpu(instance_uuid: str) -> dict[str, Any]:
        return await jobs().start(instance_uuid, no_gpu=True)

    @server.tool(
        description="关机（中断实例上的运行任务）；先取消开机重试，返回接受结果而非最终状态。",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True),
    )
    async def power_off(instance_uuid: str) -> dict[str, Any]:
        return await jobs().stop(instance_uuid)

    @server.tool(
        description=(
            "查询指定 UUID 的实例状态与配置规格：GPU 型号、单卡显存 GB（1024³ 字节）、"
            "分配的 CPU 核心数；无卡模式不代表 GPU 已挂载。不返回密码或 SSH 凭证。"
        ),
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
        description=(
            "只读等待当前进程的开机任务结束，或等待超时后返回当前 job_status 快照。"
            "timeout_seconds 为 0–300 秒（默认 300，0 立即返回）；活动 phase 表示尚未结束。"
            "本次等待超时或断开不会取消后台开机任务；任务不跨服务重启恢复。"
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )
    async def wait_for_job(
        job_id: str, timeout_seconds: Annotated[float, Field(ge=0, le=300, strict=True)] = 300
    ) -> dict[str, Any]:
        return await jobs().wait(job_id, timeout_seconds)

    @server.tool(
        description="取消开机任务的后续尝试，不撤销已接受的开机请求，不自动关机。",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
    )
    async def cancel_job(job_id: str) -> dict[str, Any]:
        return await jobs().cancel(job_id)

    return server


def _masked(token: str) -> str:
    """自检输出里只显示 Token 的首尾各 4 位，避免日志泄露完整凭证。"""
    return f"{token[:4]}…{token[-4:]}" if len(token) > 12 else "已设置"


def check() -> None:
    """自检：只验证入口与配置能否加载，不访问网络。用于安装插件后确认启动链路可用。"""
    print("autodl-mcp 自检：入口与依赖正常", flush=True)
    try:
        settings = Settings.load()
        source = config_source()
    except ValueError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        raise SystemExit(2) from None
    print(f"配置文件：{source}" if source else "配置文件：未找到 config.yaml，全部使用内置默认值")
    print(f"AUTODL_TOKEN：{_masked(settings.token)}")
    print(
        "实例白名单："
        + (
            "、".join(sorted(settings.instance_allowlist))
            if settings.instance_allowlist
            else "未限制（允许全部自有实例）"
        )
    )
    print("自检通过；Token 是否有效要等第一次真实请求才能确认。")


def main() -> None:
    if sys.argv[1:2] == ["--check"]:
        check()
        return
    try:
        settings = Settings.load()
    except ValueError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        raise SystemExit(2) from None
    create_server(settings).run(transport="stdio")
