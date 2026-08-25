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

- `POST /telemetry`：用 `Authorization: Bearer <TELEMETRY_SECRET>` 鉴权（恒定时间比较），把上报的桶写入 D1 `usage_rollup`（`ON CONFLICT DO UPDATE` 覆盖，last-write-wins）。返回 `{ ok, accepted }`。行内可选 `estimated_credits` / `credit_estimate_*`（旧客户端缺省为 `NULL`=未知；显式 `0`=测得零消耗）；可选 `ttft_*` / `generation_*`（旧客户端缺省为 `0`；平均 TTFT = sum/count，token/s = generation_completion_tokens_sum / (generation_ms_sum/1000)，生成窗仅流式）。
- `GET|POST /q/<name>`：只读查询，仅开放写死的参数化固定查询（`daily-by-user` / `model-distribution` / `active-users` / `user-totals`），默认查 `usage_daily`，结果缓存 60 分钟。**`/q/*` 不在 worker 内校验密钥，由 Cloudflare Access 在边缘保护**。
- `scheduled()`（cron）：每小时把 `usage_rollup` 卷成 `usage_daily`（按天 × user × model 聚合，含 `estimated_credits` SUM，幂等可重入）。

> 网关失败请求的 debug 抓包（请求体 / 响应流等）已改走 Sentry，不再经本 Worker 的 `/telemetry/errors`。

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

## 公告栏（Announcements）

托盘菜单顶部、"新版提醒"下方最多展示 5 条公告。客户端启动时拉一次，之后每小时一次
（菜单打开也会触发，但受同一个 1 小时 TTL 约束）。

```
POST /announcements
headers: { User-Agent: "KiroGatewayTray/<version> (<platform>)" }
body: { shared_secret, username }
# body.app_version / body.platform 仅作 curl 调试兜底；正式客户端走 UA。
→ 200 { ok: true, announcements: [ { id, body, tag, url, level, priority, dimmed, ends_at } ] }
→ 401 { error: "unauthorized" }
```

鉴权与 `/tunnel-status` 一致（body 内激活码，恒定时间比较），所以公告内容不对公网公开，
客户端也不用再存一份新密钥。

### 建表

```bash
# 已有库加表（或 DROP 后重建）
wrangler d1 execute kiro-telemetry --remote --file=./migrations/2026-08-03-announcements.sql
# 新库由 schema.sql 一并建出
```

### 发布 / 修改公告

没有 admin API —— 直接写 D1。`announcements.example.sql` 是带注释的模板，涵盖定向、
自动上下架和日常增删改查：

```bash
wrangler d1 execute kiro-telemetry --remote \
  --command "INSERT INTO announcements (body) VALUES ('欢迎使用')"
# 发新后 SELECT id 回看；改/下架用 WHERE id = N
```

### 字段与定向语义

| 字段 | 作用 |
|---|---|
| `id` | 自增主键。INSERT 不写；改/下架用 `WHERE id = N` |
| `body` | 菜单行文字。客户端会把换行折成空格并截断到 120 字，长内容请放 `url` |
| `tag` | 行尾灰色小字（macOS 右对齐）；其他平台显示为普通后缀 |
| `url` | 点击跳转。为空时点击无反应；**是否置灰由 `dimmed` 决定，与有无 url 无关**。只接受 http(s) |
| `level` | `info` / `warning` / `critical` → 行首 📢 / ⚠️ / 🚨 |
| `priority` | 排序权重，大的在前；同权重时 `starts_at` 新的在前 |
| `dimmed` | `1` = 菜单行置灰；`0` = 正常色。与有无链接无关 |
| `enabled` | 手动总开关，置 0 立刻下架 |
| `starts_at` / `ends_at` | Unix 秒（UTC），起点含、终点不含；NULL 表示该侧不限 |
| `min_version` / `max_version` | 版本闭区间 |
| `target_platforms` | `macos` / `windows` / `linux`，逗号分隔；空 = 全平台 |

匹配全部 **fail-closed**：设了 `min_version` 但客户端没上报版本、设了 `target_platforms`
但平台未知、版本区间写成非法字符串 —— 这些情况一律不展示。宁可少发一条，也不要发错人。

### 生效延迟

改完 D1 后，用户看到变化最多需要约 **1 小时 5 分钟**：Worker 对 D1 行有 5 分钟边缘缓存
（查的是 `enabled = 1` 且未过期的行；读次数与在线人数无关），客户端每小时才轮询一次。急事请配合直接通知。

排查时可以直接打端点验证 Worker 侧的判定：

```bash
curl -s https://kiro-gateway-provision.<域名>/announcements \
  -H 'Content-Type: application/json' \
  -H 'User-Agent: KiroGatewayTray/0.4.22 (macos)' \
  -d '{"shared_secret":"<激活码>","username":"<匿名哈希>"}'
```

## 本地测试

```bash
cd worker && node --test    # 无依赖，纯 node:test
```

## 注意事项

- run_token 只在 201 响应里返回一次，Worker 本身不存储任何状态（Cloudflare API 是唯一数据源）
- 吊销某用户：在 Zero Trust 控制台删 tunnel + DNS 记录即可；也可配置 `IDLE_CLEANUP_DAYS` 让 cron 自动回收长期不活跃的隧道
- CF_API_TOKEN 永远不要提交到 git，只通过 `wrangler secret put` 存入

## 闲置隧道自动清理

设置 `IDLE_CLEANUP_DAYS`（正整数，单位天）后，每小时 cron 会：

1. 列出账号下所有未删除隧道，过滤出 `HOSTNAME_PREFIX-` 前缀的（本项目签发的）
2. 跳过仍在服务的隧道：`status=healthy` **或** `status=degraded`（degraded 仍能打到边缘，只是 HA 不齐）
3. 闲置时长只用 `conns_inactive_at`。**不会**用 `created_at` 去判断一条曾经上线过的隧道——否则一次 cloudflared 重连闪断（`status=down`）会把开了几个月的在线用户当成「闲置 30 天」并删掉 CNAME
4. 从未跑过的 `inactive` 隧道才用 `created_at` 作为闲置起点
5. 超过阈值的：先删 tunnel，成功后再删 DNS
6. 随后给**剩下的**隧道补 proxied CNAME（`/reconcile-dns`，cron 也会跑），避免再出现「隧道还在、域名没有」

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
