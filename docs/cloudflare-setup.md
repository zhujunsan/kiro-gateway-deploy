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

## 换成你自己的域名

如果不用 `example.com`，改这几处后重新部署：

1. `worker/wrangler.toml` → `[[routes]]` 的 `pattern`，改成 `kiro-gateway-provision.<你的域名>`
2. `wrangler secret put DOMAIN_SUFFIX` → 填你的域名
3. 第 4 步的 API Token 资源范围 → 选你的域名对应的 zone
4. 第 3 步的 `CF_ZONE_ID` → 用你的域名的 Zone ID

> 注意：Worker 里 ingress 固定指向 `http://localhost:64005`，对应 App 网关的默认端口 64005。如果用户改了 `config.toml` 里的 `gateway.port`，需要保持和这个一致，否则隧道连不上本机网关。
