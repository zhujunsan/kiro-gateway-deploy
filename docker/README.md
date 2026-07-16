# Docker 部署（kiro-gateway + Cloudflare Tunnel）

用 Docker Compose 把 kiro-gateway 跑成长驻容器，并经 Cloudflare Tunnel 暴露成公网
`https://<your-domain>/v1` 供 Cursor 使用。

> 这是仓库早期的部署方式，功能仍然保留。如果你只是想在自己机器上用，推荐改用原生托盘
> App（见[仓库根目录 README](../README.md)），无需 Docker、无需自己的域名。

本目录文件：

- `docker-compose.yml` — 两个服务：`kiro-gateway`（网关）+ `cloudflared`（隧道）
- `.env.example` — 环境变量模板
- `.env` — 你的实际密钥（已 gitignore，不进库）

## 前置条件

1. **Docker**：Mac 上推荐 [OrbStack](https://orbstack.dev/)（轻量替代 Docker Desktop），
   自带 `docker` 和 `docker compose`。
2. **Kiro 凭据**：本机已完成 Kiro SSO 登录，确保 `~/.aws/sso/cache/kiro-auth-token.json`
   存在（打开 Kiro IDE 登录一次即可）。容器以只读方式挂载这个缓存目录。
3. **Cloudflare Tunnel**：一个托管在 Cloudflare 的域名（域名可在别处买，改 NS 指到
   Cloudflare 免费版即可）。

## 准备 Cloudflare Tunnel

去 [Cloudflare Zero Trust](https://one.dash.cloudflare.com/) 控制台：

1. 左侧 Networks → Tunnels → Create a tunnel
2. 选 Cloudflared，取个名字，创建后会给你一个 tunnel token（`eyJ...` 开头的长串）
3. 在 Public Hostname 里添加一条：域名指向 `http://kiro-gateway:8000`（compose 内部服务名）

记下 tunnel token，填到 `.env` 的 `CLOUDFLARED_TOKEN`。最终 Base URL 是
`https://<your-cloudflare-domain>/v1`。

## 配置并启动

在本目录（`docker/`）下操作：

```bash
cp .env.example .env
open -e .env            # 或用你习惯的编辑器
```

至少填这三项：

```
PROFILE_ARN=arn:aws:codewhisperer:us-east-1:<account>:profile/<id>
PROXY_API_KEY=<给客户端用的鉴权 key，建议 openssl rand -hex 32 生成>
CLOUDFLARED_TOKEN=<上一步拿到的 tunnel token>
```

启动：

```bash
docker compose up -d
docker compose logs kiro-gateway | tail -20   # 应看到 uvicorn 监听 :8000、healthcheck 通过
```

## 配置 Cursor

Cursor Settings → Models → OpenAI API Key & Base URL：

- **API Key**：`.env` 里的 `PROXY_API_KEY`
- **Base URL**：`https://<your-cloudflare-domain>/v1`

可用模型别名见[根目录 README 的「可用模型」](../README.md#可用模型)。

## 常用命令

```bash
docker compose up -d                 # 启动
docker compose down                  # 停止并移除容器
docker compose restart kiro-gateway  # 重启网关
docker compose logs -f kiro-gateway  # 跟随网关日志
docker compose logs -f cloudflared   # 跟随隧道日志
docker compose ps                    # 查看健康状态
```

## 环境变量

`.env` 中的变量（完整针对 Cursor 的调优项见 `.env.example` 内注释）：

| 变量 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `PROFILE_ARN` | 是 | — | CodeWhisperer profile ARN |
| `PROXY_API_KEY` | 是 | — | 客户端调用网关时的 Bearer token |
| `CLOUDFLARED_TOKEN` | 是 | — | Cloudflare Tunnel token |
| `KIRO_GATEWAY_PORT` | 否 | `18000` | 宿主机暴露端口 |
| `KIRO_API_REGION` | 否 | `us-east-1` | Kiro / CodeWhisperer 区域 |
| `MODEL_DISCOVERY_CACHE_TTL_SECONDS` | 否 | `14400` | 模型列表按需发现节流窗口（每账号 4 小时） |
| `TUNNEL_TRANSPORT_PROTOCOL` | 否 | `http2` | Cloudflare 隧道传输协议 |
| `FAKE_REASONING` | 否 | `false` | 是否注入伪造思考标签 |
| `AUTO_TRIM_PAYLOAD` | 否 | `true` | 超限时自动裁剪请求体 |
| `KIRO_MAX_PAYLOAD_BYTES` | 否 | `600000` | 请求体上限字节数（硬限约 615KB） |
| `TRUNCATION_RECOVERY` | 否 | `true` | 响应被截断时自动通知模型续写 |
| `WEB_SEARCH_ENABLED` | 否 | `false` | 是否自动注入 web_search 工具 |
| `FIRST_TOKEN_TIMEOUT` | 否 | `30` | 等待首个 token 的超时秒数 |
| `STREAMING_READ_TIMEOUT` | 否 | `300` | 流式分块间的读超时秒数 |

模型发现只由 `GET /v1/models` 或 `GET /v1/responses/models` 触发；生成请求继续走 runtime endpoint。首次发现失败会缓存 fallback，动态刷新失败会保留 stale，两种失败都在随后 4 小时内不再请求上游。

## 升级镜像

镜像在 `docker-compose.yml` 里被刻意 pin 到某个 `main-<sha>`，不会自动升级。

- **只拿 fork 的新构建**：fork 仓库 push 后 CI 产出新的 `main-<sha>` tag，把
  `docker-compose.yml` 的 `image:` 改成新 tag，`docker compose down && docker compose up -d`。
- **同步上游新版本**：在 fork（`zhujunsan/kiro-gateway`）把上游
  `jwadow/kiro-gateway` 的 main rebase/merge 进来，推送后 CI 出新 tag，再改 `image:`。

## 故障排查

```bash
docker compose ps
docker compose logs --tail=200 kiro-gateway
docker compose logs --tail=200 cloudflared
```

常见原因：SSO token 失效（重新登录 Kiro 刷新 `~/.aws/sso/cache/kiro-auth-token.json`，
容器只读挂载会自动看到新 token）、`PROFILE_ARN` 写错、AWS 网络不通。

## 注意事项

- `.env` 永远不要提交到 git，模板见 `.env.example`
- `PROXY_API_KEY` 建议用 `openssl rand -hex 32` 生成强随机串
- `/usage` 和聊天接口共用 `PROXY_API_KEY`，也会经隧道暴露到公网
- GPT 系列使用真实模型名并由网关 fallback 暴露，无需配置 alias 或手动增加 model name；
  Claude 模型仍使用根目录 README 列出的 `kiro-*` 别名。
