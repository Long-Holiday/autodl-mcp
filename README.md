# autodl-mcp

管理**普通 AutoDL 容器实例**的 MCP 服务 + 跨 agent 插件包：开机（含无卡模式）、关机、查询实例与后台开机任务状态。缺货时在后台按指数退避自动重试，不用你盯着。

不适用于 AutoDL Pro（专业版）。

仓库根目录本身就是插件根目录，一份技能（`skills/autodl/SKILL.md`）被所有 agent 共用。

## 支持哪些 agent

| Agent | 走的格式 | 安装方式 |
| --- | --- | --- |
| Antigravity CLI / IDE | Agent Plugins v1.0.0（`plugin.json` + `mcp_config.json` + `skills/`） | `agy plugin install <本仓库>` |
| Claude Code | 私有格式（`.claude-plugin/` + `.mcp.json`） | `/plugin install autodl-mcp --marketplace Long-Holiday/autodl-mcp` |
| Cursor | 私有格式（`.cursor-plugin/`） | 应用内 `/add-plugin`，MCP 写进 `.cursor/mcp.json` |
| Codex CLI、VS Code / Copilot、Kiro、Qwen Code 等 | 同一份 Agent Plugins v1.0.0 格式 | 各家插件生态的安装命令 |
| 其余客户端（Windsurf、Cline、Roo、Zed、opencode、Continue…） | 通用 `mcpServers` 片段 | `autodl-mcp-install --generic` |

详细的每个生态的步骤见 [INSTALL.md](INSTALL.md)。

## 快速开始

```bash
git clone https://github.com/Long-Holiday/autodl-mcp
cd autodl-mcp

# 自动装进本机检测到的 agent（不写 token）
uv run autodl-mcp-install --list
uv run autodl-mcp-install --agent antigravity --agent claude

# 其它客户端：打印可直接粘贴的配置片段
uv run autodl-mcp-install --generic --format json
```

`--dry-run` 只打印将要执行的命令和将要写入的内容。

## Token 怎么给

**仓库和插件清单里不存任何密钥。** 服务从环境变量 `AUTODL_TOKEN` 读取，需要你自己在 agent 的 MCP 配置里设置：

```bash
export AUTODL_TOKEN='控制台登录 Token 原文（不要加 Bearer 前缀）'
```

- Token 从浏览器 DevTools → Network → 任意实例请求的 `Authorization` 头里取原文。
- 从 shell 启动的 agent（Claude Code、agy CLI、Codex…) 会继承这个环境变量，设一次就够。
- 从桌面图标启动的 GUI 应用可能不继承 shell 环境，此时在该客户端的 MCP 配置里给 `autodl` 服务加 `"env": {"AUTODL_TOKEN": "…"}`（见 INSTALL.md 的通用片段）。

设置完自检：

```bash
uv run autodl-mcp --check
```

自检只验证配置能否加载，不访问网络；Token 是否有效要等第一次真实请求才能确认。

## 提供的工具

| 工具 | 作用 |
| --- | --- |
| `instance_status` | 查实例状态（不返回密码或 SSH 凭证） |
| `power_on` | 申请 GPU 开机，**立即返回 `job_id`**，缺货后台重试 |
| `power_on_no_gpu` | 无卡模式开机（仅按量计费实例） |
| `power_off` | 关机，**会中断实例上正在运行的任务** |
| `job_status` | 查后台开机任务的 phase / 重试时间 / 错误 |
| `cancel_job` | 只取消后续尝试，不会关机 |

配套的技能文件会告诉 agent：开机前要先确认（会计费）、不要在 `confirming` 阶段重复发起、`cancel_job` 不等于关机、不同 `error.kind` 分别该怎么处理。

## 配置

非敏感配置随代码提交在 [config.yaml](config.yaml)，每一项都能被同名环境变量覆盖：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `instance_allowlist` | `[]` | 允许操作的实例 UUID；空表示允许全部自有实例 |
| `capacity_codes` | `[]` | 额外视为「无空闲 GPU」的上游返回码 |
| `retry_interval` / `retry_max_interval` | `60` / `300` | 缺货重试的起止间隔（秒） |
| `transient_retry_interval` / `transient_retry_max_interval` | `60` / `900` | 网络类错误的退避起止间隔（秒） |
| `max_wait_seconds` | `0` | 单次开机等待上限，`0` 表示不限时 |
| `poll_interval` | `5` | 开机状态轮询间隔（秒） |
| `start_timeout` | `600` | 单次开机请求的确认上限（秒） |
| `request_timeout` | `30` | 单次 HTTP 请求超时（秒） |

用 `AUTODL_CONFIG=/path/to/config.yaml` 可以指向别处的配置文件。

## 开发

```bash
uv run pytest -q
uv run ruff check .
```

仓库约定见 [AGENTS.md](AGENTS.md)。
