"""把本插件装进 Antigravity（`agy`）：调用它原生的插件安装命令。

本插件只面向 Antigravity：清单只有 Agent Plugins 标准那一份（`plugin.json` + `mcp.json`）。
只用标准库。**不会写入 AUTODL_TOKEN**：token 始终由用户在 agy 的 MCP 配置 env 里自行设置。
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

PLUGIN_NAME = "autodl-mcp"
AGENT_CLI = "agy"
GIT_SOURCE = "https://github.com/Long-Holiday/autodl-mcp"

TOKEN_HINT = (
    "下一步：在 Antigravity 里为 autodl 服务设置环境变量 AUTODL_TOKEN\n"
    "（AutoDL 控制台登录 Token 原文，不要加 Bearer 前缀）——从终端启动 agy 时\n"
    "export 一次即可；用 GUI 启动就在 MCP 配置的 env 里给，然后运行：\n"
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
    """人看的输出一律走 stderr，stdout 留给将来可能有的机器可读输出。"""
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


def _manual_steps(source: str) -> str:
    return (
        "  手动安装（Antigravity CLI / IDE）：\n"
        f"    {AGENT_CLI} plugin install {source}\n"
        f"    {AGENT_CLI} plugin list          # 确认已装\n"
        f"    {AGENT_CLI} plugin validate .    # 可选：校验插件\n"
        f"    {AGENT_CLI} plugin uninstall {PLUGIN_NAME}   # 卸载"
    )


def install_agent(source: str, *, dry_run: bool) -> bool:
    """装进 Antigravity；探测不到 agy 时只打印手动步骤。"""
    if shutil.which(AGENT_CLI) is None:
        _log(f"  没找到 {AGENT_CLI} 命令，跳过自动安装。")
        _log(_manual_steps(source))
        return False
    ok = _run([AGENT_CLI, "plugin", "install", source], dry_run=dry_run)
    if not ok:
        _log(_manual_steps(source))
    return ok


def detect() -> str:
    path = shutil.which(AGENT_CLI)
    if path is None:
        return "未安装"
    version = ""
    try:
        result = subprocess.run(
            [AGENT_CLI, "--version"], check=False, capture_output=True, text=True, timeout=15
        )
        version = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    except (OSError, subprocess.SubprocessError):
        pass
    return f"{path}" + (f"（{version}）" if version else "")


def list_agents() -> None:
    _log(f"目标 agent：Antigravity（{AGENT_CLI}）")
    _log(f"  {AGENT_CLI:<12} {detect()}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="autodl-mcp-install",
        description="把 autodl-mcp 插件装进 Antigravity（不写入 AUTODL_TOKEN）",
    )
    parser.add_argument("--list", action="store_true", help="查看本机 agy 的安装情况")
    parser.add_argument("--source", help=f"安装源（路径或 git URL），默认 {GIT_SOURCE} 或当前检出")
    parser.add_argument(
        "--from-git", action="store_true", help=f"强制用远端仓库安装（{GIT_SOURCE}）"
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印将执行的命令")
    args = parser.parse_args()

    if args.list:
        list_agents()
        return

    source = GIT_SOURCE if args.from_git else (args.source or default_source())
    _log(f"安装源：{source}" + ("（dry-run，不会真正执行）" if args.dry_run else ""))
    if not install_agent(source, dry_run=args.dry_run):
        _log("\n未能自动完成，请按上面的手动步骤操作。")

    _log(f"\n{TOKEN_HINT}")


if __name__ == "__main__":
    main()
