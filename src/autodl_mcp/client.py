import asyncio
import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx2 as httpx

from autodl_mcp.config import Settings

CAPACITY_MESSAGES = (
    "无空闲机器",
    "没有空闲机器",
    "暂无空闲机器",
    "无空闲GPU",
    "没有空闲GPU",
    "暂无空闲GPU",
    "空闲GPU不足",
    "空闲机器不足",
    "无可用GPU",
    "暂无可用GPU",
)
TERMINAL_CODES = {
    "AuthorizeFailed": "登录凭证失效，请更新控制台 Token",
    "TORecharge": "账户余额不足，请充值后重试",
    "TORealName": "需要完成实名认证",
}


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        if value.strip().isascii() and value.strip().isdigit():
            seconds = float(value)
        else:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=UTC)
            seconds = max(0.0, (date - datetime.now(UTC)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


class AutoDLError(Exception):
    def __init__(
        self,
        kind: str,
        message: str,
        *,
        ambiguous: bool = False,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.kind = kind
        self.ambiguous = ambiguous
        self.retry_after = retry_after

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "message": str(self),
            "ambiguous": self.ambiguous,
            "retry_after": self.retry_after,
        }


class AutoDLClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.http = httpx.AsyncClient(
            base_url="https://www.autodl.com",
            headers={"Authorization": settings.token, "Accept": "application/json"},
            timeout=settings.request_timeout,
            follow_redirects=False,
            transport=transport,
        )

    async def close(self) -> None:
        await self.http.aclose()

    async def _post(self, path: str, body: dict[str, Any], *, mutation: bool = False) -> Any:
        return await self._request("POST", path, body, mutation=mutation)

    async def _request(
        self, method: str, path: str, body: dict[str, Any], *, mutation: bool = False
    ) -> Any:
        try:
            response = await self.http.request(
                method, path, **({"params": body} if method == "GET" else {"json": body})
            )
        except httpx.TransportError:
            raise AutoDLError(
                "network",
                "AutoDL 网络请求失败",
                ambiguous=mutation,
            ) from None
        if response.status_code in {401, 403}:
            raise AutoDLError("auth", "AutoDL 拒绝访问，请检查登录凭证和权限")
        if response.status_code == 429:
            raise AutoDLError(
                "rate_limit",
                "AutoDL 请求限流，请稍后重试",
                ambiguous=mutation,
                retry_after=parse_retry_after(response.headers.get("Retry-After")),
            )
        if response.status_code >= 500:
            raise AutoDLError(
                "network",
                "AutoDL 服务暂时不可用",
                ambiguous=mutation,
                retry_after=parse_retry_after(response.headers.get("Retry-After")),
            )
        if response.status_code != 200:
            raise AutoDLError("http", "AutoDL 返回非预期 HTTP 状态", ambiguous=mutation)
        try:
            result = response.json()
        except ValueError:
            raise AutoDLError("protocol", "AutoDL 返回非 JSON 响应", ambiguous=mutation) from None
        if not isinstance(result, dict) or not isinstance(result.get("code"), str):
            raise AutoDLError("protocol", "AutoDL 响应缺少有效 code", ambiguous=mutation)
        code = result["code"]
        if code == "Success":
            return result.get("data")
        if code in TERMINAL_CODES:
            raise AutoDLError(
                "auth" if code == "AuthorizeFailed" else "business", TERMINAL_CODES[code]
            )
        message = result.get("msg", "")
        normalized = "".join(message.upper().split()) if isinstance(message, str) else ""
        if (
            mutation
            and path.endswith("/power_on")
            and body.get("payload") != "non_gpu"
            and (
                code in self.settings.capacity_codes
                or any(phrase in normalized for phrase in CAPACITY_MESSAGES)
            )
        ):
            raise AutoDLError("capacity", "目标机器无空闲 GPU，等待资源释放")
        # Never expose arbitrary upstream messages: they may contain credentials or account data.
        raise AutoDLError("business", "AutoDL 拒绝操作，请在控制台检查实例、账户及资源状态")

    async def _hardware(self, instance_uuid: str) -> dict[str, Any]:
        data = await self._request(
            "GET", "/api/v1/instance/snapshot", {"instance_uuid": instance_uuid}
        )
        if not isinstance(data, dict):
            raise AutoDLError("protocol", "AutoDL 实例规格结构发生变化")
        machine = data.get("machine_info_snapshot")
        machine = machine if isinstance(machine, dict) else {}
        gpu = machine.get("gpu_type")
        gpu = gpu if isinstance(gpu, dict) else {}
        memory = gpu.get("memory")
        try:
            memory_gb = (
                float(memory) / 1024**3
                if isinstance(memory, (int, float)) and not isinstance(memory, bool) and memory >= 0
                else None
            )
        except OverflowError:
            memory_gb = None
        if memory_gb is not None and (not math.isfinite(memory_gb) or memory_gb < 0):
            memory_gb = None
        cores = machine.get("cpu_used")
        name = data.get("snapshot_gpu_alias_name")
        return {
            "gpu_name": name if isinstance(name, str) and name.strip() else None,
            "gpu_memory_gb": memory_gb,
            "cpu_cores": cores
            if isinstance(cores, int) and not isinstance(cores, bool) and cores >= 0
            else None,
        }

    async def _statuses(self, wanted: set[str]) -> dict[str, dict[str, Any]]:
        found = {}
        page = 1
        observed = 0
        while True:
            data = await self._post(
                "/api/v1/instance",
                {
                    "page_index": page,
                    "page_size": 100,
                    "status": [],
                    "charge_type": [],
                    "sub_name": "",
                    "unbind_sub_user": False,
                },
            )
            if (
                not isinstance(data, dict)
                or not isinstance(data.get("list"), list)
                or not isinstance(data.get("result_total"), int)
                or isinstance(data.get("result_total"), bool)
                or data["result_total"] < 0
            ):
                raise AutoDLError("protocol", "AutoDL 实例列表结构发生变化")
            rows = data["list"]
            for row in rows:
                if not isinstance(row, dict):
                    raise AutoDLError("protocol", "AutoDL 实例记录结构发生变化")
                if isinstance(row.get("uuid"), str) and row["uuid"] in wanted:
                    instance_uuid = row["uuid"]
                    if not isinstance(row.get("status"), str) or not row["status"]:
                        raise AutoDLError("protocol", "AutoDL 实例缺少有效状态")
                    result = {
                        "instance_uuid": instance_uuid,
                        "status": row["status"],
                        "sub_status": row.get("sub_status")
                        if isinstance(
                            row.get("sub_status"),
                            str,
                        )
                        else None,
                        "start_mode": row.get("start_mode")
                        if isinstance(row.get("start_mode"), str)
                        else None,
                        "charge_type": row.get("charge_type")
                        if isinstance(row.get("charge_type"), str)
                        else None,
                        "observed_at": datetime.now(UTC).isoformat(),
                    }
                    found[instance_uuid] = result
            if wanted <= found.keys():
                return found
            observed += len(rows)
            if observed >= data["result_total"] or not rows:
                return found
            page += 1
            if page > 1000:
                raise AutoDLError("protocol", "实例列表分页超出安全上限")

    async def status(self, instance_uuid: str, *, include_hardware: bool = True) -> dict[str, Any]:
        self.settings.validate_uuid(instance_uuid)
        found = await self._statuses({instance_uuid})
        if instance_uuid not in found:
            raise AutoDLError("not_found", "未找到指定 UUID 的实例")
        result = found[instance_uuid]
        if include_hardware:
            result.update(await self._hardware(instance_uuid))
        return result

    async def list_instances(self, instance_uuids: list[str]) -> dict[str, Any]:
        if not isinstance(instance_uuids, list) or not 1 <= len(instance_uuids) <= 100:
            raise ValueError("instance_uuids 必须包含 1 到 100 个 UUID")
        for uuid in instance_uuids:
            if not isinstance(uuid, str):
                raise ValueError("instance_uuid 必须是字符串")
            self.settings.validate_uuid(uuid)
        uuids = list(dict.fromkeys(instance_uuids))
        found = await self._statuses(set(uuids))
        semaphore = asyncio.Semaphore(5)

        async def enrich(uuid: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
            if uuid not in found:
                return None, {
                    "instance_uuid": uuid,
                    "error": AutoDLError("not_found", "未找到指定 UUID 的实例").as_dict(),
                }
            result = found[uuid]
            result.update(price=None, price_unavailable_reason="价格字段及计费单位尚未验证")
            try:
                async with semaphore:
                    result.update(await self._hardware(uuid))
            except AutoDLError as exc:
                result.update(gpu_name=None, gpu_memory_gb=None, cpu_cores=None)
                return result, {"instance_uuid": uuid, "error": exc.as_dict()}
            return result, None

        results = await asyncio.gather(*(enrich(uuid) for uuid in uuids))
        return {
            "instances": [row for row, _ in results if row is not None],
            "errors": [error for _, error in results if error is not None],
        }

    async def power_on(self, instance_uuid: str, *, no_gpu: bool = False) -> None:
        self.settings.validate_uuid(instance_uuid)
        body = {"instance_uuid": instance_uuid}
        if no_gpu:
            body["payload"] = "non_gpu"
        await self._post(
            "/api/v1/instance/power_on",
            body,
            mutation=True,
        )

    async def power_off(self, instance_uuid: str) -> None:
        self.settings.validate_uuid(instance_uuid)
        await self._post(
            "/api/v1/instance/power_off",
            {"instance_uuid": instance_uuid},
            mutation=True,
        )
