"""安装器：Antigravity（agy）命令构造，以及「绝不写 token、绝不写配置文件」。"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from autodl_mcp import install

ROOT = Path(__file__).resolve().parents[1]


def test_default_source_is_this_checkout():
    assert install.default_source() == str(ROOT)


def _record(monkeypatch, *, agy_available: bool):
    """把 shutil.which 和 subprocess.run 换成假的，记录将会执行的命令。"""
    commands: list[list[str]] = []
    monkeypatch.setattr(
        install.shutil, "which", lambda cli: f"/usr/bin/{cli}" if agy_available else None
    )
    monkeypatch.setattr(
        install.subprocess,
        "run",
        lambda cmd, check: commands.append(cmd) or SimpleNamespace(returncode=0),
    )
    return commands


def test_install_command(monkeypatch):
    commands = _record(monkeypatch, agy_available=True)
    assert install.install_agent("/repo", dry_run=False)
    assert commands == [["agy", "plugin", "install", "/repo"]]


def test_install_without_cli_prints_manual_steps(monkeypatch, capsys):
    commands = _record(monkeypatch, agy_available=False)
    assert not install.install_agent("/repo", dry_run=False)
    assert commands == [], "没装 agy 时不应该执行任何命令"
    err = capsys.readouterr().err
    assert "手动安装" in err
    assert "agy plugin install /repo" in err


def test_dry_run_executes_nothing(monkeypatch, capsys):
    commands = _record(monkeypatch, agy_available=True)
    install.install_agent("https://github.com/Long-Holiday/autodl-mcp", dry_run=True)
    assert commands == []
    assert "agy plugin install https://github.com/Long-Holiday/autodl-mcp" in (
        capsys.readouterr().err
    )


def test_main_dry_run_uses_this_checkout(monkeypatch, capsys):
    _record(monkeypatch, agy_available=True)
    monkeypatch.setattr(install.sys, "argv", ["autodl-mcp-install", "--dry-run"])
    install.main()
    assert f"agy plugin install {ROOT}" in capsys.readouterr().err


def test_main_from_git_ignores_checkout(monkeypatch, capsys):
    commands = _record(monkeypatch, agy_available=True)
    monkeypatch.setattr(install.sys, "argv", ["autodl-mcp-install", "--from-git"])
    install.main()
    assert commands == [["agy", "plugin", "install", install.GIT_SOURCE]]


def test_main_list_reports_to_stderr(monkeypatch, capsys):
    monkeypatch.setattr(install.sys, "argv", ["autodl-mcp-install", "--list"])
    install.main()
    captured = capsys.readouterr()
    assert "Antigravity" in captured.err
    assert "agy" in captured.err
    assert captured.out == "", "stdout 不给人看的输出"


def test_main_always_reminds_about_token(monkeypatch, capsys):
    """装完之后只提醒用户自己设置 AUTODL_TOKEN，不代写。"""
    _record(monkeypatch, agy_available=True)
    monkeypatch.setattr(install.sys, "argv", ["autodl-mcp-install", "--dry-run"])
    install.main()
    assert "AUTODL_TOKEN" in capsys.readouterr().err


def test_installer_writes_no_files(tmp_path, monkeypatch, capsys):
    """安装器只调 agy，自己不动文件系统——尤其不写 token。"""
    _record(monkeypatch, agy_available=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(install.sys, "argv", ["autodl-mcp-install"])
    install.main()
    assert list(tmp_path.iterdir()) == []
    assert "AUTODL_TOKEN" in capsys.readouterr().err


def test_installer_has_only_the_antigravity_cli():
    source = (ROOT / "src" / "autodl_mcp" / "install.py").read_text(encoding="utf-8")
    assert install.AGENT_CLI == "agy"
    for cli in ("claude", "cursor", "codex"):
        assert cli not in source, f"安装器里不该再出现 {cli}"


@pytest.mark.parametrize("flag", ["--agent", "--generic", "--scope", "--format", "--command"])
def test_removed_flags_are_gone(flag, monkeypatch, capsys):
    """给别的 agent 用的命令行开关已经删掉，别留半截。"""
    monkeypatch.setattr(install.sys, "argv", ["autodl-mcp-install", flag])
    with pytest.raises(SystemExit):
        install.main()
    assert "unrecognized arguments" in capsys.readouterr().err
