"""把本插件装进各个 agent：调用各家插件生态的原生安装命令，或输出通用兜底配置。

只用标准库。**不会写入 AUTODL_TOKEN**：token 始终由用户在客户端 MCP 配置的 env 里自行设置。
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

PLUGIN_NAME = "autodl-mcp"
MARKETPLACE_NAME = "autodl-mcp"
SERVER_NAME = "autodl"
GIT_SOURCE = "https://github.com/Long-Holiday/autodl-mcp"

# 三个有原生插件生态的 agent；其余客户端一律走 --generic 输出的通用片段。
AGENT_TARGETS = ("antigravity", "claude", "cursor")

TOKEN_HINT = (
    "下一步：在你的 agent 的 MCP 配置里为 autodl 服务设置环境变量 AUTODL_TOKEN\n"
    "（AutoDL 控制台登录 Token 原文，不要加 Bearer 前缀），然后运行：\n"
    "    autodl-mcp --check"
)


def plugin_root() -> Path | None:
    """源码检出时返回仓库根（插件根）；作为已安装工具运行时返回 None。"""
    root = Path(__file__).resolve().parents[2]
    return root if (root / "plugin.json").is_file() else None


def default_source() -> str:
    """默认安装源：优先用当前检出，其次回落到远端仓库。"""
    root = plugin_root()
    return str(root) if root else GIT_SOURCE


def _log(message: str = "") -> None:
    """人看的输出一律走 stderr，stdout 只留给机器可读的片段（--generic）。"""
    print(message, file=sys.stderr)


def _run(command: list[str], *, dry_run: bool) -> bool:
    _log("  $ " + " ".join(command))
    if dry_run:
        return True
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        _log(f"  命令失败（退出码 {result.returncode}）")
        return False
    return True


def _manual_steps(agent: str, source: str) -> str:
    if agent == "antigravity":
        return (
            "  手动安装（Antigravity CLI / IDE）：\n"
            f"    agy plugin install {source}\n"
            "    agy plugin list          # 确认已装\n"
            "    agy plugin validate .    # 可选：校验插件\n"
            f"    agy plugin uninstall {PLUGIN_NAME}   # 卸载"
        )
    if agent == "claude":
        return (
            "  手动安装（Claude Code）：\n"
            f"    claude plugin marketplace add {source}\n"
            f"    claude plugin install {PLUGIN_NAME}@{MARKETPLACE_NAME}\n"
            "  或在会话里：/plugin marketplace add "
            f"{source} 然后 /plugin install {PLUGIN_NAME}@{MARKETPLACE_NAME}"
        )
    return (
        "  手动安装（Cursor）：\n"
        "    在 Cursor 里用 /add-plugin，或从插件市场搜索 autodl-mcp；\n"
        "    MCP 服务按下面 --generic 输出的片段写进 .cursor/mcp.json（项目级）"
        "或 ~/.cursor/mcp.json（全局）。"
    )


def install_agent(agent: str, source: str, *, dry_run: bool, command: str, scope: str) -> bool:
    """安装单个 agent；探测不到 CLI 时只打印手动步骤。"""
    _log(f"\n[{agent}]")
    if agent == "cursor":
        return install_cursor(dry_run=dry_run, command=command, scope=scope)
    if agent == "antigravity":
        cli, commands = "agy", [["agy", "plugin", "install", source]]
    else:
        cli = "claude"
        commands = [
            ["claude", "plugin", "marketplace", "add", source],
            ["claude", "plugin", "install", f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"],
        ]

    if shutil.which(cli) is None:
        _log(f"  没找到 {cli} 命令，跳过自动安装。")
        _log(_manual_steps(agent, source))
        return False
    ok = all(_run(cmd, dry_run=dry_run) for cmd in commands)
    if not ok:
        _log(_manual_steps(agent, source))
    return ok


def _mcp_entry(command: str) -> dict[str, Any]:
    """无 token 的 stdio 服务条目；env 留空由用户自己填 AUTODL_TOKEN。"""
    if command == "path":
        return {"command": "autodl-mcp"}
    root = plugin_root()
    if root is None:
        raise SystemExit("以已安装工具运行时，请用 --command path（先执行 uv tool install .）。")
    return {"command": "uv", "args": ["run", "--directory", str(root), "autodl-mcp"]}


def _write_mcp_config(path: Path, entry: dict[str, Any], *, dry_run: bool) -> None:
    """读改写，保留文件里其它 MCP 服务；内容不是合法 JSON 时拒绝覆盖。"""
    servers: dict[str, Any] = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise SystemExit(
                f"{path} 不是合法 JSON，已中止以免覆盖你的配置，请手动编辑。"
            ) from None
        if isinstance(existing, dict) and isinstance(existing.get("mcpServers"), dict):
            servers = existing["mcpServers"]
        servers[SERVER_NAME] = entry
        payload = {**existing, "mcpServers": servers}
    else:
        payload = {"mcpServers": {SERVER_NAME: entry}}
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if dry_run:
        _log(f"  将写入 {path}：\n{text}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _log(f"  已写入 {path}")


def install_cursor(*, dry_run: bool, command: str, scope: str) -> bool:
    """Cursor 没有插件安装 CLI：写 MCP 配置，插件本体在应用内装。"""
    if scope == "project":
        path = Path.cwd() / ".cursor" / "mcp.json"
    else:
        path = Path.home() / ".cursor" / "mcp.json"
    _write_mcp_config(path, _mcp_entry(command), dry_run=dry_run)
    _log(
        "  Cursor 插件本体请在应用内安装：/add-plugin autodl-mcp，"
        "或从插件市场搜索 autodl-mcp。\n"
        f"  卸载 MCP 服务：删掉 {path} 里的 mcpServers.autodl。"
    )
    return True


def generic_snippet(fmt: str, command: str) -> str:
    """给没有插件机制的客户端用的通用片段。"""
    entry = _mcp_entry(command)
    if fmt == "toml":
        args = ", ".join(json.dumps(a) for a in entry.get("args", []))
        lines = [f"[mcp_servers.{SERVER_NAME}]", f"command = {json.dumps(entry['command'])}"]
        if args:
            lines.append(f"args = [{args}]")
        return "\n".join(lines) + "\n"
    return json.dumps({"mcpServers": {SERVER_NAME: entry}}, ensure_ascii=False, indent=2) + "\n"


def detect(agent: str) -> str:
    cli = {"antigravity": "agy", "claude": "claude", "cursor": "cursor"}[agent]
    path = shutil.which(cli)
    if path is None:
        return "未安装"
    version = ""
    try:
        result = subprocess.run(
            [cli, "--version"], check=False, capture_output=True, text=True, timeout=15
        )
        version = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    except (OSError, subprocess.SubprocessError):
        pass
    return f"{path}" + (f"（{version}）" if version else "")


def list_agents() -> None:
    _log("支持的 agent：")
    for agent in AGENT_TARGETS:
        _log(f"  {agent:<12} {detect(agent)}")
    _log("\n其余客户端（Windsurf、Cline、Roo、Zed、opencode、Continue、Kiro…）用：")
    _log("  autodl-mcp-install --generic --format json   # 或 --format toml（Codex 风格）")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="autodl-mcp-install",
        description="把 autodl-mcp 插件装进各个 agent（不写入 AUTODL_TOKEN）",
    )
    parser.add_argument("--list", action="store_true", help="列出支持的 agent 及本机安装情况")
    parser.add_argument(
        "--agent",
        action="append",
        choices=AGENT_TARGETS,
        help="要安装的 agent，可重复；不指定时配合 --generic 使用",
    )
    parser.add_argument("--generic", action="store_true", help="输出通用 MCP 配置片段")
    parser.add_argument("--format", choices=("json", "toml"), default="json", help="片段格式")
    parser.add_argument(
        "--command",
        choices=("uv", "path"),
        default="uv",
        help=(
            "配置里怎么拉起服务：uv 走本仓库（默认），"
            "path 走 PATH 上的 autodl-mcp（需先 uv tool install .）"
        ),
    )
    parser.add_argument(
        "--scope",
        choices=("global", "project"),
        default="global",
        help="Cursor 写哪个层级的配置",
    )
    parser.add_argument("--source", help=f"安装源（路径或 git URL），默认 {GIT_SOURCE} 或当前检出")
    parser.add_argument(
        "--from-git", action="store_true", help=f"强制用远端仓库安装（{GIT_SOURCE}）"
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印将执行的命令和将写入的内容")
    args = parser.parse_args()

    if args.list:
        list_agents()
        return

    source = GIT_SOURCE if args.from_git else (args.source or default_source())

    if args.generic:
        sys.stdout.write(generic_snippet(args.format, args.command))
    elif not args.agent:
        parser.error("请指定 --agent，或用 --generic / --list")

    if args.agent:
        _log(f"安装源：{source}" + ("（dry-run，不会真正执行）" if args.dry_run else ""))
        failed = [
            a
            for a in args.agent
            if not install_agent(
                a, source, dry_run=args.dry_run, command=args.command, scope=args.scope
            )
        ]
        if failed:
            _log(f"\n以下 agent 未能自动完成：{'、'.join(failed)}")

    _log(f"\n{TOKEN_HINT}")


if __name__ == "__main__":
    main()
