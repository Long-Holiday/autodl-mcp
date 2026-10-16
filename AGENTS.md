# 仓库约定

`autodl-mcp` 既是 MCP 服务，也是可分发给多个 agent 的**插件包**：仓库根目录就是插件根目录。

## 目录职责

| 路径 | 作用 |
| --- | --- |
| `src/autodl_mcp/` | MCP 服务本体（client / jobs / config / server）与安装器 `install.py` |
| `skills/autodl/SKILL.md` | 唯一的技能文件，所有 agent 共用；讲的是工具给不了的判断（计费、异步、重试、错误处置） |
| `plugin.json` + `mcp.json` | Agent Plugins v1.0.0 标准 → Antigravity、Codex、VS Code/Copilot、Kiro、Qwen 等 |
| `mcp_config.json` | Antigravity 原生 MCP 配置（形状与 `mcp.json` 相同，但没有 `type` 字段） |
| `.claude-plugin/` + `.mcp.json` | Claude Code 私有格式（Anthropic 未加入 Agent Plugins 标准） |
| `.cursor-plugin/` | Cursor 私有格式 |

## 硬性规则

- **任何提交进仓库的文件里都不能出现 AutoDL Token。** 服务只从环境变量 `AUTODL_TOKEN` 读；`config.yaml` 里出现 `token:` 会被 `Settings.load()` 直接拒绝（见 `src/autodl_mcp/config.py`）。
- 三份 MCP 配置（`mcp.json`、`mcp_config.json`、`.mcp.json`）必须描述同一个服务：服务名 `autodl`，除标准那份多一个 `"type": "stdio"` 外，字节级一致。`tests/test_plugin.py` 会断言这一点。
- **三份配置里都不许出现 `${...}` 占位符。** 客户端对占位符的支持参差不齐——实测 `agy` 1.3.5 读的是插件根目录的 `mcp_config.json`（不读标准的 `mcp.json`），而 Claude Code 只认自己前缀的 `${CLAUDE_PLUGIN_ROOT}`、不展开规范里的 `${PLUGIN_ROOT}`，没展开时会静默变成字面量路径。改用 `uv run autodl-mcp` 的相对形式：`cwd` 默认就是插件根目录（规范和 Claude Code 都这么定义），少依赖一个客户端行为。
- `CLAUDE.md`、`GEMINI.md` 是指向 `AGENTS.md` 的**符号链接**，不要改成普通文件：插件根目录出现真实的 `CLAUDE.md` 会让 `claude plugin validate --strict` 失败。
- 技能的 `allowed-tools` 只列只读工具。开机/关机/取消会改账户状态，必须留给用户确认，不要预先放行。
- `plugin.json` 的字段集是封闭的（`$schema`、`name`、`version`、`description`、`author`、`homepage`、`repository`、`license`、`keywords`、`extensions`），加别的字段会被校验器忽略或报错。

## 开发

```bash
uv run pytest -q                  # 全部测试
uv run ruff check .               # lint
uv run autodl-mcp --check         # 配置自检（不访问网络）
uv run autodl-mcp-install --list  # 查看本机装了哪些 agent CLI
```

改完清单后建议用真机校验：

```bash
agy plugin validate .
claude plugin validate .
```

## MCP 服务的约定

- 服务跑在 stdio 上，stdout 只走协议；日志和报错一律走 stderr。
- 上游返回的原始 `msg` 不进日志、不回传给模型——可能含账号信息或凭证（见 `client.py` 的 `_post`）。
- 后台任务只活在当前进程里，不跨服务重启恢复；这点在工具描述和技能里都写明了，改动时要保持一致。
