# kiro-gateway-deploy

在 Cursor 里通过自定义 OpenAI Base URL，使用 Kiro 订阅的 Claude 模型。

把 kiro-gateway 跑成 Mac / Windows / Linux 的本地托盘小工具（**无需 Docker、无需自己的服务器**）：
进程内跑网关，子进程跑 cloudflared，把本机网关经 Cloudflare 网络暴露成
`https://kg-<你的用户名>.<域名>/v1` 供 Cursor 直接使用。

> 💸 **Team 方案注意：即使 BYOK 也要收 Cursor Token 费。**
>
> 根据 [Cursor 模型与价格文档](https://cursor.com/cn/docs/models-and-pricing#cursor-token)，在**团队（Teams）方案**中，非 Auto 的智能体请求需支付每百万 token **$0.25** 的 Cursor Token 费率。这笔费用是在模型 API 定价之外**额外收取**的，且**适用于自带密钥（BYOK）用量**——也就是说，即使你用本网关把模型流量接到自己的 Kiro 订阅上，Cursor 仍会按通过它的 token 量收这笔费。
>
> 只有 **Auto 免收** Cursor Token 费率。个人方案（Pro / Pro Plus / Ultra）目前不收这笔费用，此提示主要针对 Team 方案用户。

## 背景

Cursor 支持自定义 OpenAI 兼容的 API 地址，但有几个坑：

1. **需要公网地址**：Cursor 会先把请求发回自己的服务器，再转发到你指定的目标地址——所以本地部署的服务 Cursor 根本到不了。本项目用 Cloudflare Tunnel 把本机网关暴露到公网（托盘 App 自动完成隧道创建，普通用户不用碰 Cloudflare 控制台）。

2. **只能走 OpenAI 兼容协议，Claude 模型需要别名**：Cursor 只允许自定义 OpenAI 地址，不能自定义 Anthropic 地址，并会特殊处理 `claude-*` 模型名。因此 Claude 使用不含 `opus`/`sonnet`/`haiku` 的 `kiro-o-*`、`kiro-s-*`、`kiro-h-*` 别名。GPT 系列直接使用 Cursor 已有的真实模型名，无需 alias，也无需手动增加 model name。`auto` 同样直接使用原生名称。上游列表缺失但可用的模型仍会按真实 ID 补入 FALLBACK。

3. **用量查询**：用了这个以后就不直接用 Kiro 客户端了，看不到额度消耗。所以加了一个 `GET /usage` 端点，能随时查订阅用量。

## 快速开始（托盘 App）

**前置条件**：本机已用 Kiro IDE 登录过（存在 `~/.aws/sso/cache/kiro-auth-token.json`）。

1. 从 [GitHub Releases](https://github.com/zhujunsan/kiro-gateway-deploy/releases) 下载对应平台的安装包：
   - macOS：`KiroGatewayTray-<ver>-macos-arm64.dmg` → 打开 DMG，拖入 Applications
   - Windows：`KiroGatewayTray-<ver>-windows-amd64-setup.exe` → 双击运行安装向导
   - Linux：`kiro-gateway-tray-<ver>-linux-x86_64.AppImage` → `chmod +x`，双击或直接运行

   > macOS 也可以用 Homebrew 安装（本仓库即是 tap）：
   >
   > ```bash
   > brew tap zhujunsan/kiro-gateway-deploy https://github.com/zhujunsan/kiro-gateway-deploy
   > brew trust zhujunsan/kiro-gateway-deploy
   > brew install --cask kiro-gateway-tray
   > ```
   >
   > 新版 Homebrew 默认拒绝加载第三方 tap，若安装时报 `Refusing to load cask ... from untrusted tap`，先执行上面的 `brew trust zhujunsan/kiro-gateway-deploy`（或 `brew trust --cask zhujunsan/kiro-gateway-deploy/kiro-gateway-tray`）再重试。
   >
   > App 采用**临时（ad-hoc）签名**，不是付费 Apple 开发者签名/公证。临时签名消除了 Apple Silicon 上「已损坏，无法打开」的报错，但首次打开仍会提示「来自身份不明的开发者」。两种打开方式任选其一：
   > - **右键打开**：在 Applications 里右键点 `KiroGatewayTray.app` → 「打开」→ 弹窗里再点「打开」（仅首次需要）。
   > - **去掉隔离标记**（Homebrew 安装已自动执行，DMG 手动安装时可用）：
   >
   > ```bash
   > xattr -dr com.apple.quarantine "/Applications/KiroGatewayTray.app"
   > ```
   >
   > 升级：`brew update && brew upgrade --cask kiro-gateway-tray`

2. 首次运行 App → 自动弹出引导对话框，只需填两项：
   - **Provision 服务地址**：管理员提供的隧道签发 URL（已填过则不再问）
   - **激活码**：管理员发给你的共享密钥

   其余配置全自动完成（`profile_arn`/`api_region` 从 Kiro token 读取、`proxy_api_key`
   自动生成、注册成功后 `hostname`/`run_token` 自动写入），无需手动编辑 `config.toml`。

3. 注册完成后 App 自动启动网关和隧道。从托盘菜单复制凭据，填进
   Cursor → Settings → Models → OpenAI API Key & Base URL：
   - **API Key**：托盘「复制 Gateway 密码」（自动生成的 `proxy_api_key`）
   - **Base URL**：托盘「复制 Tunnel URL」（即 `https://kg-<你的用户名>.<域名>/v1`）

4. Claude 和其他需要避开名称嗅探的模型，在 Cursor 模型列表里添加下方别名；GPT 系列与 `auto` 直接选择 Cursor 已有模型，无需新增 model name。以后每次开机启动 App 即可，无需再输激活码。

> 完整的 App 使用说明、开发者构建步骤、`config.toml` 配置项见 **[`app/README.md`](app/README.md)**。
> 管理员部署签发服务（Worker）见 **[`docs/cloudflare-setup.md`](docs/cloudflare-setup.md)**。

## 可用模型

直接使用真实模型名（无需 alias，也无需在 Cursor 手动增加 model name）：`auto`、`gpt-5.6-sol`、`gpt-5.6-terra`、`gpt-5.6-luna`。

需要手动添加的别名：

| 别名 | 实际模型 |
|---|---|
| `kiro-o-4.8` | `claude-opus-4.8` |
| `kiro-o-4.7` | `claude-opus-4.7` |
| `kiro-o-4.6` | `claude-opus-4.6` |
| `kiro-s-5` | `claude-sonnet-5` |
| `kiro-s-4.6` | `claude-sonnet-4.6` |
| `kiro-h-4.5` | `claude-haiku-4.5` |
| `kiro-deepseek-3.2` | `deepseek-3.2` |
| `kiro-glm-5` | `glm-5` |
| `kiro-minimax-m2.5` | `minimax-m2.5` |
| `kiro-qwen3-coder-next` | `qwen3-coder-next` |

要修改别名，编辑 fork（[`zhujunsan/kiro-gateway`](https://github.com/zhujunsan/kiro-gateway)）`kiro/config.py` 里的 `MODEL_ALIASES`，推送后 CI 产出新镜像 tag。

## 查额度（`GET /usage`）

Kiro 官方客户端能看到用量，但你用网关代替后就看不到了。本项目额外注入了一个端点：

```bash
curl -H "Authorization: Bearer $PROXY_API_KEY" https://kg-<你的用户名>.<域名>/usage
```

返回示例：

```json
{
  "subscription": "KIRO PRO",
  "nextDateReset": 1782864000.0,
  "breakdowns": [
    { "used": 787.73, "limit": 1000.0 }
  ],
  "region": "us-east-1"
}
```

加 `?raw=true` 可以看上游返回的完整原始 JSON。

## 其他部署方式：Docker

仓库早期用 Docker Compose 把网关跑成长驻容器 + Cloudflare Tunnel，这套方式仍然保留，
适合想跑在常开服务器上、或不想装托盘 App 的场景。完整说明见 **[`docker/README.md`](docker/README.md)**。

## 仓库结构

```
.
├── README.md                   # 本文件（总览 + 托盘 App 快速开始）
├── app/                        # 原生托盘 App（主线）
│   └── README.md               # App 使用 / 构建 / 配置说明
├── worker/                     # kiro-provision Cloudflare Worker（隧道签发服务）
│   └── README.md               # Worker 部署说明
├── docker/                     # Docker Compose 部署（早期方式，仍保留）
│   ├── docker-compose.yml
│   ├── .env.example
│   └── README.md
└── docs/
    └── cloudflare-setup.md     # 管理员：Cloudflare + Worker 配置操作手册
```

---

## 技术细节

本项目基于 [`ghcr.io/zhujunsan/kiro-gateway`](https://github.com/zhujunsan/kiro-gateway) —— 这是上游 [`jwadow/kiro-gateway`](https://github.com/jwadow/kiro-gateway) 的 fork，把针对 Cursor 后端的四处改动直接写进了源码：

1. `kiro/config.py` + `kiro/model_resolver.py` — 注册 `kiro-*` 模型别名，并让 `get_model_id_for_kiro()`（转换器热路径）识别别名
2. `main.py` — 新增 `GET /usage` 端点（走 Amazon Q `getUsageLimits`）
3. `kiro/converters_openai.py` — OpenAI 适配器回退解析 Anthropic 风格的 `tool_use` content block，避免工具调用历史被降级成纯文本
4. `kiro/payload_guards.py` — 裁剪超大 payload 时固定 `history[0]`，避免折叠进去的 system prompt 被裁掉

fork 的 CI（`.github/workflows/docker.yml`）在 push 到 main 时自动构建并推送多架构镜像，tag 为 `main-<sha>` 与 `latest`。托盘 App 内打包的是同一份网关源码。

## 致谢

- [jwadow/kiro-gateway](https://github.com/jwadow/kiro-gateway) — 上游网关
- [hank9999/kiro.rs](https://github.com/hank9999/kiro.rs) — `getUsageLimits` 接口调用参考
