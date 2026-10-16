import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# 非敏感配置放在 config.yaml（随代码提交）；token 只从环境变量读取，
# 由 MCP 客户端配置的 env 块传入，避免密钥进入版本库。
CONFIG_ENV_VAR = "AUTODL_CONFIG"
DEFAULT_CONFIG_NAME = "config.yaml"

# 必须为正数的字段；max_wait_seconds 允许为 0，retry_jitter 另有 [0, 1) 检查。
_NUMBER_FIELDS = (
    "retry_interval",
    "retry_max_interval",
    "transient_retry_interval",
    "transient_retry_max_interval",
    "poll_interval",
    "start_timeout",
    "request_timeout",
    "max_wait_seconds",
)
_PARSE_NUMBER_FIELDS = ("retry_jitter", *_NUMBER_FIELDS)
_SEQUENCE_FIELDS = ("instance_allowlist", "capacity_codes")
_FILE_FIELDS = frozenset((*_PARSE_NUMBER_FIELDS, *_SEQUENCE_FIELDS))


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    instance_allowlist: frozenset[str] = frozenset()
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
            raise ValueError(
                "缺少 AutoDL 控制台登录 Token：请在 agent 的 MCP 配置里为 autodl 服务设置环境变量 "
                "AUTODL_TOKEN，或在 shell 中 export AUTODL_TOKEN="
                "<控制台登录 Token 原文，不含 Bearer 前缀>"
            )
        for name in _NUMBER_FIELDS:
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
        if self.instance_allowlist and instance_uuid not in self.instance_allowlist:
            raise ValueError("实例不在 instance_allowlist（AUTODL_INSTANCE_ALLOWLIST）中")

    @classmethod
    def load(cls, config_path: Path | None = None) -> "Settings":
        """读取 config.yaml，再让同名的 AUTODL_* 环境变量覆盖其中的字段。"""
        values = _read_config_file(config_path)
        values["token"] = os.getenv("AUTODL_TOKEN", "").strip()
        for name in _PARSE_NUMBER_FIELDS:
            if (raw := os.getenv(f"AUTODL_{name.upper()}")) is not None:
                values[name] = _parse_number(name, raw)
        for name in _SEQUENCE_FIELDS:
            if (raw := os.getenv(f"AUTODL_{name.upper()}")) is not None:
                values[name] = _parse_sequence(name, raw)
        return cls(**values)


def _config_path(config_path: Path | None) -> Path | None:
    """显式指定的配置文件必须存在；默认的 ./config.yaml 不存在时跳过。"""
    if config_path is not None:
        path = config_path
    elif explicit := os.getenv(CONFIG_ENV_VAR):
        path = Path(explicit).expanduser()
    else:
        default = Path.cwd() / DEFAULT_CONFIG_NAME
        return default if default.is_file() else None
    if not path.is_file():
        raise ValueError(f"找不到配置文件：{path}")
    return path


def config_source() -> Path | None:
    """本次启动会读取的配置文件；返回 None 表示没找到文件、全部走内置默认值。"""
    return _config_path(None)


def _read_config_file(config_path: Path | None) -> dict[str, Any]:
    path = _config_path(config_path)
    if path is None:
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError:
        raise ValueError(f"无法读取配置文件：{path}") from None
    except yaml.YAMLError as exc:
        raise ValueError(f"配置文件不是有效的 YAML：{path}（{exc}）") from None
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"配置文件顶层必须是键值映射：{path}")
    return _parse_config_file(path, raw)


def _parse_config_file(path: Path, raw: dict[Any, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key, value in raw.items():
        if key == "token":
            raise ValueError(
                f"{path} 中不能写入 token，请在 MCP 客户端配置的 env 中设置 AUTODL_TOKEN"
            )
        if key not in _FILE_FIELDS:
            raise ValueError(
                f"{path} 中不支持的配置项 {key!r}，可用的配置项：{'、'.join(sorted(_FILE_FIELDS))}"
            )
        if value is None:  # 留空的配置项走默认值
            continue
        values[key] = (
            _parse_sequence(key, value) if key in _SEQUENCE_FIELDS else _parse_number(key, value)
        )
    return values


def _parse_number(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是数字")
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} 必须是数字") from None


def _parse_sequence(name: str, value: Any) -> frozenset[str]:
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, (list, tuple)) or any(
        isinstance(v, (list, tuple, dict)) for v in value
    ):
        raise ValueError(f"{name} 必须是字符串列表，例如 [uuid-a, uuid-b]")
    return frozenset(filter(None, (str(v).strip() for v in value)))
