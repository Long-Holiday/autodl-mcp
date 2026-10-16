# 仓库约定

`autodl-mcp` 既是 MCP 服务，也是**发给 Antigravity（`agy`）的插件包**：仓库根目录就是插件根目录。

插件只面向 Antigravity 一家：清单只有 Agent Plugins 标准那一份，不再提供别的 agent 的插件格式或 MCP 配置。别人家的那几份清单/配置原本放在仓库根目录，已经删干净，不要加回来（`tests/test_plugin.py` 会把它们的路径列出来兜底）。

## 目录职责

| 路径 | 作用 |
| --- | --- |
| `src/autodl_mcp/` | MCP 服务本体（client / jobs / config / server）与安装器 `install.py` |
| `skills/autodl/SKILL.md` | 唯一的技能文件；讲的是工具给不了的判断（计费、异步、重试、错误处置） |
| `plugin.json` | Agent Plugins v1.0.0 清单，`agy plugin install` / `validate` 读它 |
| `mcp.json` | MCP 服务定义（服务名 `autodl`），agy 启动时读它 |
| `mcp-config.example.json` | 手动改 MCP 配置时的示例（带 `AUTODL_TOKEN` 占位符），不参与插件加载 |
| `AGENTS.md` | 本文件；仓库约定只有这一份 |

## 硬性规则

- **任何提交进仓库的文件里都不能出现 AutoDL Token。** 服务只从环境变量 `AUTODL_TOKEN` 读；`config.yaml` 里出现 `token:` 会被 `Settings.load()` 直接拒绝（见 `src/autodl_mcp/config.py`）。
- **`mcp.json` 里不许出现 `${...}` 占位符。** 展开行为各家客户端都不一样，没展开时会静默变成字面量路径、只在启动时报一个看不懂的连接错误。改用相对形式 `uv run autodl-mcp`：`cwd` 默认就是插件根目录（Agent Plugins 规范这么定义）。
- **只有一份 MCP 配置。** 实测 agy 1.4.0 读的是插件根目录的 `mcp.json`；1.3.5 时代只认 `mcp_config.json`，那一份已经删掉，不要再补。
- **不许在对外文件里提别的 agent。** `tests/test_plugin.py` 扫描 `plugin.json`、`mcp.json`、`README.md`、`INSTALL.md`、`AGENTS.md`、`SKILL.md`、`install.py`，出现别人家的名字就失败——代码里出现意味着又接了一条安装路径，文档里出现意味着又在向别人承诺支持。
- 技能的 `allowed-tools` 只列只读工具。开机/关机/取消会改账户状态，必须留给用户确认，不要预先放行。
- `plugin.json` 的字段集是封闭的（`$schema`、`name`、`version`、`description`、`author`、`homepage`、`repository`、`license`、`keywords`、`extensions`），加别的字段会被校验器忽略或报错。

## 开发

```bash
uv run pytest -q                  # 全部测试
uv run ruff check .               # lint
uv run autodl-mcp --check         # 配置自检（不访问网络）
uv run autodl-mcp-install --list  # 看本机 agy 的情况
```

改完清单后用真机校验：

```bash
agy plugin validate .
```

## MCP 服务的约定

- 服务跑在 stdio 上，stdout 只走协议；日志和报错一律走 stderr。
- 上游返回的原始 `msg` 不进日志、不回传给模型——可能含账号信息或凭证（见 `client.py` 的 `_post`）。
- 后台任务只活在当前进程里，不跨服务重启恢复；这点在工具描述和技能里都写明了，改动时要保持一致。
