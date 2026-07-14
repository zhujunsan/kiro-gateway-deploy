# kiro-provision Worker

## 首次部署

1. `npm install -g wrangler && wrangler login`
2. 复制 `secrets.json.example` 为 `secrets.json`，把值填好，然后 `wrangler secret bulk secrets.json` 一次性导入
3. `wrangler deploy`

## Secrets 清单

下表即 `secrets.json` 需要填的字段（`secrets.json.example` 是模板）：

| Secret | 说明 |
|---|---|
| SHARED_SECRET | 发给用户的一次性激活码，泄露了重新设一个即可。可用 `openssl rand -hex 16` 生成 |
| CF_API_TOKEN | Custom Token：Tunnel:Edit + DNS:Edit(example.com) |
| CF_ACCOUNT_ID | Cloudflare Account ID |
| CF_ZONE_ID | example.com 的 Zone ID |
| DOMAIN_SUFFIX | example.com |
| HOSTNAME_PREFIX | kg |
| TELEMETRY_SECRET | 客户端上报 `/telemetry` 用的预共享密钥，独立于 SHARED_SECRET。可用 `openssl rand -hex 32` 生成 |

### 可选 Secrets/Vars

| Secret | 说明 |
|---|---|
| IDLE_CLEANUP_DAYS | 闲置隧道清理阈值（天）。设为正整数后 cron 会自动清理超过该天数未连接的隧道及对应 DNS 记录。未配置则不清理，安全默认 |

## 更新 SHARED_SECRET（换批用户时）

```bash
wrangler secret put SHARED_SECRET   # 可用 openssl rand -hex 16 生成一个新激活码
wrangler deploy
```

## 遥测（Telemetry）

遥测复用本 worker（方案 A），新增三块能力（详见 `docs/2026-06-25-telemetry-design.md`）：

- `POST /telemetry`：用 `Authorization: Bearer <TELEMETRY_SECRET>` 鉴权（恒定时间比较），把上报的桶写入 D1 `usage_rollup`（`ON CONFLICT DO UPDATE` 覆盖，last-write-wins）。返回 `{ ok, accepted }`。行内可选 `estimated_credits` / `credit_estimate_*`（旧客户端缺省为 `NULL`=未知；显式 `0`=测得零消耗）。
- `POST /telemetry/errors`：同一 Bearer 鉴权。每次请求只收一条 `record`（`manifest` 或 `artifact_chunk`），校验后写入 **一条** `console.error` 到 Workers Logs（不写 D1）。单条序列化上限 192 KiB，超出返回 413。查询时按 `kind=kiro_gateway_incident` / `incident_id` / `source` / `code` 过滤；`artifact_chunk` 用 `part_id`/`part_index` 去重重组。**日志可能含完整请求/响应正文，请收紧 Cloudflare 账号访问权限。**
- `GET|POST /q/<name>`：只读查询，仅开放写死的参数化固定查询（`daily-by-user` / `model-distribution` / `active-users` / `user-totals`），默认查 `usage_daily`，结果缓存 60 分钟。**`/q/*` 不在 worker 内校验密钥，由 Cloudflare Access 在边缘保护**。
- `scheduled()`（cron）：每小时把 `usage_rollup` 卷成 `usage_daily`（按天 × user × model 聚合，含 `estimated_credits` SUM，幂等可重入）。

线上已有库加列：

```bash
wrangler d1 execute kiro-telemetry --remote --file=./migrations/2026-07-14-estimated-credits.sql
```

部署时请在 `wrangler.toml` 打开 `[observability]`（见 `wrangler.toml.sample`）：`enabled=true`、`head_sampling_rate=1`、`invocation_logs=false`。

### 密钥分发与轮换

`TELEMETRY_SECRET` 不写死在客户端，分发与轮换通过两个变化完成（设计文档第八节）：

- **首次下发（provision 附带）**：`/provision` 成功响应额外带 `telemetry_secret`（值取 `env.TELEMETRY_SECRET`；未配置则省略该字段，不影响隧道创建）。客户端写入本地 config。
- **刷新（/telemetry-secret）**：客户端上报 `/telemetry` 收到 401（本地密钥过期）后，调 `POST /telemetry-secret` 拉最新密钥。该端点用激活码 `shared_secret`（body 内，恒定时间比较）鉴权而非 `TELEMETRY_SECRET`（否则会死锁），成功返回 `{ telemetry_secret }`；**只读 env 返回密钥，绝不创建/删除/修改任何 tunnel 或 DNS**——与 `/provision` 的幂等重建彻底分离，不会断连。`TELEMETRY_SECRET` 未配置时返回 500 `{error:"telemetry not configured"}`。
- 轮换运维：`wrangler secret put TELEMETRY_SECRET` + `wrangler deploy` 即可，客户端无需发版（下次 401 后自动经 `/telemetry-secret` 拉到新值）。

### 部署提示

```bash
cd worker
# 1) 创建 D1 并建表
wrangler d1 create kiro-telemetry           # 把输出的 database_id 填进 wrangler.toml
wrangler d1 execute kiro-telemetry --remote --file=./schema.sql

# 2) 设置上报密钥（也可一并写进 secrets.json 用 bulk 导入）
wrangler secret put TELEMETRY_SECRET        # openssl rand -hex 32

# 3) 部署（wrangler.toml 已含 [[d1_databases]] 与 [triggers] crons）
wrangler deploy
```

### 保护查询路径 /q

在 Zero Trust 控制台给 `kiro-gateway-provision.<域名>` 的 **Path `/q`** 加一条 self-hosted application + Service Auth 策略，签发 Service Token，把 `CF-Access-Client-Id/Secret` 填进 Grafana Infinity datasource。`/telemetry` 与 `/provision` 不受此 Access 影响（靠 path 限定）。

## 注意事项

- run_token 只在 201 响应里返回一次，Worker 本身不存储任何状态（Cloudflare API 是唯一数据源）
- 吊销某用户：在 Zero Trust 控制台删 tunnel + DNS 记录即可；也可配置 `IDLE_CLEANUP_DAYS` 让 cron 自动回收长期不活跃的隧道
- CF_API_TOKEN 永远不要提交到 git，只通过 `wrangler secret put` 存入

## 闲置隧道自动清理

设置 `IDLE_CLEANUP_DAYS`（正整数，单位天）后，每小时 cron 会：

1. 列出账号下所有未删除隧道，过滤出 `HOSTNAME_PREFIX-` 前缀的（本项目签发的）
2. 跳过 `status=healthy` 或正在有活跃连接的隧道
3. 闲置时长 = `now - conns_inactive_at`（若无连接记录则用 `created_at`）
4. 超过阈值的：删除对应 DNS CNAME + 删除 tunnel 本身

审计日志通过 `console.log` 输出到 Worker Logs（Cloudflare Dashboard → Workers → Logs），每条记录被清理的隧道名和闲置天数。

客户端侧：隧道被清理后，下次启动时会自动检测到隧道不存在并静默重建（使用本地持久化的激活码），用户无感。

## 隧道状态查询 `/tunnel-status`

供客户端判断云端隧道是否仍存在（只读，绝不修改 tunnel/DNS）：

```
POST /tunnel-status
body: { shared_secret, username }
→ 200 { exists: true/false }
→ 401 { error: "unauthorized" }
```
