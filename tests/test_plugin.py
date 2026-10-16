"""插件清单校验：格式合规、MCP 配置、面向 Antigravity 的打包、任何文件里都不含 token。"""

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

# 只面向 Antigravity：插件清单就一份标准格式。
# 实测 agy 1.4.0 读的是这里；别的 agent 的私有清单/配置（.claude-plugin/、.cursor-plugin/、
# .mcp.json、mcp_config.json）都已经删掉，不该再加回来（见 test_only_antigravity_packaging）。
MCP_CONFIG = ROOT / "mcp.json"

# 对外文件：都得存在，且都不许提到别的 agent（代码里提到=又接了一条安装路径，
# 文档里提到=又在向别人承诺支持）。
AGENT_FACING_FILES = [
    "plugin.json",
    "mcp.json",
    "mcp-config.example.json",
    "README.md",
    "INSTALL.md",
    "AGENTS.md",
    "skills/autodl/SKILL.md",
    "src/autodl_mcp/install.py",
]
OTHER_AGENTS = re.compile(
    r"\b(claude|anthropic|cursor|codex|copilot|windsurf|cline|roo|kiro|qwen|zed|opencode|"
    r"gemini|continue)\b",
    re.IGNORECASE,
)


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


def test_mcp_config_shape():
    config = load(MCP_CONFIG)
    assert config["$schema"] == MCP_SCHEMA
    assert set(config) <= {"$schema", "mcpServers"}

    assert set(config["mcpServers"]) == {SERVER_NAME}
    server = config["mcpServers"][SERVER_NAME]
    assert set(server) <= STDIO_FIELDS
    assert server["type"] == "stdio"

    # command 必须是单个可执行 token，不能是 shell 字符串
    assert server["command"] == "uv"
    assert " " not in server["command"] and "${" not in server["command"]
    # args 用相对路径：cwd 默认是插件根目录，这样不需要展开占位符
    assert server["args"] == ["run", "autodl-mcp"]
    assert "env" not in server, "环境变量由用户自己在 agy 的 MCP 配置里给，插件不预设"


def test_no_placeholder_dependency():
    """配置里不能出现占位符。

    客户端对占位符的支持参差不齐，一旦没展开会静默变成字面量路径，很难排查。所以一个都不用。
    """
    assert "${" not in MCP_CONFIG.read_text(encoding="utf-8")


def test_only_antigravity_packaging():
    """本插件只发给 Antigravity；别的 agent 的清单/配置不许回来。"""
    for stray in (
        ".claude-plugin",
        ".cursor-plugin",
        ".mcp.json",
        "mcp_config.json",
        "GEMINI.md",
    ):
        assert not (ROOT / stray).exists(), f"{stray} 属于别的 agent，本仓库只面向 Antigravity"


def test_no_other_agent_names_in_agent_facing_files():
    for name in AGENT_FACING_FILES:
        text = (ROOT / name).read_text(encoding="utf-8")
        found = sorted({m.group(0).lower() for m in OTHER_AGENTS.finditer(text)})
        assert not found, f"{name} 里提到了别的 agent：{found}"


def test_all_manifests_agree_on_version():
    versions = {load(ROOT / "plugin.json")["version"], _project_version()}
    assert len(versions) == 1, f"版本号不一致：{versions}"


def _project_version() -> str:
    """版本号以 pyproject.toml 为准，避免清单和包各写各的。"""
    for line in (ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        if line.startswith("version = "):
            return line.split("=", 1)[1].strip().strip('"')
    raise AssertionError("pyproject.toml 里没有 version")


def test_skill_frontmatter_and_safety():
    skill = ROOT / "skills" / "autodl" / "SKILL.md"
    text = skill.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    frontmatter = yaml.safe_load(text.split("---", 2)[1])
    # 目录名必须与技能名一致，description 要带英文关键词供模型路由
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
    for path in (MCP_CONFIG, ROOT / "mcp-config.example.json"):
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
    # 插件清单里根本不该提到 token：值由用户自己在 agy 的 MCP 配置里给
    assert "AUTODL_TOKEN" not in (ROOT / "plugin.json").read_text(encoding="utf-8")


def test_repo_root_has_no_agent_instruction_files():
    """仓库约定只有 AGENTS.md 一份，别的 agent 的约定文件不要出现。"""
    assert (ROOT / "AGENTS.md").is_file()
    for name in ("CLAUDE.md", "GEMINI.md"):
        assert not (ROOT / name).exists(), f"{name} 是别的 agent 的约定文件"


@pytest.mark.parametrize("name", AGENT_FACING_FILES)
def test_agent_facing_files_exist(name):
    assert (ROOT / name).is_file(), f"{name} 不该被删掉或改名"
