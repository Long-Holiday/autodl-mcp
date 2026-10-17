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
| `list_instances` | 批量查询 1–100 个 UUID 的状态和硬件，共享分页；价格未验证时明确返回未知 |
| `switch_mode` | 一个后台任务完成关机、确认关机、按目标模式开机；会中断工作且重新计费 |
| `power_on` | 申请 GPU 开机，**立即返回 `job_id`**，缺货后台重试 |
| `power_on_no_gpu` | 无卡模式开机（仅按量计费实例） |
| `power_off` | 关机，**会中断实例上正在运行的任务** |
| `job_status` | 查后台开机任务的 phase / 重试时间 / 错误 |
| `wait_for_job` | 短等待任务完成（默认最多 15 秒），缺货立即返回进度；不取消任务 |
| `cancel_job` | 只取消后续尝试，不会关机；返回取消后的物理状态 `resulting_status` |

配套的技能文件会告诉 agent：开机前要先确认（会计费）、不要在 `confirming` 阶段重复发起、`cancel_job` 不等于关机、不同 `error.kind` 分别该怎么处理。

### 硬件规格与等待任务

`instance_status(instance_uuid)` 新增 `gpu_name`、`gpu_memory_gb`、`cpu_cores`。
规格来自控制台「规格详情」的配置快照，关机时也可查询；缺失或无效的可选值为 `null`。
`gpu_memory_gb` 是**单卡**显存，按 1024³ 字节换算（与控制台 GB 显示一致）；
`cpu_cores` 是分配的 vCPU 数，不是宿主机总核数。无卡模式下返回的 GPU 配置不代表当前已挂载 GPU。

开机或切换得到 `job_id` 后，可调用 `wait_for_job(job_id)`：默认最多等 **15 秒**，
任务结束或发现缺货（`waiting_capacity`）时立即返回。即使显式传入 `timeout_seconds=120`，
默认也不会在已知缺货时继续静默等待。快照包含 `attempts`、`elapsed_seconds`、`suggested_next_poll_seconds`。
`suggested_next_poll_seconds` 是建议下次拉取状态前等待的秒数，基于单调时钟计算、向上取整且不小于 0；
缺货/网络退避时等于最近一次尝试间隔加当前倒计时（约跨过两次尝试窗口），其余等待阶段就是当前倒计时；
无待定等待时为 `null`。它避免调用方用本地时钟计算时间差，但仍有传输和处理延迟，不代表资源可用倒计时。
`elapsed_seconds` 是任务累计耗时（终态固定），**不是平台承诺的排队时长或排位**。

收到缺货快照后，先向用户报告尝试次数、已等待时间和建议的下次拉取间隔，不要紧密重复调用。
按 `suggested_next_poll_seconds` 用宿主定时调度器登记一次性唤醒（Antigravity CLI 为 `schedule`），
到点再用 `job_status` 复核；不要用 shell `sleep` 盲等，也不要设置高频循环唤醒。
宿主没有定时能力时，需要继续短等待才用 `wait_for_job(job_id, timeout_seconds=15, return_on_capacity=False)`；
显式长等待仍可使用 `timeout_seconds=120, return_on_capacity=False`。
秒数范围为 0–300，0 立即返回；活动 phase 表示尚未完成。等待超时或中断不会取消后台任务，
也不会增加上游查询或开机请求。即时进度仍可用 `job_status` 查询。
任务只保存在当前 MCP 进程中，不跨服务重启恢复。

### 批量查询

`list_instances(instance_uuids=["uuid-a", "uuid-b"])` 共用一次列表分页，再以最多 5 路并发读取硬件快照。
1–100 个 UUID 全部通过格式/白名单校验后才发请求；重复 UUID 去重，结果按输入顺序排列。
返回 `instances` 与 `errors`：单个实例找不到或硬件获取失败不会抹掉其他成功结果；
硬件失败时保留状态，规格为 `null`，错误在 `errors` 中。列表整体鉴权/协议错误则整次调用失败。

**价格尚未提供准确报价**：现有接口资料未验证价格字段、币种及计费单位，
所以每项返回 `price: null` 和 `price_unavailable_reason`，**不代表免费**。
接入真实报价需要经过验证的接口资料或脱敏响应样例，不根据 `charge_type` 猜金额。

### 无卡 / GPU 模式切换

确认中断任务与重新计费后，调用 `switch_mode(instance_uuid, target_mode="gpu")`，
或 `target_mode="non_gpu"`（无卡，仅按量计费）。工具立即返回 `job_id`，后台顺序完成：
检查 → 关机 → 确认关机 → 按目标模式开机 → 确认运行；GPU 缺货沿用自动退避。
已按目标模式运行时不关机；已经关机则直接开机；已经关机中则只等待。
同一实例相同切换复用活动任务，其他活动操作冲突需先取消并确认实例状态。

这是**编排而非可回滚事务**。失败、取消或服务退出可能留下运行中、关机中或已关机的实例，
不会自动回滚；取消不会撤销已经接受的电源请求。
`cancel_job` 会返回取消后立即查询的物理状态 `resulting_status`（以及 `resulting_start_mode`）。
例如关机完成后在 `waiting_capacity` 等 GPU 时取消，会直接得到 `resulting_status: "shutdown"`，
不必再调 `instance_status`；过渡态（如 `starting` / `shutting_down`）或查询失败（`resulting_status` 为 `null`，
原因见 `resulting_status_error`）时仍需复查。
`power_off` 会先取消切换，防止其后续自动拉起。
`stopping` 表示正在请求/确认关机，未观察到关机状态绝不开机。
关机确认受 `stop_timeout` 限制，超时或结果不确定时停止切换，请查询实际状态再决定。

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
| `stop_timeout` | `600` | 模式切换的关机确认上限（秒），可用 `AUTODL_STOP_TIMEOUT` 覆盖 |
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
