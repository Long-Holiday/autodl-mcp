"""安装器：命令构造、配置合并、以及「绝不写 token」。"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from autodl_mcp import install

ROOT = Path(__file__).resolve().parents[1]


def test_default_source_is_this_checkout():
    assert install.default_source() == str(ROOT)


def test_generic_json_uses_repo_path_by_default():
    snippet = json.loads(install.generic_snippet("json", "uv"))
    server = snippet["mcpServers"]["autodl"]
    assert server == {"command": "uv", "args": ["run", "--directory", str(ROOT), "autodl-mcp"]}
    assert "AUTODL_TOKEN" not in json.dumps(snippet), "片段里不能夹带 token"


def test_generic_json_path_mode():
    snippet = json.loads(install.generic_snippet("json", "path"))
    assert snippet["mcpServers"]["autodl"] == {"command": "autodl-mcp"}


def test_generic_toml_is_codex_shaped():
    text = install.generic_snippet("toml", "uv")
    assert "[mcp_servers.autodl]" in text
    assert 'command = "uv"' in text
    assert f'"{ROOT}"' in text


def test_cursor_writes_global_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    install.install_cursor(dry_run=False, command="path", scope="global")
    written = json.loads((tmp_path / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    assert written == {"mcpServers": {"autodl": {"command": "autodl-mcp"}}}
    assert "AUTODL_TOKEN" not in json.dumps(written)


def test_cursor_project_scope_follows_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    install.install_cursor(dry_run=False, command="path", scope="project")
    assert (tmp_path / ".cursor" / "mcp.json").is_file()


def test_cursor_merges_without_dropping_other_servers(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    config = tmp_path / ".cursor" / "mcp.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({"mcpServers": {"other": {"command": "other"}}, "extra": 1}),
        encoding="utf-8",
    )
    install.install_cursor(dry_run=False, command="path", scope="global")
    written = json.loads(config.read_text(encoding="utf-8"))
    assert written["mcpServers"]["other"] == {"command": "other"}
    assert written["mcpServers"]["autodl"] == {"command": "autodl-mcp"}
    assert written["extra"] == 1, "用户配置文件里的其它键必须保留"


def test_cursor_refuses_to_clobber_invalid_json(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    config = tmp_path / ".cursor" / "mcp.json"
    config.parent.mkdir(parents=True)
    config.write_text("{ 这不是 JSON", encoding="utf-8")
    with pytest.raises(SystemExit, match="不是合法 JSON"):
        install.install_cursor(dry_run=False, command="path", scope="global")
    assert config.read_text(encoding="utf-8") == "{ 这不是 JSON", "拒绝覆盖时不能改动原文件"


def test_cursor_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    install.install_cursor(dry_run=True, command="path", scope="global")
    assert not (tmp_path / ".cursor").exists()
    assert "将写入" in capsys.readouterr().err


def _record(monkeypatch, *, available: set[str]):
    """把 shutil.which 和 subprocess.run 换成假的，记录将会执行的命令。"""
    commands: list[list[str]] = []
    monkeypatch.setattr(
        install.shutil, "which", lambda cli: f"/usr/bin/{cli}" if cli in available else None
    )
    monkeypatch.setattr(
        install.subprocess,
        "run",
        lambda cmd, check: commands.append(cmd) or SimpleNamespace(returncode=0),
    )
    return commands


def test_antigravity_install_command(monkeypatch):
    commands = _record(monkeypatch, available={"agy"})
    assert install.install_agent(
        "antigravity", "/repo", dry_run=False, command="uv", scope="global"
    )
    assert commands == [["agy", "plugin", "install", "/repo"]]


def test_claude_install_commands(monkeypatch):
    commands = _record(monkeypatch, available={"claude"})
    assert install.install_agent("claude", "/repo", dry_run=False, command="uv", scope="global")
    assert commands == [
        ["claude", "plugin", "marketplace", "add", "/repo"],
        ["claude", "plugin", "install", "autodl-mcp@autodl-mcp"],
    ]


def test_install_agent_without_cli_prints_manual_steps(monkeypatch, capsys):
    commands = _record(monkeypatch, available=set())
    assert not install.install_agent(
        "antigravity", "/repo", dry_run=False, command="uv", scope="global"
    )
    assert commands == [], "没装 CLI 时不应该执行任何命令"
    err = capsys.readouterr().err
    assert "手动安装" in err
    assert "agy plugin install /repo" in err


def test_dry_run_executes_nothing(monkeypatch, capsys):
    commands = _record(monkeypatch, available={"agy", "claude"})
    install.install_agent("claude", "/repo", dry_run=True, command="uv", scope="global")
    assert commands == []
    assert "claude plugin marketplace add /repo" in capsys.readouterr().err


def test_main_list_reports_to_stderr(monkeypatch, capsys):
    monkeypatch.setattr(install.sys, "argv", ["autodl-mcp-install", "--list"])
    install.main()
    captured = capsys.readouterr()
    assert "antigravity" in captured.err
    assert captured.out == "", "stdout 只留给 --generic 的机器可读片段"


def test_generic_stdout_is_pure_json(monkeypatch, capsys):
    """--generic 的输出要能直接重定向进文件，不能被提示语污染。"""
    argv = ["autodl-mcp-install", "--generic", "--format", "json"]
    monkeypatch.setattr(install.sys, "argv", argv)
    install.main()
    captured = capsys.readouterr()
    assert json.loads(captured.out)["mcpServers"]["autodl"]["command"] == "uv"
    assert "AUTODL_TOKEN" in captured.err, "token 提示仍然要出现，只是走 stderr"


def test_main_without_target_is_an_error(monkeypatch, capsys):
    monkeypatch.setattr(install.sys, "argv", ["autodl-mcp-install"])
    with pytest.raises(SystemExit):
        install.main()
    assert "--agent" in capsys.readouterr().err


def test_main_always_reminds_about_token(tmp_path, monkeypatch, capsys):
    """无论装哪个 agent，最后都要提醒用户自己设置 AUTODL_TOKEN（且只提醒，不写入）。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(install.sys, "argv", ["autodl-mcp-install", "--agent", "cursor"])
    install.main()
    err = capsys.readouterr().err
    assert "AUTODL_TOKEN" in err
    written = (tmp_path / ".cursor" / "mcp.json").read_text(encoding="utf-8")
    assert "AUTODL_TOKEN" not in written
