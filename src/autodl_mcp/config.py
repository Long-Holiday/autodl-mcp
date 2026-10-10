import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    allowlist: frozenset[str] = frozenset()
    capacity_codes: frozenset[str] = frozenset()
    retry_interval: float = 60
    retry_max_interval: float = 300
    retry_jitter: float = 0.3
    transient_retry_interval: float = 60
    transient_retry_max_interval: float = 900
    max_wait_seconds: float = 0
    poll_interval: float = 5
    start_timeout: float = 600
    request_timeout: float = 30

    def __post_init__(self) -> None:
        if not self.token or any(c.isspace() for c in self.token):
            raise ValueError("需要有效的 AutoDL 控制台登录 Token（不含 Bearer 前缀）")
        for name in (
            "retry_interval",
            "retry_max_interval",
            "transient_retry_interval",
            "transient_retry_max_interval",
            "poll_interval",
            "start_timeout",
            "request_timeout",
            "max_wait_seconds",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0 or (name != "max_wait_seconds" and value == 0):
                raise ValueError(f"{name} 必须是有效的正数（max_wait_seconds 可为 0）")
        if self.retry_max_interval < self.retry_interval:
            raise ValueError("最大重试间隔不能小于初始重试间隔")
        if self.transient_retry_max_interval < self.transient_retry_interval:
            raise ValueError("最大网络退避间隔不能小于初始网络退避间隔")
        if not math.isfinite(self.retry_jitter) or not 0 <= self.retry_jitter < 1:
            raise ValueError("retry_jitter 必须在 [0, 1) 范围内")

    def validate_uuid(self, instance_uuid: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", instance_uuid):
            raise ValueError("instance_uuid 格式无效，请使用控制台显示的完整实例 UUID")
        if self.allowlist and instance_uuid not in self.allowlist:
            raise ValueError("实例不在 AUTODL_INSTANCE_ALLOWLIST 中")

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(Path.cwd() / ".env", override=False)
        token = os.getenv("AUTODL_TOKEN", "").strip()
        if not token and (filename := os.getenv("AUTODL_TOKEN_FILE")):
            try:
                token = Path(filename).expanduser().read_text(encoding="utf-8").strip()
            except OSError:
                raise ValueError("无法读取 AUTODL_TOKEN_FILE") from None
        values = {}
        for name in (
            "retry_interval",
            "retry_max_interval",
            "retry_jitter",
            "transient_retry_interval",
            "transient_retry_max_interval",
            "max_wait_seconds",
            "poll_interval",
            "start_timeout",
            "request_timeout",
        ):
            if (value := os.getenv(f"AUTODL_{name.upper()}")) is not None:
                try:
                    values[name] = float(value)
                except ValueError:
                    raise ValueError(f"AUTODL_{name.upper()} 必须是数字") from None
        return cls(
            token=token,
            allowlist=frozenset(
                filter(
                    None, (v.strip() for v in os.getenv("AUTODL_INSTANCE_ALLOWLIST", "").split(","))
                )
            ),
            capacity_codes=frozenset(
                filter(None, (v.strip() for v in os.getenv("AUTODL_CAPACITY_CODES", "").split(",")))
            ),
            **values,
        )
