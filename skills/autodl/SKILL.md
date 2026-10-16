---
name: autodl
description: 管理 AutoDL 容器实例：开机（含无卡模式）、关机、查询实例状态与后台开机任务；缺货时后台按指数退避自动重试，不要手动反复调用。Use when the user asks to start, stop, restart, or check an AutoDL instance, when a GPU instance is out of capacity and needs waiting, or when a task fails because the instance is powered off. Not for AutoDL Pro (专业版) instances.
license: MIT
allowed-tools: mcp__autodl__instance_status, mcp__autodl__job_status
---

# AutoDL 实例管理

通过 `autodl` MCP 工具管理**普通 AutoDL 容器实例**（不适用于 Pro / 专业版）。所有操作都作用在真实账户上：**开机会计费，关机会中断实例上正在跑的任务**。

## 动手前必须确认

1. **开机前**先跟用户确认实例 UUID 和「现在开机」这件事——开机即开始计费，且会一直计费到关机。
2. **关机前**必须警告：实例上正在运行的任务会被中断，未保存的进度会丢失。除非用户明确说「关机」，不要主动关机。
3. 用户没给 UUID 时，先问，不要猜。用 `instance_status` 探测到的 UUID 也要回显给用户确认。

`power_on`、`power_on_no_gpu`、`power_off`、`cancel_job` 都是会改变账户/实例状态的操作，执行前要用一句话说明将要发生什么（含计费影响）。

## 标准流程

### 用户要「把实例开起来」

1. `instance_status(instance_uuid)` —— 先看现在是什么状态，避免重复开机。
2. 状态属于**可开机**集合（见下表）才继续；`running` / `starting` / `restarting` 说明已经在开机或已开机，直接告诉用户，不要重复发起。
3. 确认后 `power_on(instance_uuid)`。它**立即返回**一个 `job_id`，真正的等待在后台进行。
4. 向用户报告 `job_id`，并说明：缺货会在后台自动重试，不需要反复调用。
5. 需要进度时 `job_status(job_id)`；`phase == "succeeded"` 表示实例已 `running` 且启动模式符合预期。

### 用户要「等有空闲 GPU」

不要循环调用 `power_on`。**只调用一次**，然后：
- 用 `job_status(job_id)` 查看 `phase`：`waiting_capacity` 表示在等机器，`attempts` 是已尝试次数，`next_retry_at` 是下次重试时间。
- 把 `next_retry_at` 告诉用户，或者按这个时间间隔再查一次，而不是每秒轮询。
- 缺货重试间隔从 60 秒起步、逐步放大到 300 秒（可用 `AUTODL_RETRY_INTERVAL` / `AUTODL_RETRY_MAX_INTERVAL` 调整），所以短时间内频繁查询没有意义。

### 用户要「关机」

1. 先警告任务会被中断并得到确认。
2. `power_off(instance_uuid)`。
3. 返回值里：`already_stopped: true` 表示本来就是关机状态；`already_stopping: true` 表示已在关机中；否则 `accepted: true` 表示请求已接受。
4. **关机是异步的**。返回 `accepted` 不代表已经关掉，需要时用 `instance_status` 复查（`shutting_down` → 关机中，`shutdown` → 已关机）。

## 工具速查

| 工具 | 作用 | 读/写 |
| --- | --- | --- |
| `instance_status(instance_uuid)` | 查单个实例状态（`status`、`sub_status`、`start_mode`、`charge_type`）。不返回密码或 SSH 凭证 | 只读 |
| `power_on(instance_uuid)` | 申请 GPU 开机，立即返回 `job_id` | 计费 |
| `power_on_no_gpu(instance_uuid)` | 无卡模式开机，不申请 GPU，立即返回 `job_id` | 计费 |
| `power_off(instance_uuid)` | 关机，中断实例上的任务 | 破坏性 |
| `job_status(job_id)` | 查后台开机任务的 `phase` / `attempts` / `error` / `next_retry_at` | 只读 |
| `cancel_job(job_id)` | 停止某个开机任务的**后续尝试** | 写 |

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
| `starting` | 已发出开机请求 | 等待 |
| `waiting_capacity` | 目标机器无空闲 GPU，后台等资源释放 | 等待，不要重新调用 `power_on` |
| `waiting_network` | 网络类错误后退避重试（超时、5xx、限流） | 等待 |
| `confirming` | 已发出请求但结果不确定，正在观察实际状态 | 等待；此时**绝不能**再发一次开机 |
| `succeeded` | 实例已按预期模式运行 | 完成 |
| `failed` | 出错停止，原因在 `error` 里 | 按下面的错误处置 |
| `cancelled` | 被 `cancel_job` 取消 | 实例状态未改变，需要时重新发起 |

## 错误处置

`job_status` 返回的 `error` 有 `kind` 字段，按它决定动作：

| kind | 含义 | 动作 |
| --- | --- | --- |
| `capacity` | 无空闲 GPU | 正常现象，等重试；不要反复开新任务 |
| `network` / `rate_limit` | 网络失败 / 被限流 | 等重试；`error.retry_after` 是建议等待秒数 |
| `auth` | Token 失效或无权限 | 让用户更新客户端 MCP 配置里的 `AUTODL_TOKEN`（控制台登录 Token 原文，**不带 `Bearer` 前缀**） |
| `business` | 账户或实例状态被上游拒绝 | 让用户到控制台检查余额、实名认证、实例状态 |
| `state` | 实例进入非预期状态，或开机模式冲突 | 先 `instance_status` 看实际状态，再决定；不要盲目重试 |
| `timeout` | 达到单次开机等待上限（`AUTODL_START_TIMEOUT`，默认 600 秒） | 报给用户，由用户决定是否再来一次 |
| `uncertain` | 请求可能已生效但**未确认** | **绝对不要直接重试**，先 `instance_status` 看是不是已经开机 |
| `not_found` | UUID 不存在或不属于当前账户 | 让用户核对 UUID |
| `internal` | 服务内部错误 | 报给用户，不要重试 |

## 无卡模式

- 无卡模式开机**仍然计费**，只是不占用 GPU；适用场景是「先把机器开起来装环境、传数据」。
- 只支持**按量计费**（`charge_type == "payg"`）实例，包年包月会直接报 `state` 错误。
- **不会**把正在以 GPU 模式运行的实例自动切成无卡模式，也不会反过来。实例已用另一种模式运行时，会报「实例已以其他模式运行」——此时要让用户先关机（并确认 `status` 变成 `shutdown`）再切换模式。

## 取消任务

`cancel_job(job_id)` 只停止**后续的重试尝试**：

- 不会撤销已经发出去的、上游可能已经接受的开机请求；
- 不会自动关机；
- 实例状态不会因此改变。

如果用户真正想要的是「停下来并关掉」，顺序是：`cancel_job` → `instance_status` 确认实际状态 → 视情况 `power_off`。

## 不要做的事

- 不要为了等空闲 GPU 反复调用 `power_on`——它是幂等的，重复调用只会拿到同一个 `job_id`。
- 不要在 `phase` 处于 `confirming` 时重新发起开机。
- 不要在用户没确认的情况下关机。
- 不要把 `power_off` 的 `accepted: true` 当成「已经关掉了」。
- 不要用 `instance_status` 的失败去推断实例不存在——`not_found` 也可能是 UUID 打错或不属于当前账户。
- 任务只存在当前服务进程里，**不跨服务重启恢复**；取消或服务退出都不会自动关机。
