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

1. **需要公网地址**：Cursor 会先把请求发回自己的服务器，再转发到你指定的目标地址——所以本地部署的服务 Cursor 根本到不了。本项目用 Cloudflare Tunnel 把网关暴露到公网。

2. **只能走 OpenAI 兼容协议，且 `claude-*` 模型名有特殊处理**：Cursor 只允许自定义 OpenAI 地址，不能自定义 Anthropic 地址。而且对 `claude` 开头的模型名做了特殊路由——即使你把它加到自定义模型列表里，请求仍然不走你的 OpenAI 兼容地址。所以需要给模型起别名（`kiro-sonnet-4.6`、`kiro-opus-4.6` 等），让 Cursor 认为这是一个未知模型，走你的自定义地址。另外上游 `ListModels` 里没有 opus 4.8，也顺手补上了。

3. **用量查询**：用了这个以后就不直接用 Kiro 客户端了，看不到额度消耗。所以加了一个 `GET /usage` 端点，能随时查订阅用量。

## 快速开始

### 1. 安装 Docker

Mac 上推荐 [OrbStack](https://orbstack.dev/)（轻量替代 Docker Desktop），安装后开箱即用，自带 `docker` 和 `docker compose` 命令。

### 2. 准备 Cloudflare Tunnel

需要一个公网入口。去 [Cloudflare Zero Trust](https://one.dash.cloudflare.com/) 控制台：

1. 左侧 Networks → Tunnels → Create a tunnel
2. 选 Cloudflared，取个名字，创建后会给你一个 tunnel token（`eyJ...` 开头的长串）
3. 在 Public Hostname 里添加一条：域名指向 `http://kiro-gateway:8000`（这是 compose 内部的服务名）

记下 tunnel token，后面要用。

### 2. 准备 Kiro 凭据

本机需要已完成 Kiro SSO 登录，确保 `~/.aws/sso/cache/kiro-auth-token.json` 存在。直接打开 Kiro IDE 登录一次即可。

### 3. 配置并启动

```bash
git clone https://github.com/zhujunsan/kiro-gateway-deploy.git
cd kiro-gateway-deploy
cp .env.example .env
open -e .env
```

填入三项：

```
PROFILE_ARN=arn:aws:codewhisperer:us-east-1:<account>:profile/<id>
PROXY_API_KEY=<给客户端用的鉴权 key，自己生成一个强随机串>
CLOUDFLARED_TOKEN=<上一步拿到的 tunnel token>
```

启动：

```bash
docker compose up -d
```

查看日志确认 patch 成功：

```bash
docker compose logs kiro-gateway | grep '\[ok\]'
```

正常应输出三行 `[ok] patched ...`。

### 4. 配置 Cursor

Cursor Settings → Models → OpenAI API Key & Base URL：

- **API Key**: 填你 `.env` 里设的 `PROXY_API_KEY`
- **Base URL**: `https://<your-cloudflare-domain>/v1`

然后在模型列表里添加你想用的别名（见下表），就可以在 Cursor 里选这些模型了。

### 5. 验证

```bash
curl -H "Authorization: Bearer $PROXY_API_KEY" \
     https://<your-cloudflare-domain>/v1/models
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

要修改别名，编辑 `patches/apply_aliases.py` 中的 `EXTRA_ALIASES`，然后 `docker compose down && docker compose up -d`。

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

本项目基于 [`ghcr.io/jwadow/kiro-gateway`](https://github.com/jwadow/kiro-gateway) 镜像，启动时通过两个 Python 脚本 patch 容器内的源码：

1. `patches/apply_aliases.py` — 注入 `kiro-*` 模型别名到 `config.py` 和 `model_resolver.py`
2. `patches/add_usage_endpoint.py` — 注入 `GET /usage` 端点到 `main.py`

Patch 是幂等的（有 sentinel 标记），`restart` 不会重复打，但修改 patch 脚本后必须 `down && up` 重建容器。

### 目录结构

```
.
├── docker-compose.yml
├── .env                        # 实际密钥（已 gitignore）
├── .env.example                # 占位模板
├── .gitignore
├── README.md
├── tools/
│   └── check_usage.py          # 独立脚本：不依赖容器，直接读 token 查额度
└── patches/
    ├── apply_aliases.py        # 启动时 patch：注入 kiro-* 模型别名
    └── add_usage_endpoint.py   # 启动时 patch：注入 GET /usage 端点
```

### 配置项

`.env` 中所有变量

| 变量 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `PROFILE_ARN` | 是 | — | CodeWhisperer profile ARN |
| `PROXY_API_KEY` | 是 | — | 客户端调用网关时的 Bearer token |
| `CLOUDFLARED_TOKEN` | 是 | — | Cloudflare Tunnel token |
| `KIRO_GATEWAY_PORT` | 否 | `18000` | 宿主机暴露端口 |
| `KIRO_API_REGION` | 否 | `us-east-1` | Kiro / CodeWhisperer 区域 |
| `TUNNEL_TRANSPORT_PROTOCOL` | 否 | `http2` | Cloudflare 隧道传输协议 |

### 常用命令

```bash
docker compose up -d                 # 启动
docker compose down                  # 停止并移除容器
docker compose restart kiro-gateway  # 重启网关（不重跑 patch）
docker compose logs -f kiro-gateway  # 跟随网关日志
docker compose logs -f cloudflared   # 跟随隧道日志
docker compose ps                    # 查看健康状态
```

> 镜像 tag 已在 `docker-compose.yml` 里固定（pin 到某个 `main-<sha>`），所以 `docker compose pull` 拿到的还是同一个版本，不会自动升级。升级方法见下方「升级上游镜像」。

### 升级上游镜像

镜像被刻意 pin 在 `docker-compose.yml` 里的 `main-<sha>`，因为 `patches/` 下的补丁是针对这个特定构建验证过的。直接升到新版可能让 patch 锚点失效、容器起不来（这是预期的「响亮失败」——patch 对不上会直接 `sys.exit`，不会静默跑错）。

升级步骤：

1. 在 [上游 commits](https://github.com/jwadow/kiro-gateway/commits/main) 找到想升级到的提交，取它的短 sha，对应镜像 tag 即 `main-<短sha>`。
2. 改 `docker-compose.yml` 里的 `image:` 为新 tag，`docker compose down && docker compose up -d` 重建。
3. 看日志确认三条 patch 都 `[ok]`：

   ```bash
   docker compose logs -f kiro-gateway
   ```

   若出现 `MODEL_ALIASES not found` / `get_model_id_for_kiro body not found` / `app.include_router not found`，说明上游结构变了，按「故障排查」对应条目调整 patch 脚本的锚点后再重建。

### 故障排查

**`config.py: MODEL_ALIASES not found`**

上游镜像结构变了。进容器确认：

```bash
docker compose run --rm --entrypoint python kiro-gateway -c "import kiro.config; print([k for k in dir(kiro.config) if 'ALIAS' in k])"
```

**`model_resolver.py: get_model_id_for_kiro body not found`**

上游改了函数体。进容器确认：

```bash
docker compose run --rm --entrypoint python kiro-gateway -c "import inspect, kiro.model_resolver as m; print(inspect.getsource(m.get_model_id_for_kiro))"
```

**`main.py: app.include_router not found`**

上游改了 `main.py` 结构。进容器确认：

```bash
docker compose run --rm --entrypoint python kiro-gateway -c "import main; print([r.path for r in main.app.routes])"
```

**healthcheck 不过 / cloudflared 起不来**

```bash
docker compose ps
docker compose logs --tail=200 kiro-gateway
```

常见原因：SSO token 失效（重新登录 Kiro）、`PROFILE_ARN` 写错、AWS 网络不通。

**SSO token 过期**

宿主机重新跑 Kiro 登录流程刷新 `~/.aws/sso/cache/kiro-auth-token.json`，容器是只读挂载，会自动看到新 token。

### 注意事项

- `.env` 永远不要提交到 git，模板见 `.env.example`
- `PROXY_API_KEY` 建议用 `openssl rand -hex 32` 生成强随机串，不要用弱密码
- patch 脚本是幂等的，`restart` 不会重复打补丁，但修改 patch 后必须 `down && up`
- 镜像 tag 已 pin，不会自动升级；升级步骤见「升级上游镜像」
- `/usage` 和聊天接口共用 `PROXY_API_KEY`，也会经 cloudflared 暴露到公网
- 启用本方案后，Cursor 里的官方 OpenAI 模型（GPT 系列）将无法使用，详见文首「重要限制」

## 致谢

- [jwadow/kiro-gateway](https://github.com/jwadow/kiro-gateway) — 上游网关
- [hank9999/kiro.rs](https://github.com/hank9999/kiro.rs) — `getUsageLimits` 接口调用参考
