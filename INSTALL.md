# 安装

## 前置

- [uv](https://docs.astral.sh/uv/)（插件清单用 `uv run` 拉起服务，uv 会自己准备 Python 和依赖）
- `git clone https://github.com/Long-Holiday/autodl-mcp`（下面用 `<repo>` 指代这个目录）

一键装进本机检测到的 agent：

```bash
cd <repo>
uv run autodl-mcp-install --list                        # 看看有哪些 agent
uv run autodl-mcp-install --agent antigravity --agent claude
uv run autodl-mcp-install --agent cursor                # 写 MCP 配置 + 打印应用内步骤
uv run autodl-mcp-install --agent claude --dry-run      # 只看要执行什么
uv run autodl-mcp-install --from-git --agent claude     # 用远端仓库装，不依赖本地检出
```

安装器**不会写入 Token**，装完还要按最后一节设置 `AUTODL_TOKEN`。

---

## Antigravity CLI / IDE

仓库根目录是符合 [Agent Plugins v1.0.0](https://agent-plugins.org/) 的插件包，Antigravity 直接原生安装：

```bash
agy plugin install <repo>          # 也可以用 git URL：agy plugin install https://github.com/Long-Holiday/autodl-mcp
agy plugin list                    # 确认已装
agy plugin validate <repo>         # 校验插件清单
agy plugin uninstall autodl-mcp    # 卸载
```

一次安装同时拿到两样东西：

- `skills/autodl/SKILL.md` —— 技能（什么时候该开机、缺货怎么等、出错怎么办）
- `mcp_config.json` —— MCP 服务定义，服务名 `autodl`

> 实测（agy 1.3.5）：**Antigravity 读的是插件根目录的 `mcp_config.json`**，放一份标准的 `mcp.json` 进去它不认（`agy plugin validate` 会报 `mcpServers: skipped`）。本仓库两个都提供了：`mcp_config.json` 给 Antigravity，`mcp.json` 给其它按 Agent Plugins 标准实现的客户端。

插件里不带 Token（见最后一节）。

**同一份格式也被这些客户端读取**：Codex CLI、VS Code / GitHub Copilot、Kiro、Qwen Code 等 Agent Plugins 实现方。各家安装命令不同（例如 Qwen Code 用 `qwen extensions install <repo>`，Codex 用 `codex plugin marketplace add <owner>/<repo>`），但读的都是同一份 `plugin.json` + `mcp.json` + `skills/`。

---

## Claude Code

Anthropic 没有加入 Agent Plugins 标准，所以这里走 Claude Code 自己的插件格式（`.claude-plugin/` + `.mcp.json`）：

```bash
# 一步装好（Claude Code 2.1.275+）
claude plugin install autodl-mcp --marketplace Long-Holiday/autodl-mcp

# 或者先加市场再装
claude plugin marketplace add Long-Holiday/autodl-mcp
claude plugin install autodl-mcp@autodl-mcp
```

会话内等价写法：

```
/plugin install autodl-mcp --marketplace Long-Holiday/autodl-mcp
```

卸载：

```bash
claude plugin uninstall autodl-mcp
```

---

## Cursor

Cursor 的插件在应用内安装，没有插件安装 CLI：

1. 在 Cursor 里执行 `/add-plugin autodl-mcp`，或打开插件市场搜索 `autodl-mcp`（`plugin.json` 已经写好）。
2. MCP 服务由安装器接进配置：

```bash
uv run autodl-mcp-install --agent cursor                    # 写 ~/.cursor/mcp.json（全局）
uv run autodl-mcp-install --agent cursor --scope project    # 写 ./.cursor/mcp.json（项目级）
```

安装器是**读改写**：会保留文件里原有的其它 MCP 服务；如果目标文件不是合法 JSON，它会中止而不是覆盖。

---

## 其它客户端（通用兜底）

没有插件机制的客户端（Windsurf、Cline、Roo Code、Zed、opencode、Continue、Kiro、Trae…）用通用片段：

```bash
uv run autodl-mcp-install --generic --format json    # mcpServers 片段
uv run autodl-mcp-install --generic --format toml    # Codex 风格的 [mcp_servers.autodl]
```

片段默认用 `uv run --directory <绝对路径> autodl-mcp` 拉起服务。**如果你不希望配置里写死仓库路径**，先把服务装到 PATH 再用 `--command path`：

```bash
uv tool install <repo>                               # 装出 autodl-mcp 命令
uv run autodl-mcp-install --generic --command path
```

输出像这样，把它粘进对应客户端的 MCP 配置文件：

```json
{
  "mcpServers": {
    "autodl": {
      "command": "autodl-mcp",
      "env": {
        "AUTODL_TOKEN": "在这里填控制台登录 Token"
      }
    }
  }
}
```

`env` 这一段就是 Token 唯一需要出现的地方——它只存在于你自己的客户端配置里，不在仓库里。

---

## 设置 Token

服务从环境变量 `AUTODL_TOKEN` 读取控制台登录 Token（原文，**不要加 `Bearer` 前缀**）。两种给法：

**1. 从 shell 设（推荐，CLI agent 都适用）**

```bash
export AUTODL_TOKEN='你的 Token'
```

写进 `~/.zshrc` 或 `~/.bashrc` 后，从终端启动的 `agy`、`claude`、`codex`、`cursor-agent` 都会继承。

**2. 写在客户端 MCP 配置的 env 里（GUI 应用用这种）**

桌面图标启动的客户端可能不继承 shell 环境变量。这时给 `autodl` 服务加 `env`：

```json
"env": { "AUTODL_TOKEN": "你的 Token" }
```

配置完自检：

```bash
uv run autodl-mcp --check
```

它会打印实际读到的配置文件、Token 掩码和白名单，不访问网络。缺 Token 时会直接告诉你去哪里补。

---

## 附录：键名与众不同的客户端（未验证）

插进通用片段时注意，这几种客户端的配置结构和大部分客户端不一样，改错键名往往**不报错、只是静默不生效**：

| 客户端 | 顶层键 | 备注 |
| --- | --- | --- |
| VS Code / Copilot | `servers` | 项目级 `.vscode/mcp.json` |
| Zed | `context_servers` | `settings.json` |
| opencode | `mcp` | 每个服务要 `"type": "local"`，`command` 是「可执行文件 + 参数」的单个数组 |
| Codex CLI | TOML 的 `[mcp_servers.autodl]` | `~/.codex/config.toml`，用 `--format toml` |

以上键名来自公开资料，未在本仓库验证；以各家官方文档为准。

## 已知边界

- **插件清单里刻意不用 `${PLUGIN_ROOT}` 之类的占位符**。客户端对占位符的支持参差不齐：Claude Code 就不展开规范里的 `${PLUGIN_ROOT}`（只认自己的 `${CLAUDE_PLUGIN_ROOT}`），没展开时它会静默变成字面量路径、只在启动时报一个看不懂的连接错误。所以三份配置都用 `uv run autodl-mcp` 的相对形式——`cwd` 默认就是插件根目录（Agent Plugins 规范与 Claude Code 都这么定义）。
- 这样做的代价：uv 会把虚拟环境建在插件目录下的 `.venv`，插件升级后第一次启动会重建一次（约 1 秒）。
- **Cursor 插件是否自带 MCP 未公开**：所以 Cursor 的 MCP 走 `.cursor/mcp.json`，不假设插件能带 MCP。
- **`CLAUDE.md` / `GEMINI.md` 是指向 `AGENTS.md` 的符号链接**；在没开 `core.symlinks` 的 Windows 检出上会变成普通文件（功能不受影响，只是会重复一份内容）。
- 未发布 PyPI，所以清单里不用 `uvx autodl-mcp`。
- 任务只活在当前服务进程里，**不跨服务重启恢复**；取消任务或服务退出都不会自动关机。

### 装了插件但工具没出现？

按顺序排查：

1. 客户端里确认 autodl 服务已加载（Antigravity：`agy plugin list`，应显示 `1 MCP server`）。
2. 手动跑一遍同一条命令，看真实报错：

```bash
cd <repo> && AUTODL_TOKEN=你的Token uv run autodl-mcp --check
```

3. 如果报「缺少 AutoDL 控制台登录 Token」，那是预期的——按上一节设置 `AUTODL_TOKEN` 即可。
4. 如果客户端不是从终端启动的（macOS/Windows 桌面图标），它可能没有继承 `PATH`，找不到 `uv`。改用「其它客户端」那节里带上绝对路径、或先 `uv tool install` 再用 `--command path` 的写法。

## 卸载

```bash
agy plugin uninstall autodl-mcp              # Antigravity
claude plugin uninstall autodl-mcp           # Claude Code
uv run autodl-mcp-install --agent cursor --dry-run   # 看 Cursor 配置路径，手动删 mcpServers.autodl
uv tool uninstall autodl-mcp                 # 如果之前 uv tool install 过
```
