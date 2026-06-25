# Cloudflare 配置操作手册

本文档面向**管理员（你）**，一步步说明把签发服务（Worker）跑起来需要在 Cloudflare 做哪些操作。普通用户不需要看这篇——他们只需要拿到你给的「激活码」和 Worker 地址。

## 它是怎么运作的（先理解再操作）

```
用户 App ──POST /provision (激活码 + 用户名)──▶  Cloudflare Worker
                                                    │
                                   用 CF API 自动做三件事：
                                   1. 建一条 cloudflared tunnel
                                   2. 配 ingress → http://localhost:64005
                                   3. 建 proxied CNAME: kg-<用户名>.example.com
                                                    │
        ◀──返回 { hostname, run_token }─────────────┘
        App 把 run_token 写进本地 config，启动 cloudflared，
        本机网关就暴露成 https://kg-<用户名>.example.com/v1
```

你要做的就是把这个 Worker 部署到自己的 Cloudflare 账号，并给它配好凭据。一次性操作，之后只在换激活码时动一下。

## 前提

- 一个 Cloudflare 账号
- 一个已经托管在该账号下的域名（zone）。本项目默认用 `example.com`，换成你自己的域名时需要同步改 `worker/wrangler.toml`、`DOMAIN_SUFFIX` 两处（见最后「换自己的域名」）。
- 本机装好 Node.js（用来跑 wrangler CLI）

---

## 第 1 步：安装并登录 wrangler

```bash
npm install -g wrangler
wrangler login
```

`wrangler login` 会打开浏览器让你授权，登录到你的 Cloudflare 账号。

## 第 2 步：创建 KV namespace

Worker 用 KV 记录「哪个用户已经注册过」，做幂等（同一用户重复请求不会重复建 tunnel）。

```bash
wrangler kv namespace create PROVISION_KV
```

命令会输出一段，里面有 `id = "xxxxxxxx"`。把这个 id 填进 `worker/wrangler.toml`：

```toml
[[kv_namespaces]]
binding = "PROVISION_KV"
id = "<这里填上一步拿到的 id>"
```

## 第 3 步：收集两个 ID

后面设 secrets 要用到 Account ID 和 Zone ID。

**Account ID：**
- 登录 Cloudflare 控制台 → 右侧栏 / 任意域名的 Overview 页右下角能看到 **Account ID**
- 或命令行：`wrangler whoami`

**Zone ID：**
- 控制台选中你的域名（`example.com`）→ Overview 页右下角 **Zone ID**

## 第 4 步：创建 scoped API Token

Worker 要用 Cloudflare API 建 tunnel 和 DNS 记录，需要一个 **API Token**（不是 Global API Key，权限要收窄）。

1. 控制台右上角头像 → **My Profile** → **API Tokens** → **Create Token**
2. 选 **Create Custom Token**
3. 权限（Permissions）加两条：
   - **Account** → **Cloudflare Tunnel** → **Edit**
   - **Zone** → **DNS** → **Edit**
4. 资源范围（限定到你的域名，别给全账号）：
   - **Account Resources**：Include → 你的账号
   - **Zone Resources**：Include → Specific zone → `example.com`
5. 创建后**复制 token 字符串**（只显示一次，丢了就重建）

> 为什么要收窄：这个 token 存在 Worker 里，万一泄露，攻击者最多能动你这个域名的 tunnel 和 DNS，碰不到账号其他资源。

## 第 5 步：设置 Worker Secrets

复制模板，填好值，一次性批量导入。在 `worker/` 目录下：

```bash
cd worker
cp secrets.json.example secrets.json   # 然后编辑 secrets.json，填进下表各值
wrangler secret bulk secrets.json      # 一次性导入所有 secret
```

> `secrets.json` 含真实密钥，已在 `.gitignore` 里，不会进 git；`secrets.json.example` 是不含真实值的模板。

各 secret 的作用：

| Secret | 说明 |
|---|---|
| `SHARED_SECRET` | 发给用户的激活码。用户激活时填的就是它。泄露了重设一个再 deploy 即可。可用 `openssl rand -hex 16` 生成 |
| `CF_API_TOKEN` | 第 4 步的 scoped token，Worker 用它调 CF API |
| `CF_ACCOUNT_ID` | 你的 Cloudflare Account ID |
| `CF_ZONE_ID` | `example.com` 的 Zone ID |
| `DOMAIN_SUFFIX` | 域名后缀，最终地址 = `kg-<用户名>.<DOMAIN_SUFFIX>` |
| `HOSTNAME_PREFIX` | 主机名前缀，默认 `kg` |

> Secrets 通过 `wrangler secret bulk` 存进 Cloudflare，**不会**出现在代码或 git 里。`CF_API_TOKEN` 永远不要写进任何会进 git 的文件。

## 第 6 步：部署 Worker

```bash
wrangler deploy
```

`wrangler.toml` 里配了 `custom_domain = true`，部署时 wrangler 会**自动**在 `example.com` 这个 zone 里建好路由和代理 DNS 记录，把 Worker 绑到：

```
https://kiro-gateway-provision.example.com
```

这个地址就是用户要填进 App `config.toml` 的 `provision_url`。

## 第 7 步：验证 Worker 正常

```bash
# 错误的激活码 → 应返回 401
curl -s -X POST https://kiro-gateway-provision.example.com/provision \
  -H "Content-Type: application/json" \
  -d '{"shared_secret":"wrong","username":"test"}'
# 期望: {"error":"unauthorized"}

# 正确的激活码 → 应返回 201 + hostname + run_token
curl -s -X POST https://kiro-gateway-provision.example.com/provision \
  -H "Content-Type: application/json" \
  -d '{"shared_secret":"<你设的 SHARED_SECRET>","username":"test"}'
# 期望: {"hostname":"kg-test.example.com","run_token":"eyJ..."}
```

验证完记得清理这条测试用户（见下面「运维」）。

---

## 日常运维

### 发激活码给新用户

用户激活时需要两样东西，你发给他：
1. **激活码** = 你设的 `SHARED_SECRET`
2. **Worker 地址** = `https://kiro-gateway-provision.example.com`（填进 App 的 `provision_url`）

用户在 App 里填好 `provision_url`、输入激活码、再选一个用户名（小写字母数字+连字符，1-32 位），App 会自动完成注册。

### 换一批激活码

```bash
cd worker
wrangler secret put SHARED_SECRET   # 输入新激活码（可用 openssl rand -hex 16 生成）
wrangler deploy
```

旧激活码立即失效，已注册用户不受影响（他们的 run_token 已经在本地了）。

### 查看 / 管理已注册用户

```bash
# 列出所有已注册用户
wrangler kv key list --namespace-id <KV_NAMESPACE_ID>

# 看某个用户的信息
wrangler kv key get --namespace-id <KV_NAMESPACE_ID> "user:alice"

# 删掉测试用户的 KV 记录（注意：这只删记录，不删 tunnel/DNS，见下）
wrangler kv key delete --namespace-id <KV_NAMESPACE_ID> "user:test"
```

### 吊销某个用户

KV 记录删掉只是让该用户名能重新注册，**不会**关掉已经建好的 tunnel。要彻底吊销：

1. Cloudflare Zero Trust 控制台 → **Networks** → **Tunnels** → 删掉 `kg-<用户名>` 这条 tunnel
2. DNS 设置里手动删掉 `kg-<用户名>.example.com` 这条 CNAME 记录
3. （可选）删掉 KV 里 `user:<用户名>` 记录

---

## 关于安全的几点

- **run_token 只在首次注册时返回一次**，KV 里不保存它，避免 KV 变成 token 仓库。用户重复请求只会拿到 hostname，run_token 为 null。
- **run_token 是窄权限凭据**：它只能运行那一条 tunnel 的连接器，无法访问你的 Cloudflare 账号、改 DNS 或看别的 tunnel。发给用户是安全的。
- **CF_API_TOKEN 是高权限凭据**：它能建 tunnel / 改 DNS，只存在 Worker secret 里，绝不能泄露或进 git。

---

## 遥测（Telemetry）部署

这一章是在上面 provision 服务跑通之后的**增量部署**。遥测**复用同一个 `kiro-provision` worker**（方案 A：签发与遥测聚合在一个 worker 里），不新建 worker、不新建域名。新增的能力是：一个 D1 数据库 + 三条路由（`/telemetry` 上报、`/telemetry-secret` 刷新、`/q/*` 只读查询）+ 一个 cron 卷动任务，全部留在 **Free 额度**，不花钱。设计细节见 `docs/2026-06-25-telemetry-design.md`。

下面分两部分：**第一部分**是维护者已经执行过的步骤（用本项目的真实值，命令可直接复现）；**第二部分**是部署后仍需你/管理员手动完成的（设上报密钥、配 Access、接 Grafana）。

### 它新增了什么（先理解再操作）

```
客户端网关子进程 ──POST /telemetry (Bearer TELEMETRY_SECRET)──▶ worker ──▶ D1 (usage_rollup)
                                                                          │ 每小时 cron 卷动
                                                                   D1 (usage_daily 日聚合)
                                                                          │
Grafana(Infinity) ──CF-Access-Client-Id/Secret──▶ Cloudflare Access ──▶ worker /q/* (只读) ──▶ D1
```

三条路由的鉴权各不相同，部署前先记住这张矩阵：

| 端点 | 鉴权方式 | 谁来校验 |
|---|---|---|
| `POST /telemetry`（上报） | `Authorization: Bearer <TELEMETRY_SECRET>` | worker 代码（恒定时间比较） |
| `POST /telemetry-secret`（刷新密钥） | body 里的激活码 `shared_secret`（同 `/provision`） | worker 代码 |
| `GET\|POST /q/*`（查询） | Cloudflare Access Service Token（`CF-Access-Client-Id/Secret` 头） | Cloudflare 边缘（到不了 worker） |

> `/telemetry` 与 `/q/*` 在同一个 worker、同一个 `custom_domain`（`kiro-gateway-provision.botsonny.top`）下，靠 **path** 区分鉴权方式。`/telemetry-secret` 用激活码而不是 `TELEMETRY_SECRET`，是因为客户端来刷新的前提就是本地 `TELEMETRY_SECRET` 已失效（拿失效的密钥去鉴权会死锁）。

---

### 第一部分：维护者已执行的步骤（真实值，可复现）

下面命令在 `worker/` 目录下执行，按真实顺序排列。本项目实际用的值已写进命令里。

**1) 创建 D1 数据库**

```bash
cd worker
wrangler d1 create kiro-telemetry
```

本项目执行后拿到的库信息（已固化到 `wrangler.toml`，可照抄）：

- 数据库名：`kiro-telemetry`
- `database_id`：`0799e47a-3a75-47f6-8606-895b959569f2`
- region：`APAC`

> `database_id` **不是敏感信息**，可以写进文档和提交进 git——它只是数据库的定位 ID，没有它配套的账号凭据和 Access 也读不到数据。真正不能进 git 的是 `TELEMETRY_SECRET`、Access Service Token、`CF_API_TOKEN`。

**2) 把 `database_id` 与 cron 填进 `worker/wrangler.toml`**

这一步本项目已完成，`wrangler.toml` 现在长这样（D1 binding 名固定为 `TELEMETRY_DB`，worker 代码按这个名字取库）：

```toml
[[d1_databases]]
binding = "TELEMETRY_DB"
database_name = "kiro-telemetry"
database_id = "0799e47a-3a75-47f6-8606-895b959569f2"

[triggers]
crons = ["7 * * * *"]   # 每小时把 usage_rollup 卷成 usage_daily（错峰到第 7 分钟）
```

**3) 在远端 D1 建表**

```bash
wrangler d1 execute kiro-telemetry --remote --file=./schema.sql
```

`schema.sql` 建两张表：`usage_rollup`（客户端按 10 分钟桶上报的明细聚合）和 `usage_daily`（cron 每小时把 rollup 卷成「天 × user × model」，供看板默认查询、省读额度）。注意一定要带 `--remote`，否则只会建在本地模拟库里。

**4) 部署带新路由的 worker**

`src/index.js` 已实现 `/telemetry`、`/telemetry-secret`、`/q/*` 三条路由和处理 cron 卷动的 `scheduled()`。直接部署：

```bash
wrangler deploy
```

本项目部署成功的一个版本示例：`bad55b6f-6e38-4aab-b7f9-49ab98fd316b`（wrangler 4.104.0）。部署后路由就挂在 `https://kiro-gateway-provision.botsonny.top` 下，cron 也随之生效。

---

### 第二部分：你/管理员还需手动完成的

D1 和路由部署完，遥测**还跑不通**——上报会因为没设密钥而被拒、查询路径还没人保护。下面三件事需要手动做。

**A) 设置上报密钥 `TELEMETRY_SECRET`**

```bash
cd worker
wrangler secret put TELEMETRY_SECRET    # 提示后粘贴一个随机值
wrangler deploy
```

密钥值用 `openssl rand -hex 32` 生成一个即可：

```bash
openssl rand -hex 32
```

> 这个密钥独立于发给用户的激活码 `SHARED_SECRET`：激活码会随用户批次轮换，复用会导致换激活码时遥测一起断。`TELEMETRY_SECRET` 通过 `wrangler secret put` 存进 Cloudflare，**绝不写进任何会进 git 的文件**。客户端不需要你手动分发它：`/provision` 成功时响应会附带当前 `telemetry_secret`，客户端自动写进本地 config。

**B) 配 Cloudflare Access 保护查询路径 `/q`**

查询路由 `/q/*` 在 worker 里**不自校验**，靠 Cloudflare Access 在边缘挡住未授权请求。在 **Zero Trust 控制台**：

1. **Access → Applications → Add an application → Self-hosted**：
   - **Application domain** 填 `kiro-gateway-provision.botsonny.top`，**Path 限定为 `/q`**（只保护查询路径；`/telemetry` 和 `/provision` 不受影响）。
2. **Policy**：建一条策略，**Action = Service Auth**，规则匹配下一步要建的 Service Token。
3. **Access → Service Tokens → Create**：拿到一对 `Client-Id` 与 `Client-Secret`（**Secret 只显示一次**，当场存好）。
4. 把这对凭据交给 Grafana（见下一步）。

> 验证：不带 token 访问 `https://kiro-gateway-provision.botsonny.top/q/...` 应被 Access 拦在边缘（跳转登录页或 403，根本到不了 worker）；带正确 token 才放行。

**C) 接入 Grafana（概述）**

D1 没有原生 Grafana datasource，用 **Infinity 插件**走 HTTPS + Cloudflare Access：

- datasource 类型选 Infinity（JSON over HTTPS），`POST` 到 `https://kiro-gateway-provision.botsonny.top/q/<查询名>`。
- 在 datasource 的**自定义请求头**里填上一步拿到的 Service Token：
  - `CF-Access-Client-Id: <client-id>`
  - `CF-Access-Client-Secret: <client-secret>`
- 凭据存在 Infinity 的**加密字段**里，**不要**写进 panel 查询或导出的看板 JSON（导出即泄露）。
- worker 只开放写死的参数化固定查询（`daily-by-user` / `model-distribution` / `active-users` / `user-totals`），默认查 `usage_daily`，结果缓存 60 分钟，读额度稳在 Free 内。详见设计文档第十二节。

---

### 遥测的日常运维

**换 `TELEMETRY_SECRET`（轮换上报密钥）**

```bash
cd worker
wrangler secret put TELEMETRY_SECRET    # 输入新值（openssl rand -hex 32）
wrangler deploy
```

**客户端无需发版**：旧密钥失效后，客户端下次上报会收到 401，自动调 `/telemetry-secret`（用激活码鉴权）拉到新密钥并写回本地 config，刷新期间数据照常落 `pending.jsonl` 不丢。

**换 Access Service Token（轮换查询凭据）**

在 Zero Trust 控制台直接轮换/吊销 Service Token，更新 Grafana datasource 里的 `Client-Id/Secret` 即可，**不动代码、不重新 deploy**。上报密钥与查询凭据完全独立（读写分离），任一泄露不影响另一边。

**D1 额度监控**

Cloudflare 控制台 → D1 → `kiro-telemetry` → Metrics，看 rows read / written。Free 额度是写 10 万行/天、读 500 万行/天。接近上限时按设计文档第十二节调查询缓存 TTL，或升级 Workers Paid（$5/月）。本项目体量（百人级）正常稳在 Free 内。

**换域名**

遥测的三条路由都挂在 provision 的 `custom_domain` 下，没有独立域名。换域名时只需按下面「换成你自己的域名」改 `wrangler.toml` 的 `pattern` 并重新部署，同时把 Cloudflare Access 应用里的 **Application domain** 改成新域名、Grafana datasource 的 URL 也相应更新。

### 遥测相关的安全注意

- **`database_id`、域名、部署版本号**都不是敏感信息，可以写进文档、提交进 git。
- **`TELEMETRY_SECRET`、Access Service Token（Client-Id/Secret）、`CF_API_TOKEN`** 是凭据，**绝不进 git**：前者只通过 `wrangler secret put` 存入 Cloudflare，后两者只存在 Cloudflare 控制台和 Grafana 的加密字段里。
- 遥测**只上报聚合量**（时间桶、匿名哈希、模型、版本、计数、字节数之和、token 数之和），**绝不上报** prompt / 响应正文 / 文件路径 / IP；`username` 是不可逆哈希。

---

## 换成你自己的域名

如果不用 `example.com`，改这几处后重新部署：

1. `worker/wrangler.toml` → `[[routes]]` 的 `pattern`，改成 `kiro-gateway-provision.<你的域名>`
2. `wrangler secret put DOMAIN_SUFFIX` → 填你的域名
3. 第 4 步的 API Token 资源范围 → 选你的域名对应的 zone
4. 第 3 步的 `CF_ZONE_ID` → 用你的域名的 Zone ID

> 注意：Worker 里 ingress 固定指向 `http://localhost:64005`，对应 App 网关的默认端口 64005。如果用户改了 `config.toml` 里的 `gateway.port`，需要保持和这个一致，否则隧道连不上本机网关。
