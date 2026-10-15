import pytest

from autodl_mcp.config import Settings


@pytest.mark.parametrize(
    "field,value",
    [
        ("retry_interval", 0),
        ("poll_interval", -1),
        ("max_wait_seconds", -1),
        ("start_timeout", float("inf")),
        ("retry_interval", float("nan")),
    ],
)
def test_invalid_settings(field, value):
    with pytest.raises(ValueError):
        Settings(token="test", **{field: value})


@pytest.mark.parametrize("token", ["", "Bearer token", "token\nvalue"])
def test_invalid_token(token):
    with pytest.raises(ValueError):
        Settings(token=token)


def test_jitter_range_and_network_backoff_validation():
    for value in (-1, 1, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="retry_jitter"):
            Settings(token="test", retry_jitter=value)
    for value in (0, 0.99):
        assert Settings(token="test", retry_jitter=value).retry_jitter == value
    with pytest.raises(ValueError, match="网络退避"):
        Settings(token="test", transient_retry_interval=100, transient_retry_max_interval=50)
    with pytest.raises(ValueError):
        Settings(token="test", transient_retry_interval=0)


def test_retry_settings_load_from_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key, value in {
        "AUTODL_TOKEN": "test",
        "AUTODL_RETRY_INTERVAL": "60",
        "AUTODL_RETRY_MAX_INTERVAL": "300",
        "AUTODL_RETRY_JITTER": "0.2",
        "AUTODL_TRANSIENT_RETRY_INTERVAL": "90",
        "AUTODL_TRANSIENT_RETRY_MAX_INTERVAL": "1200",
    }.items():
        monkeypatch.setenv(key, value)
    settings = Settings.load()
    assert settings.retry_jitter == 0.2
    assert settings.transient_retry_interval == 90
    assert settings.transient_retry_max_interval == 1200


def test_token_never_in_repr():
    assert "secret" not in repr(Settings(token="secret"))


def test_allowlist():
    settings = Settings(token="test", instance_allowlist=frozenset({"target"}))
    settings.validate_uuid("target")
    with pytest.raises(ValueError, match="instance_allowlist"):
        settings.validate_uuid("other")
    with pytest.raises(ValueError, match="格式"):
        settings.validate_uuid("../target")


def test_config_yaml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(
        "instance_allowlist: [foo, bar]\n"
        "capacity_codes: ['ConfirmedNoGPU']\n"
        "request_timeout: 12\n"
        "max_wait_seconds: 0\n"
        "retry_jitter: 0.2\n"
    )
    monkeypatch.setenv("AUTODL_TOKEN", "env-secret")
    settings = Settings.load()
    assert settings.token == "env-secret"
    assert settings.instance_allowlist == {"foo", "bar"}
    assert settings.capacity_codes == {"ConfirmedNoGPU"}
    assert settings.request_timeout == 12
    assert settings.max_wait_seconds == 0
    assert settings.retry_jitter == 0.2
    assert settings.poll_interval == 5  # 未写入文件的配置项走默认值


def test_empty_config_values_fall_back_to_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("instance_allowlist:\nrequest_timeout:\n")
    monkeypatch.setenv("AUTODL_TOKEN", "test")
    settings = Settings.load()
    assert settings.instance_allowlist == frozenset()
    assert settings.request_timeout == 30


def test_env_overrides_config_yaml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("request_timeout: 12\ninstance_allowlist: [foo]\n")
    monkeypatch.setenv("AUTODL_TOKEN", "test")
    monkeypatch.setenv("AUTODL_REQUEST_TIMEOUT", "99")
    monkeypatch.setenv("AUTODL_INSTANCE_ALLOWLIST", "bar, baz")
    settings = Settings.load()
    assert settings.request_timeout == 99
    assert settings.instance_allowlist == {"bar", "baz"}


def test_config_path_option(tmp_path, monkeypatch):
    custom = tmp_path / "custom.yaml"
    custom.write_text("poll_interval: 3\n")
    monkeypatch.setenv("AUTODL_TOKEN", "test")
    assert Settings.load(custom).poll_interval == 3
    with pytest.raises(ValueError, match="找不到配置文件"):
        Settings.load(tmp_path / "missing.yaml")


def test_autodl_config_env_var(tmp_path, monkeypatch):
    custom = tmp_path / "custom.yaml"
    custom.write_text("poll_interval: 3\n")
    monkeypatch.setenv("AUTODL_TOKEN", "test")
    monkeypatch.setenv("AUTODL_CONFIG", str(custom))
    assert Settings.load().poll_interval == 3
    monkeypatch.setenv("AUTODL_CONFIG", str(tmp_path / "missing.yaml"))
    with pytest.raises(ValueError, match="找不到配置文件"):
        Settings.load()


def test_missing_config_file_uses_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTODL_TOKEN", "test")
    assert Settings.load().request_timeout == 30


@pytest.mark.parametrize(
    "text,match",
    [
        ("request_timeout: abc\n", "必须是数字"),
        ("instance_allowlist: {a: b}\n", "字符串列表"),
        ("instance_allowlist:\n  - [nested]\n", "字符串列表"),
        ("timeouts: 5\n", "不支持的配置项"),
        ("instance_allowlist: [foo\n", "YAML"),
        ("- foo\n- bar\n", "键值映射"),
    ],
)
def test_invalid_config_file(tmp_path, monkeypatch, text, match):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(text)
    monkeypatch.setenv("AUTODL_TOKEN", "test")
    with pytest.raises(ValueError, match=match):
        Settings.load()


def test_token_rejected_in_config_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("token: leaked\n")
    monkeypatch.setenv("AUTODL_TOKEN", "test")
    with pytest.raises(ValueError, match="AUTODL_TOKEN"):
        Settings.load()


def test_missing_token(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AUTODL_TOKEN", raising=False)
    with pytest.raises(ValueError, match="Token"):
        Settings.load()
