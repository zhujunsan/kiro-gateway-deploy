# kiro-gateway-deploy

在 Cursor 里通过自定义 OpenAI Base URL，使用 Kiro 订阅的 Claude 模型。

> ⚠️ **重要限制：启用本方案后，Cursor 里的官方 OpenAI 模型（GPT 系列）将无法使用。**
>
> Cursor 一旦开启自定义 OpenAI API Key + Base URL，会把**所有 OpenAI 品牌的模型（`gpt-*`、`o*` 等）**全部路由到你设的 Base URL，而不是只对手动添加的别名生效。本网关只认识 `kiro-*` 别名，不认识 `gpt-*`，所以选 GPT 系列会直接报错。这是 Cursor 的全局劫持行为，没有「部分 GPT 走 Cursor、部分走自定义地址」的分流开关。
>
> 如果你还想在 Cursor 里用官方 GPT 模型，只能二选一：
> 1. **来回切开关**：用 Cursor 原生 GPT（订阅额度）时关掉自定义 OpenAI Key，用 Kiro 的 Claude 时再打开。
> 2. **让网关代理 GPT**：自行扩展网关，把 `gpt-*`/`o*` 透传到官方 `https://api.openai.com/v1`（用你自己的 OpenAI API Key，走 OpenAI 官方计费，非 Cursor 订阅额度）。

> 💸 **Team 方案注意：即使 BYOK 也要收 Cursor Token 费。**
>
> 根据 [Cursor 模型与价格文档](https://cursor.com/cn/docs/models-and-pricing#cursor-token)，在**团队（Teams）方案**中，非 Auto 的智能体请求需支付每百万 token **$0.25** 的 Cursor Token 费率。这笔费用是在模型 API 定价之外**额外收取**的，且**适用于自带密钥（BYOK）用量**——也就是说，即使你用本网关把模型流量接到自己的 Kiro 订阅上，Cursor 仍会按通过它的 token 量收这笔费。
>
> 只有 **Auto 免收** Cursor Token 费率。个人方案（Pro / Pro Plus / Ultra）目前不收这笔费用，此提示主要针对 Team 方案用户。

## 背景

Cursor 支持自定义 OpenAI 兼容的 API 地址，但有几个坑：

1. **需要公网地址**：Cursor 会先把请求发回自己的服务器，再转发到你指定的目标地址——所以本地部署的服务 Cursor 根本到不了。本项目提供两种把网关暴露到公网的方式，二选一或都开：
   - **Cloudflare Tunnel**：不需要自己的服务器，但要一个托管在 Cloudflare 的域名（域名可在别处买，改 NS 到 Cloudflare 免费版即可）。
   - **自建 frps 转发**：你自己有一台带公网 IP 的服务器并跑了 frps，用 frpc 把网关反代出去。大陆服务器也能用。

2. **只能走 OpenAI 兼容协议，且 `claude-*` 模型名有特殊处理**：Cursor 只允许自定义 OpenAI 地址，不能自定义 Anthropic 地址。而且对 `claude` 开头的模型名做了特殊路由——即使你把它加到自定义模型列表里，请求仍然不走你的 OpenAI 兼容地址。所以需要给模型起别名（`kiro-sonnet-4.6`、`kiro-opus-4.6` 等），让 Cursor 认为这是一个未知模型，走你的自定义地址。另外上游 `ListModels` 里没有 opus 4.8，也顺手补上了。

3. **用量查询**：用了这个以后就不直接用 Kiro 客户端了，看不到额度消耗。所以加了一个 `GET /usage` 端点，能随时查订阅用量。

## 快速开始

### 1. 安装 Docker

Mac 上推荐 [OrbStack](https://orbstack.dev/)（轻量替代 Docker Desktop），安装后开箱即用，自带 `docker` 和 `docker compose` 命令。

### 2. 准备公网入口（二选一）

Cursor 后端在海外，需要能访问到一个稳定的公网地址，再由它打回你本机的网关。下面两种方式选一个即可（也可以都配，用 `COMPOSE_PROFILES` 控制启用哪个，见步骤 3）。

#### 方式 A：Cloudflare Tunnel

不需要自己的服务器，但要一个托管在 Cloudflare 的域名（域名可在别处买，改 NS 指到 Cloudflare 免费版即可）。去 [Cloudflare Zero Trust](https://one.dash.cloudflare.com/) 控制台：

1. 左侧 Networks → Tunnels → Create a tunnel
2. 选 Cloudflared，取个名字，创建后会给你一个 tunnel token（`eyJ...` 开头的长串）
3. 在 Public Hostname 里添加一条：域名指向 `http://kiro-gateway:8000`（这是 compose 内部的服务名）

记下 tunnel token，后面填到 `.env` 的 `CLOUDFLARED_TOKEN`。最终 Base URL 是 `https://<your-cloudflare-domain>/v1`。

#### 方式 B：自建 frps 转发

你自己有一台带公网 IP 的服务器并跑了 frps（大陆服务器也能用），用 frpc 把网关反代出去。

1. 在 frps 服务器上准备 `frps.toml` 并启动，至少包含：

   ```toml
   bindPort = 7000
   auth.method = "token"
   auth.token = "<和 frpc 一致的强随机串>"
   ```

   放行入站端口：frps 的 `bindPort`（默认 7000）和你要暴露网关的 `FRP_REMOTE_PORT`（默认 8080）。

2. 本机这侧的 frpc 已在 `docker-compose.yml` 里配好，配置模板见 `frpc/frpc.toml`，只需在 `.env` 填 `FRP_SERVER_ADDR` / `FRP_TOKEN` 等变量（见步骤 3）。

最终 Base URL：
- 纯 TCP 直出：`http://<frps-ip>:<FRP_REMOTE_PORT>/v1`（明文传输，`PROXY_API_KEY` 务必用强随机串）
- 若在 frps 侧再挂一层带证书的反代（如 Caddy/Nginx）走域名：`https://<your-domain>/v1`（推荐）

### 3. 准备 Kiro 凭据

本机需要已完成 Kiro SSO 登录，确保 `~/.aws/sso/cache/kiro-auth-token.json` 存在。直接打开 Kiro IDE 登录一次即可。

### 4. 配置并启动

```bash
git clone https://github.com/zhujunsan/kiro-gateway-deploy.git
cd kiro-gateway-deploy
cp .env.example .env
open -e .env
```

公共项（两种隧道都要填）：

```
PROFILE_ARN=arn:aws:codewhisperer:us-east-1:<account>:profile/<id>
PROXY_API_KEY=<给客户端用的鉴权 key，自己生成一个强随机串>
```

再用 `COMPOSE_PROFILES` 选择启用哪个隧道，并填对应变量：

```
# 只用 Cloudflare
COMPOSE_PROFILES=cloudflare
CLOUDFLARED_TOKEN=<方式 A 拿到的 tunnel token>

# 只用 frps
COMPOSE_PROFILES=frp
FRP_SERVER_ADDR=<frps 服务器公网 IP 或域名>
FRP_TOKEN=<和 frps.toml 里 auth.token 一致的串>
# 可选：FRP_SERVER_PORT（默认 7000）、FRP_REMOTE_PORT（默认 8080）

# 两个都开
COMPOSE_PROFILES=cloudflare,frp
```

启动：

```bash
docker compose up -d
```

查看日志确认网关起来了：

```bash
docker compose logs kiro-gateway | tail -20
```

正常应看到 uvicorn 监听 `:8000`、healthcheck 通过。隧道日志可按需查看：`docker compose logs -f cloudflared` 或 `docker compose logs -f frpc`。

### 5. 配置 Cursor

Cursor Settings → Models → OpenAI API Key & Base URL：

- **API Key**: 填你 `.env` 里设的 `PROXY_API_KEY`
- **Base URL**: 按你用的隧道填：
  - Cloudflare：`https://<your-cloudflare-domain>/v1`
  - frps 纯 TCP 直出：`http://<frps-ip>:<FRP_REMOTE_PORT>/v1`
  - frps + 域名反代：`https://<your-domain>/v1`

然后在模型列表里添加你想用的别名（见下表），就可以在 Cursor 里选这些模型了。

### 6. 验证

把 `<base-url>` 换成上一步的地址：

```bash
curl -H "Authorization: Bearer $PROXY_API_KEY" \
     <base-url>/models
```

## 可用模型

| 别名 | 实际模型 |
|---|---|
| `auto-kiro` | `auto` |
| `kiro-opus-4.8` | `claude-opus-4.8` |
| `kiro-opus-4.7` | `claude-opus-4.7` |
| `kiro-opus-4.6` | `claude-opus-4.6` |
| `kiro-sonnet-4.6` | `claude-sonnet-4.6` |
| `kiro-sonnet-4.5` | `claude-sonnet-4.5` |
| `kiro-haiku-4.5` | `claude-haiku-4.5` |

要修改别名，编辑 fork（[`zhujunsan/kiro-gateway`](https://github.com/zhujunsan/kiro-gateway)）`kiro/config.py` 里的 `MODEL_ALIASES`，推送后 CI 产出新镜像 tag，再改 `docker-compose.yml` 的 `image:`。

## 查额度（`GET /usage`）

Kiro 官方客户端能看到用量，但你用网关代替后就看不到了。本项目额外注入了一个端点：

```bash
curl -H "Authorization: Bearer $PROXY_API_KEY" http://localhost:18000/usage
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

---

## 技术细节

### 工作原理

本项目基于 [`ghcr.io/zhujunsan/kiro-gateway`](https://github.com/zhujunsan/kiro-gateway) 镜像 —— 这是上游 [`jwadow/kiro-gateway`](https://github.com/jwadow/kiro-gateway) 的 fork，把针对 Cursor 后端的四处改动直接写进了源码（不再靠启动时 patch）：

1. `kiro/config.py` + `kiro/model_resolver.py` — 注册 `kiro-*` 模型别名，并让 `get_model_id_for_kiro()`（转换器热路径）识别别名
2. `main.py` — 新增 `GET /usage` 端点（走 Amazon Q `getUsageLimits`）
3. `kiro/converters_openai.py` — OpenAI 适配器回退解析 Anthropic 风格的 `tool_use` content block，避免工具调用历史被降级成纯文本
4. `kiro/payload_guards.py` — 裁剪超大 payload 时固定 `history[0]`，避免折叠进去的 system prompt 被裁掉

fork 的 CI（`.github/workflows/docker.yml`）在 push 到 main 时自动构建并推送多架构镜像，tag 为 `main-<sha>` 与 `latest`。

> `patches/` 目录保留作参考 —— 这四处改动最初就是以启动时 patch 的形式在这里验证的，现已原样落进 fork 源码。运行时不再挂载或执行它们；改逻辑请直接改 fork 源码。

### 目录结构

```
.
├── docker-compose.yml
├── .env                        # 实际密钥（已 gitignore）
├── .env.example                # 占位模板
├── .gitignore
├── README.md
├── frpc/
│   └── frpc.toml               # frpc 配置模板（用 FRP_* 环境变量渲染）
├── tools/
│   └── check_usage.py          # 独立脚本：不依赖容器，直接读 token 查额度
└── patches/                    # 参考存档：fork 源码改动的来源（运行时不再使用）
    ├── apply_aliases.py
    ├── add_usage_endpoint.py
    ├── fix_trim_keep_system.py
    └── fix_openai_tooluse_blocks.py
```

### 配置项

`.env` 中所有变量

| 变量 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `PROFILE_ARN` | 是 | — | CodeWhisperer profile ARN |
| `PROXY_API_KEY` | 是 | — | 客户端调用网关时的 Bearer token |
| `COMPOSE_PROFILES` | 否 | `cloudflare` | 启用哪个隧道：`cloudflare` / `frp` / `cloudflare,frp` |
| `CLOUDFLARED_TOKEN` | 视隧道 | — | Cloudflare Tunnel token（用 cloudflare 时必填） |
| `FRP_SERVER_ADDR` | 视隧道 | — | frps 服务器公网 IP 或域名（用 frp 时必填） |
| `FRP_TOKEN` | 视隧道 | — | frpc ↔ frps 鉴权 token，需与 frps.toml 一致（用 frp 时必填） |
| `FRP_SERVER_PORT` | 否 | `7000` | frps 的 `bindPort` |
| `FRP_REMOTE_PORT` | 否 | `8080` | frps 上暴露网关的公网端口 |
| `KIRO_GATEWAY_PORT` | 否 | `18000` | 宿主机暴露端口 |
| `KIRO_API_REGION` | 否 | `us-east-1` | Kiro / CodeWhisperer 区域 |
| `TUNNEL_TRANSPORT_PROTOCOL` | 否 | `http2` | Cloudflare 隧道传输协议 |

### 常用命令

```bash
docker compose up -d                 # 启动
docker compose down                  # 停止并移除容器
docker compose restart kiro-gateway  # 重启网关
docker compose logs -f kiro-gateway  # 跟随网关日志
docker compose logs -f cloudflared   # 跟随 Cloudflare 隧道日志（用 cloudflare 时）
docker compose logs -f frpc          # 跟随 frpc 日志（用 frp 时）
docker compose ps                    # 查看健康状态
```

> 镜像 tag 已在 `docker-compose.yml` 里固定（pin 到某个 `main-<sha>`），所以 `docker compose pull` 拿到的还是同一个版本，不会自动升级。升级方法见下方「升级上游镜像」。

### 升级

镜像被刻意 pin 在 `docker-compose.yml` 里的 `main-<sha>`。升级有两种情况：

**只想拿 fork 的新构建**（已有改动的小修）：在 fork 仓库 push 后，CI 产出新的 `main-<sha>` tag，把 `docker-compose.yml` 的 `image:` 改成新 tag，`docker compose down && docker compose up -d` 重建即可。

**想同步上游新版本**：在 fork（`zhujunsan/kiro-gateway`）把上游 `jwadow/kiro-gateway` 的 main rebase/merge 进来。四处改动现在是源码里的正常 commit，大概率能干净合并；若上游改了相同位置产生冲突，正常解决冲突即可（不会再有「patch 锚点失配」那种脆弱性）。推送后 CI 出新 tag，再改 `docker-compose.yml`。

升级后看日志确认起得来：

```bash
docker compose logs -f kiro-gateway
```

### 故障排查

**healthcheck 不过 / 隧道起不来**

```bash
docker compose ps
docker compose logs --tail=200 kiro-gateway
docker compose logs --tail=200 cloudflared   # 用 cloudflare 时
docker compose logs --tail=200 frpc          # 用 frp 时
```

常见原因：SSO token 失效（重新登录 Kiro）、`PROFILE_ARN` 写错、AWS 网络不通。

**frpc 连不上 frps**

进 `frpc` 日志看报错：

```bash
docker compose logs --tail=200 frpc
```

常见原因：`FRP_SERVER_ADDR`/`FRP_SERVER_PORT` 写错、frps 的 `bindPort` 未放行、`FRP_TOKEN` 与 frps.toml 的 `auth.token` 不一致、`FRP_REMOTE_PORT` 在 frps 上未放行或被占用。

**SSO token 过期**

宿主机重新跑 Kiro 登录流程刷新 `~/.aws/sso/cache/kiro-auth-token.json`，容器是只读挂载，会自动看到新 token。

### 注意事项

- `.env` 永远不要提交到 git，模板见 `.env.example`
- `PROXY_API_KEY` 建议用 `openssl rand -hex 32` 生成强随机串，不要用弱密码
- 镜像 tag 已 pin，不会自动升级；升级步骤见「升级」
- `/usage` 和聊天接口共用 `PROXY_API_KEY`，也会经隧道（cloudflared / frpc）暴露到公网
- frps 纯 TCP 直出走的是明文 `http`，`PROXY_API_KEY` 会明文传输；介意的话用域名 + HTTPS（在 frps 侧挂 Caddy/Nginx 反代签证书），并给 frpc ↔ frps 设 `FRP_TOKEN`
- 启用本方案后，Cursor 里的官方 OpenAI 模型（GPT 系列）将无法使用，详见文首「重要限制」

## 致谢

- [jwadow/kiro-gateway](https://github.com/jwadow/kiro-gateway) — 上游网关
- [hank9999/kiro.rs](https://github.com/hank9999/kiro.rs) — `getUsageLimits` 接口调用参考
