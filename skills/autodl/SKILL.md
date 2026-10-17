---
name: autodl
description: 管理 AutoDL 容器实例：开机（含无卡模式）、关机、查询实例状态与后台开机任务；缺货时后台按指数退避自动重试，不要手动反复调用。Use when the user asks to start, stop, restart, or check an AutoDL instance, when a GPU instance is out of capacity and needs waiting, or when a task fails because the instance is powered off. Not for AutoDL Pro (专业版) instances.
license: MIT
allowed-tools: mcp__autodl__instance_status, mcp__autodl__list_instances, mcp__autodl__job_status, mcp__autodl__wait_for_job
---

# AutoDL 实例管理

通过 `autodl` MCP 工具管理**普通 AutoDL 容器实例**（不适用于 Pro / 专业版）。所有操作都作用在真实账户上：**开机会计费，关机会中断实例上正在跑的任务**。

## 动手前必须确认

1. **开机前**先跟用户确认实例 UUID 和「现在开机」这件事——开机即开始计费，且会一直计费到关机。
2. **关机前**必须警告：实例上正在运行的任务会被中断，未保存的进度会丢失。除非用户明确说「关机」，不要主动关机。
3. 用户没给 UUID 时，先问，不要猜。用 `instance_status` 探测到的 UUID 也要回显给用户确认。

`power_on`、`power_on_no_gpu`、`power_off`、`switch_mode`、`cancel_job` 都是会改变账户/实例状态的操作，执行前要用一句话说明将要发生什么（含计费影响）。

## 标准流程

### 用户要「把实例开起来」

1. `instance_status(instance_uuid)` —— 先看现在是什么状态，避免重复开机。
2. 状态属于**可开机**集合（见下表）才继续；`running` / `starting` / `restarting` 说明已经在开机或已开机，直接告诉用户，不要重复发起。
3. 确认后 `power_on(instance_uuid)`。它**立即返回**一个 `job_id`，真正的等待在后台进行。
4. 向用户报告 `job_id`，并说明：缺货会在后台自动重试，不需要反复调用。
5. 等待完成时 `wait_for_job(job_id)`，即时看进度用 `job_status(job_id)`；`phase == "succeeded"` 表示实例已 `running` 且启动模式符合预期。

### 用户要「等有空闲 GPU」

不要循环调用 `power_on`。**只调用一次**，然后：
- 用 `job_status(job_id)` 查看 `phase`：`waiting_capacity` 表示在等机器，`attempts` 是已尝试次数，`suggested_next_poll_seconds` 是建议下次拉取前等待的秒数。
- 首次用 `wait_for_job(job_id)`：默认最多 15 秒，发现缺货立即返回。先向用户汇报 `attempts`、累计耗时 `elapsed_seconds` 和 `suggested_next_poll_seconds`；累计耗时不是平台排位或保证的排队时长。
- 不要对同一个缺货状态紧密重复调用。需要继续短等待时用 `wait_for_job(job_id, timeout_seconds=15, return_on_capacity=False)`；只有明确需要长等待时才加长秒数，最长 300 秒。不要 schedule 高频唤醒。
- 等待范围 0–300 秒（0 立即返回）；宿主调用超时更短时缩短该值。等待超时或断开不会取消开机任务，也不会额外查询上游或重新开机。
- `suggested_next_poll_seconds` 是建议下次拉取状态前等待的秒数（单调时钟计算、向上取整、最小为 0，无待定等待时为 `null`）。缺货/网络退避时 = 最近一次尝试间隔 + 当前倒计时，约跨过两次尝试窗口；首次尝试还没有历史间隔，或处于 `confirming` / `stopping` 轮询时，就是当前倒计时。需要相对等待时直接用它，无需计算本地时钟差；它不是资源可用倒计时，且仍有传输和处理延迟。
- 建议值可能超过 300 秒，`wait_for_job` 单次上限 300 秒；超出时分段等待或用 `job_status` 复查。
- 只需报告进度时，把建议的下次拉取间隔告诉用户，不要每秒轮询。
- 缺货重试间隔从 60 秒起步、逐步放大到 300 秒（可用 `AUTODL_RETRY_INTERVAL` / `AUTODL_RETRY_MAX_INTERVAL` 调整），所以短时间内频繁查询没有意义。

### 用户要「关机」

1. 先警告任务会被中断并得到确认。
2. `power_off(instance_uuid)`。
3. 返回值里：`already_stopped: true` 表示本来就是关机状态；`already_stopping: true` 表示已在关机中；否则 `accepted: true` 表示请求已接受。
4. **关机是异步的**。返回 `accepted` 不代表已经关掉，需要时用 `instance_status` 复查（`shutting_down` → 关机中，`shutdown` → 已关机）。

## 工具速查

| 工具 | 作用 | 读/写 |
| --- | --- | --- |
| `instance_status(instance_uuid)` | 查状态及 `gpu_name` / `gpu_memory_gb` / `cpu_cores` 配置规格。不返回密码或 SSH 凭证 | 只读 |
| `list_instances(instance_uuids)` | 一次批量查询 1–100 个 UUID；返回 instances 与逐项 errors，价格未知时不猜金额 | 只读 |
| `switch_mode(instance_uuid, target_mode="gpu")` | 单个后台任务关机、确认关机、按目标模式开机；目标 gpu / non_gpu | 破坏性、计费 |
| `power_on(instance_uuid)` | 申请 GPU 开机，立即返回 `job_id` | 计费 |
| `power_on_no_gpu(instance_uuid)` | 无卡模式开机，不申请 GPU，立即返回 `job_id` | 计费 |
| `power_off(instance_uuid)` | 关机，中断实例上的任务 | 破坏性 |
| `job_status(job_id)` | 查后台开机任务的 `phase` / `attempts` / `error` / `suggested_next_poll_seconds` | 只读 |
| `wait_for_job(job_id)` | 默认最多 15 秒，缺货/结束/超时返回快照；不取消任务 | 只读 |
| `cancel_job(job_id)` | 停止开机/切换任务的**后续步骤** | 写 |

## 硬件配置判读

`instance_status` 的硬件来自控制台「规格详情」配置快照，关机时也可查询。
`gpu_memory_gb` 是单卡显存（字节 / 1024³，与控制台 GB 显示一致），`cpu_cores` 是实例分配的 vCPU 数，
不是宿主总核数。缺失或无效的可选字段为 `null`，不要猜测型号对应的显存。
无卡模式也可能返回 GPU 配置，**不代表当前有 GPU 可用**；分配 GPU 任务前仍须检查 `start_mode`。

## 批量查询与价格

多个 UUID 使用一次 `list_instances(instance_uuids=[...])`，避免逐个重复扫描列表。
输入会去重并保持顺序，全部 UUID 先验证白名单；不存在实例和硬件失败在 `errors` 中，成功结果在 `instances` 中。
硬件获取失败可保留状态，规格为空；列表整体错误不能视为实例不存在。
用户没提供 UUID 时仍先询问，不猜测。`price: null` 和 `price_unavailable_reason` 表示报价尚未验证，
不是免费，也不要根据型号或 `charge_type` 推算价格。

## 状态判读

- **可开机**：`shutdown`、`created`、`created_clone_success`、`oss_merged`、`shutdown_by_starting_error`、`shutdown_by_running_error`。
- **已开机**：`running`。
- **正在开机**：`starting`、`restarting`、`queued`。
- **关机相关**：`shutting_down` 表示关机中。
- `start_mode` 为 `non_gpu` 表示当前是**无卡模式**；`charge_type` 为 `payg` 表示**按量计费**（无卡模式只支持按量计费实例）。
- 以上之外的未知状态：**不要强行开机**，把 `status` 原文报给用户，让对方在控制台确认。

## 任务 phase 判读

| phase | 含义 | 该怎么办 |
| --- | --- | --- |
| `queued` / `checking` | 排队 / 正在查实例状态 | 等待 |
| `stopping` | 模式切换正在请求/确认关机 | 等待；关机未确认绝不启动 |
| `starting` | 已发出开机请求 | 等待 |
| `waiting_capacity` | 目标机器无空闲 GPU，后台等资源释放 | 等待，不要重新调用 `power_on` |
| `waiting_network` | 网络类错误后退避重试（超时、5xx、限流） | 等待 |
| `confirming` | 已发出请求但结果不确定，正在观察实际状态 | 等待；此时**绝不能**再发一次开机 |
| `succeeded` | 实例已按预期模式运行 | 完成 |
| `failed` | 出错停止，原因在 `error` 里 | 按下面的错误处置 |
| `cancelled` | 被取消 | 不撤销已接受的电源请求；先确认实际状态，切换可能停在关机状态 |

## 错误处置

`job_status` 返回的 `error` 有 `kind` 字段，按它决定动作：

| kind | 含义 | 动作 |
| --- | --- | --- |
| `capacity` | 无空闲 GPU | 正常现象，等重试；不要反复开新任务 |
| `network` / `rate_limit` | 网络失败 / 被限流 | 等重试；`error.retry_after` 是建议等待秒数 |
| `auth` | Token 失效或无权限 | 让用户更新客户端 MCP 配置里的 `AUTODL_TOKEN`（控制台登录 Token 原文，**不带 `Bearer` 前缀**） |
| `business` | 账户或实例状态被上游拒绝 | 让用户到控制台检查余额、实名认证、实例状态 |
| `state` | 实例进入非预期状态，或开机模式冲突 | 先 `instance_status` 看实际状态，再决定；不要盲目重试 |
| `timeout` | 达到开机或关机确认上限（`AUTODL_START_TIMEOUT` / `AUTODL_STOP_TIMEOUT`，默认各 600 秒） | 报给用户，由用户决定是否再来一次 |
| `uncertain` | 请求可能已生效但**未确认** | **绝对不要直接重试**，先 `instance_status` 看是不是已经开机 |
| `not_found` | UUID 不存在或不属于当前账户 | 让用户核对 UUID |
| `internal` | 服务内部错误 | 报给用户，不要重试 |

## 无卡模式

- 无卡模式开机**仍然计费**，只是不占用 GPU；适用场景是「先把机器开起来装环境、传数据」。
- 只支持**按量计费**（`charge_type == "payg"`）实例，包年包月会直接报 `state` 错误。
- `power_on` / `power_on_no_gpu` 不会隐式切换运行中实例的模式。
- 要切换时，先确认 UUID、**中断运行任务/丢失未保存进度**以及重新开机计费，再调用一次 `switch_mode(instance_uuid, target_mode="gpu" 或 "non_gpu")`；不要手动反复编排电源工具。
- 切换是单个后台任务，不是可回滚事务；失败或取消可能停在关机状态，不自动恢复。无卡目标会在关机前校验按量计费要求。用同一 job_id 查询/等待/取消。
- 若要阻止切换后继续开机并关机，用 `power_off`；它会先取消活动切换。

## 取消任务

`cancel_job(job_id)` 只停止开机/切换任务的**后续步骤**：

- 不会撤销已经发出去的、上游可能已经接受的电源请求；
- 不会自动关机；
- 不保证实例状态未改变：已接受的关机/开机会继续生效，切换取消后可能处于关机状态。

如果用户真正想要的是「停下来并关掉」，顺序是：`cancel_job` → `instance_status` 确认实际状态 → 视情况 `power_off`。

## 不要做的事

- 不要为了等空闲 GPU 反复调用 `power_on`——它是幂等的，重复调用只会拿到同一个 `job_id`。
- 不要在 `phase` 处于 `confirming` 时重新发起开机。
- 不要在用户没确认的情况下关机。
- 不要把 `power_off` 的 `accepted: true` 当成「已经关掉了」。
- 不要用 `instance_status` 的失败去推断实例不存在——`not_found` 也可能是 UUID 打错或不属于当前账户。
- 任务只存在当前服务进程里，**不跨服务重启恢复**；取消或服务退出都不会自动关机。
