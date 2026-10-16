# 安装

插件只装进 Antigravity（`agy`）。

## 前置

- [uv](https://docs.astral.sh/uv/)（清单用 `uv run` 拉起服务，uv 会自己准备 Python 和依赖）
- `git clone https://github.com/Long-Holiday/autodl-mcp`（下面用 `<repo>` 指代这个目录）

一条命令装好：

```bash
cd <repo>
uv run autodl-mcp-install --list        # 先看本机 agy 装在哪、什么版本
uv run autodl-mcp-install               # 执行 agy plugin install <repo>
uv run autodl-mcp-install --dry-run     # 只看将要执行什么
uv run autodl-mcp-install --from-git    # 用远端仓库装，不依赖本地检出
```

安装器**不会写入 Token**，装完还要按下面「设置 Token」一节自己给。

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
- `mcp.json` —— MCP 服务定义，服务名 `autodl`

> 实测：agy 1.4.0 读插件根目录的 `mcp.json`（`agy plugin validate .` 会显示 `mcpServers 1`）。早期版本（1.3.5）只认 `mcp_config.json`，本仓库不再提供那一份——如果你卡在旧版上，先升级 `agy`。

---

## 设置 Token

服务从环境变量 `AUTODL_TOKEN` 读取控制台登录 Token（原文，**不要加 `Bearer` 前缀**）。两种给法：

**1. 从 shell 设（推荐）**

```bash
export AUTODL_TOKEN='你的 Token'
```

写进 `~/.zshrc` 或 `~/.bashrc` 后，从终端启动的 `agy` 会继承。

**2. 写在 MCP 配置的 env 里（GUI 启动时用这种）**

桌面图标启动的 Antigravity 可能不继承 shell 环境变量。这时给 `autodl` 服务加 `env`：

```json
"env": { "AUTODL_TOKEN": "你的 Token" }
```

完整的示例见 [mcp-config.example.json](mcp-config.example.json)——`env` 那一段就是 Token 唯一需要出现的地方，它只存在于你自己的配置里，不在仓库里。

配置完自检：

```bash
uv run autodl-mcp --check
```

它会打印实际读到的配置文件、Token 掩码和白名单，不访问网络。缺 Token 时会直接告诉你去哪里补。

---

## 已知边界

- **清单里刻意不用 `${PLUGIN_ROOT}` 之类的占位符**：没展开时它会静默变成字面量路径，只在启动时报一个看不懂的连接错误，很难排查。所以 `mcp.json` 用 `uv run autodl-mcp` 的相对形式——`cwd` 默认就是插件根目录（Agent Plugins 规范这么定义）。
- 这样做的代价：uv 会把虚拟环境建在插件目录下的 `.venv`，插件升级后第一次启动会重建一次（约 1 秒）。
- 未发布 PyPI，所以清单里不用 `uvx autodl-mcp`。
- 任务只活在当前服务进程里，**不跨服务重启恢复**；取消任务或服务退出都不会自动关机。

### 装了插件但工具没出现？

按顺序排查：

1. Antigravity 里确认 autodl 服务已加载：`agy plugin list` 应显示 `1 MCP server`。
2. 手动跑一遍同一条命令，看真实报错：

```bash
cd <repo> && AUTODL_TOKEN=你的Token uv run autodl-mcp --check
```

3. 如果报「缺少 AutoDL 控制台登录 Token」，那是预期的——按上面「设置 Token」一节处理。
4. 如果 Antigravity 不是从终端启动的（macOS/Windows 桌面图标），它可能没有继承 `PATH`，找不到 `uv`。改用 `"command": "uv", "args": ["run", "--directory", "<仓库绝对路径>", "autodl-mcp"]`，或先 `uv tool install <repo>` 再用 `"command": "autodl-mcp"`。

## 卸载

```bash
agy plugin uninstall autodl-mcp
uv tool uninstall autodl-mcp       # 如果之前 uv tool install 过
```
