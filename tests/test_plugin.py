"""插件清单校验：格式合规、三份 MCP 配置一致、任何文件里都不含 token。"""

import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
# Agent Plugins 1.0.0 的 plugin.json 字段集是封闭的
PLUGIN_FIELDS = {
    "$schema",
    "name",
    "version",
    "description",
    "author",
    "homepage",
    "repository",
    "license",
    "keywords",
    "extensions",
}
PLUGIN_NAME = re.compile(r"^(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
STDIO_FIELDS = {"type", "command", "args", "env", "cwd"}
SERVER_NAME = "autodl"
CLAUDE_MCP = ROOT / ".mcp.json"

# 三份 MCP 配置：(文件, 是否带 $schema 与 type)
# 故意不使用 ${PLUGIN_ROOT} 之类的占位符：cwd 默认就是插件根目录（Agent Plugins 规范与
# Claude Code 都这么定义），所以相对路径就够，少依赖一个客户端行为。
# Claude Code 不会展开规范里的 ${PLUGIN_ROOT}（只认自己的 ${CLAUDE_PLUGIN_ROOT}）。
MCP_CONFIGS = [
    (ROOT / "mcp.json", True),
    (ROOT / "mcp_config.json", False),
    (CLAUDE_MCP, False),
]


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_agent_plugins_manifest():
    manifest = load(ROOT / "plugin.json")
    assert manifest["$schema"] == PLUGIN_SCHEMA
    assert set(manifest) <= PLUGIN_FIELDS, "plugin.json 出现了规范之外的字段"
    assert {"$schema", "name"} <= set(manifest)
    name = manifest["name"]
    assert 1 <= len(name) <= 64
    assert PLUGIN_NAME.fullmatch(name), f"name 不符合 Agent Plugins 命名规则：{name}"
    assert manifest["version"] == _project_version()


def _project_version() -> str:
    """版本号以 pyproject.toml 为准，避免三份清单各写各的。"""
    for line in (ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        if line.startswith("version = "):
            return line.split("=", 1)[1].strip().strip('"')
    raise AssertionError("pyproject.toml 里没有 version")


@pytest.mark.parametrize(("path", "typed"), MCP_CONFIGS)
def test_mcp_config_shape(path, typed):
    config = load(path)
    if typed:  # 只有 Agent Plugins 标准那份带 $schema
        assert config["$schema"] == MCP_SCHEMA
        assert set(config) <= {"$schema", "mcpServers"}
    else:
        assert set(config) == {"mcpServers"}

    assert set(config["mcpServers"]) == {SERVER_NAME}
    server = config["mcpServers"][SERVER_NAME]
    assert set(server) <= STDIO_FIELDS
    if typed:
        assert server["type"] == "stdio"
    else:
        assert "type" not in server, "Antigravity / Claude Code 的原生配置不接受 type 字段"

    # command 必须是单个可执行 token，不能是 shell 字符串
    assert server["command"] == "uv"
    assert " " not in server["command"] and "${" not in server["command"]
    # args 用相对路径：cwd 默认是插件根目录，这样哪个客户端都不需要展开占位符
    assert server["args"] == ["run", "autodl-mcp"]
    assert "env" not in server, "环境变量由用户自己在客户端配置里给，插件不预设"


def test_no_placeholder_dependency():
    """三份配置里都不能出现占位符。

    客户端对占位符的支持参差不齐（Claude Code 就不展开规范里的 ${PLUGIN_ROOT}），
    一旦没展开会静默变成字面量路径，很难排查。所以干脆一个都不用。
    """
    for path, _typed in MCP_CONFIGS:
        assert "${" not in path.read_text(encoding="utf-8")


def test_three_mcp_configs_describe_one_server():
    """三份配置除标准那份多一个 type 外，服务定义必须完全一致。"""
    shapes = {
        json.dumps(
            {k: v for k, v in load(path)["mcpServers"][SERVER_NAME].items() if k != "type"}
        )
        for path, _typed in MCP_CONFIGS
    }
    assert len(shapes) == 1, "三份 MCP 配置描述的启动方式不一致"


def test_claude_plugin_and_marketplace():
    manifest = load(ROOT / ".claude-plugin" / "plugin.json")
    assert manifest["name"] == "autodl-mcp"
    # Claude Code 拒绝冒用官方名字的插件名
    assert not manifest["name"].startswith(("claude-", "anthropic-", "cc-plugin-"))

    marketplace = load(ROOT / ".claude-plugin" / "marketplace.json")
    assert marketplace["name"] and marketplace["owner"]["name"]
    entries = marketplace["plugins"]
    assert [entry["name"] for entry in entries] == ["autodl-mcp"]
    assert entries[0]["source"] == "./"


def test_cursor_plugin_and_marketplace():
    manifest = load(ROOT / ".cursor-plugin" / "plugin.json")
    assert manifest["name"] == "autodl-mcp"

    marketplace = load(ROOT / ".cursor-plugin" / "marketplace.json")
    assert [entry["name"] for entry in marketplace["plugins"]] == ["autodl-mcp"]
    assert marketplace["plugins"][0]["source"] == "."


def test_all_manifests_agree_on_version():
    versions = {
        load(ROOT / "plugin.json")["version"],
        load(ROOT / ".claude-plugin" / "plugin.json")["version"],
        load(ROOT / ".cursor-plugin" / "plugin.json")["version"],
        _project_version(),
    }
    assert len(versions) == 1, f"版本号不一致：{versions}"


def test_skill_frontmatter_and_safety():
    skill = ROOT / "skills" / "autodl" / "SKILL.md"
    text = skill.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    frontmatter = yaml.safe_load(text.split("---", 2)[1])
    # 目录名必须与技能名一致，description 要带英文关键词供模型路由和插件市场检索
    assert frontmatter["name"] == skill.parent.name
    assert "autodl" in frontmatter["description"].lower()
    assert "AutoDL" in frontmatter["description"]

    # 会改账户状态（且计费）的工具不能预先放行，必须留给用户确认
    allowed = frontmatter["allowed-tools"]
    if not isinstance(allowed, list):
        allowed = allowed.split(",")
    assert {tool.strip() for tool in allowed} == {
        "mcp__autodl__instance_status",
        "mcp__autodl__job_status",
    }


def test_no_token_committed_anywhere():
    """仓库里任何配置文件都不能出现真实 token；示例文件只允许占位符。"""
    for path in [*(path for path, _typed in MCP_CONFIGS), ROOT / "mcp-config.example.json"]:
        text = path.read_text(encoding="utf-8")
        if "AUTODL_TOKEN" not in text:
            continue
        for server in load(path)["mcpServers"].values():
            token = (server.get("env") or {}).get("AUTODL_TOKEN")
            if token is not None:
                assert token.startswith("<"), f"{path} 里的 AUTODL_TOKEN 必须留占位符"

    # 随代码提交的 config.yaml 会被 Settings.load() 拒绝写入 token，这里再兜一层
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    assert "token" not in config
    # 插件清单里根本不该提到 token：值由用户自己在客户端配置里给
    assert "AUTODL_TOKEN" not in (ROOT / "plugin.json").read_text(encoding="utf-8")


def test_plugin_root_has_no_real_claude_md():
    """真实的根 CLAUDE.md 会让 claude plugin validate --strict 失败，必须是符号链接。"""
    if not (ROOT / "CLAUDE.md").exists():
        pytest.skip("Windows 未启用符号链接的检出")
    assert (ROOT / "CLAUDE.md").is_symlink()
    assert (ROOT / "CLAUDE.md").readlink() == Path("AGENTS.md")
    assert (ROOT / "GEMINI.md").is_symlink()
