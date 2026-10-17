# autodl-mcp

管理**普通 AutoDL 容器实例**的 MCP 服务 + [Antigravity](https://antigravity.google/) 插件：开机（含无卡模式）、关机、查询实例与后台开机任务状态。缺货时在后台按指数退避自动重试，不用你盯着。

不适用于 AutoDL Pro（专业版）。

仓库根目录本身就是插件根目录，装进 Antigravity 一次拿到技能（`skills/autodl/SKILL.md`）和 MCP 服务（`mcp.json`）。

## 只支持 Antigravity

插件清单只提供一份 Agent Plugins v1.0.0 标准格式（`plugin.json` + `mcp.json` + `skills/`），安装入口只有 `agy`。仓库里**没有**别的 agent 的插件格式或 MCP 配置文件——不打算支持，也不要往这里加。

需要别的形态（比如手写一份 `mcpServers` 片段）时，照着 [mcp-config.example.json](mcp-config.example.json) 自己写，服务本身是标准的 stdio MCP 服务。

## 快速开始

```bash
git clone https://github.com/Long-Holiday/autodl-mcp
cd autodl-mcp

uv run autodl-mcp-install --list        # 看本机 agy 装在哪、什么版本
uv run autodl-mcp-install               # 装进 Antigravity（不写 token）
uv run autodl-mcp-install --dry-run     # 只看将要执行的命令
```

`--from-git` 用远端仓库装，不依赖本地检出；`--source <路径或 git URL>` 指定别的安装源。

装完在 Antigravity 里应该能看到 `autodl` 服务的这些工具：

| 工具 | 作用 |
| --- | --- |
| `instance_status` | 查实例状态及 GPU 型号、单卡显存、分配的 CPU 核心数（不返回凭证） |
| `power_on` | 申请 GPU 开机，**立即返回 `job_id`**，缺货后台重试 |
| `power_on_no_gpu` | 无卡模式开机（仅按量计费实例） |
| `power_off` | 关机，**会中断实例上正在运行的任务** |
| `job_status` | 查后台开机任务的 phase / 重试时间 / 错误 |
| `wait_for_job` | 长轮询等待任务完成（默认最多 300 秒），超时返回当前状态，不取消任务 |
| `cancel_job` | 只取消后续尝试，不会关机 |

配套的技能文件会告诉 agent：开机前要先确认（会计费）、不要在 `confirming` 阶段重复发起、`cancel_job` 不等于关机、不同 `error.kind` 分别该怎么处理。

### 硬件规格与等待任务

`instance_status(instance_uuid)` 新增 `gpu_name`、`gpu_memory_gb`、`cpu_cores`。
规格来自控制台「规格详情」的配置快照，关机时也可查询；缺失或无效的可选值为 `null`。
`gpu_memory_gb` 是**单卡**显存，按 1024³ 字节换算（与控制台 GB 显示一致）；
`cpu_cores` 是分配的 vCPU 数，不是宿主机总核数。无卡模式下返回的 GPU 配置不代表当前已挂载 GPU。

开机得到 `job_id` 后，可调用 `wait_for_job(job_id, timeout_seconds=300)`，
任务进入 `succeeded` / `failed` / `cancelled` 时立即返回，否则等待到期返回当前快照。
`timeout_seconds` 范围为 0–300 秒，0 表示立即查询；若返回活动 `phase`，说明任务尚未结束，
可继续长轮询。等待超时或中断不会取消后台任务，也不会增加上游查询或开机请求。
仍可用 `job_status` 即时查看进度。宿主工具调用超时若短于 300 秒，请选用更短的等待时间。
任务只保存在当前 MCP 进程中，不跨服务重启恢复。

## Token 怎么给

**仓库和插件清单里不存任何密钥。** 服务从环境变量 `AUTODL_TOKEN` 读取，需要你自己设置：

```bash
export AUTODL_TOKEN='控制台登录 Token 原文（不要加 Bearer 前缀）'
```

- Token 从浏览器 DevTools → Network → 任意实例请求的 `Authorization` 头里取原文。
- 从终端启动的 `agy` 会继承这个环境变量，写进 `~/.zshrc` 或 `~/.bashrc` 设一次就够。
- 从桌面图标启动的 GUI 应用可能不继承 shell 环境，这时在它的 MCP 配置里给 `autodl` 服务加 `"env": {"AUTODL_TOKEN": "…"}`（见 [mcp-config.example.json](mcp-config.example.json)）。

设置完自检：

```bash
uv run autodl-mcp --check
```

自检只验证配置能否加载，不访问网络；Token 是否有效要等第一次真实请求才能确认。

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

详细的安装、排查与卸载步骤见 [INSTALL.md](INSTALL.md)。

## 开发

```bash
uv run pytest -q
uv run ruff check .
agy plugin validate .
```

仓库约定见 [AGENTS.md](AGENTS.md)。

## 许可证

[MIT](LICENSE)
