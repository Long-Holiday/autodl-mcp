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
    settings = Settings.from_env()
    assert settings.retry_jitter == 0.2
    assert settings.transient_retry_interval == 90
    assert settings.transient_retry_max_interval == 1200


def test_token_never_in_repr():
    assert "secret" not in repr(Settings(token="secret"))


def test_allowlist():
    settings = Settings(token="test", allowlist=frozenset({"target"}))
    settings.validate_uuid("target")
    with pytest.raises(ValueError, match="ALLOWLIST"):
        settings.validate_uuid("other")
    with pytest.raises(ValueError, match="格式"):
        settings.validate_uuid("../target")


def test_dotenv_and_token_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    token_file = tmp_path / "test.token"
    token_file.write_text("file-secret\n")
    (tmp_path / ".env").write_text(
        f"AUTODL_TOKEN_FILE={token_file}\nAUTODL_INSTANCE_ALLOWLIST=foo, bar\n"
    )
    for name in ("AUTODL_TOKEN", "AUTODL_TOKEN_FILE", "AUTODL_INSTANCE_ALLOWLIST"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings.from_env()
    assert settings.token == "file-secret"
    assert settings.allowlist == {"foo", "bar"}
    monkeypatch.setenv("AUTODL_TOKEN", "env-secret")
    assert Settings.from_env().token == "env-secret"
