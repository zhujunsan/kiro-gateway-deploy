# Kiro Gateway 跨平台托盘 App 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把现有依赖 Docker 的 kiro-gateway 部署，重做成一个 Mac / Windows / Linux(Ubuntu 24.04+) 都能用的本地托盘 App：进程内跑网关，子进程跑 cloudflared 隧道，无需 Docker、无需本机预装 Python。用户打开 App、填 Kiro token 位置、首次输入一次性共享密钥，App 自动向你部署的 Cloudflare Worker 签发一个专属子域名（`kg-<username>.botsonny.top`），此后每次启动直接连通，无需任何手工操作。

**Architecture:**

```
用户 App
  ├── 内嵌 kiro-gateway（vendored FastAPI，后台线程 uvicorn）
  ├── cloudflared 子进程（连接到 Cloudflare 网络）
  └── 首启注册：调 Cloudflare Worker → 拿 hostname + run_token → 写 config.toml

Cloudflare Worker（你部署，用户永远接触不到）
  ├── 校验共享密钥
  ├── 读 Kiro token 文件提取邮箱前缀作为 username
  ├── 调 Cloudflare API 建 tunnel + 配 ingress + 建 CNAME DNS 记录
  └── 存 KV（username → tunnel_id），返回 run_token 给 App

公网访问路径：
  Cursor（海外）→ https://kg-alice.botsonny.top/v1 → Cloudflare → cloudflared → App 内网关
```

App 在构建期把上游 `jwadow/kiro-gateway`(pin 在 `a5292ca`) 的源码 vendor 进来并打补丁(注入 `kiro-*` 别名 + `/usage` 端点)；运行期先把用户配置翻译成环境变量、chdir 到可写数据目录，再在后台线程里用 uvicorn 跑 `main:app`，同时把 cloudflared 官方二进制作为子进程拉起。托盘 UI 用 `pystray`(三平台同一套代码)，检测不到托盘时(典型是 Ubuntu/GNOME)自动退化成 CLI 模式。出包交给 GitHub Actions：三平台 matrix workflow，打 tag 时自动发 Release。

**Tech Stack:** Python 3.11+、FastAPI + uvicorn(网关，vendored)、pystray + Pillow(托盘)、platformdirs(跨平台目录)、tomllib/tomli-w(TOML 配置)、httpx(健康检查/usage/Worker 调用)、cloudflared 官方二进制、PyInstaller(打包)、GitHub Actions(三平台 matrix 构建 + Release)、Cloudflare Worker + KV(签发服务)。

---

## 执行进度

> 最后更新：2026-06-17。图例：✅ 完成　🚧 进行中　⬜ 未开始　🔒 需你的 Cloudflare 账号手动操作

| Task | 状态 | 说明 |
|---|---|---|
| Task 0 — Cloudflare Worker | ✅ 代码完成 / 🔒 部署待你 | `worker/` 三件套已写好；登录、建 KV、设 secrets、deploy 需你的 CF 账号 |
| Task 1 — 骨架 + 跨平台目录 | ✅ | paths.py + 包元数据，3 passed |
| Task 2 — Vendor + 打补丁 | ✅ | vendor 同步 `a5292ca`，三条 patch 全命中 |
| Task 3 — TOML 配置 | ✅ | appconfig.py，4 passed |
| Task 4 — 内嵌网关 | ✅ | gateway.py，2 passed |
| Task 5 — cloudflared + 注册 | ✅ | fetch/provision/cloudflared，3 passed（二进制下载冒烟留到打包） |
| Task 6 — Supervisor | ✅ | supervisor.py，4 passed |
| Task 7 — 本地 /usage | ✅ | usage.py |
| Task 8 — 托盘 UI | ✅ | tray.py + 图标，25 passed |
| Task 9 — 入口 + CLI 兜底 | ✅ | cli.py + __main__.py，--print-config 冒烟通过 |
| Task 10 — PyInstaller spec | ⬜ | |
| Task 11 — 打包脚本 + README | ⬜ | |
| Task 12 — GitHub Actions | ⬜ | |
| Task 13 — 更新提醒 | ✅ | updates.py + tray/cli 接线全部完成 |
| Task 14 — Homebrew tap | ⬜ | |

**累计：** 25 passed，已完成 Task 0–9 + 13 的代码。剩余 Task 10/11/12/14（打包 + CI + Homebrew）。

---

## 关键约束(实现前必读)

这些是从上游源码里读出来的硬约束，违反任何一条 App 都跑不起来：

1. **配置在 import 期就被读取。** `kiro/config.py` 顶层执行 `load_dotenv()` + 一堆 `os.getenv(...)`，`main.py` import 时也读。因此 App **必须在 import vendored `main` 之前**就把所有环境变量设好，import 之后再改环境变量无效。

2. **legacy 模式每次启动都会在「当前工作目录」重建 `credentials.json` 和 `state.json`。** 见 `main.py` lifespan(约 418-451 行)。所以 App 启动网关前**必须 `os.chdir()` 到一个可写的数据目录**，否则会试图写进 App 安装目录(打包后只读)而崩溃。

3. **`MODEL_ALIASES` 是 `config.py` 里写死的 dict，没有环境变量开关。** `/usage` 端点上游根本不存在。两者都只能靠**改源码**实现 → 复用现有 `patches/` 的两个脚本，但锚点要从容器内的 `/app/...` 改成 vendored 源码的相对路径，并且**在构建期打一次**(而不是每次启动)。

4. **patch 锚点已对当前 pin 的 sha 验证过：**
   - `kiro/model_resolver.py` 含 `normalized = normalize_model_name(model_name)\n    internal = hidden_models.get(normalized, normalized)`(本版本在 217-218 行)。
   - `kiro/routes_openai.py` 含 `verify_api_key`(本版本 68 行，注意现有 `patches/add_usage_endpoint.py` 从 `kiro.routes_openai import verify_api_key`，正确)。
   - `kiro/config.py` 含 `MODEL_ALIASES`。
   - `main.py` 含 `app.include_router`。
   - 上游 pin sha = `a5292ca`(对应现有 `docker-compose.yml` 的镜像 tag `main-a5292ca`)。**vendor 时必须 checkout 这个 sha**，换 sha 必须重验四个锚点。

5. **依赖清单(网关侧)：** `fastapi`、`uvicorn[standard]`、`httpx`、`loguru`、`python-dotenv`、`tiktoken`。其中 `tiktoken`(含数据文件/Rust 扩展)和 `uvicorn[standard]`(含 `websockets`、`httptools`、`uvloop` 等可选 C 扩展)在 PyInstaller 下需要显式收集，见 Task 10(spec)与 Task 12(CI)。

6. **cloudflared 的 DNS CNAME 不会自动建。** 用 API 建 remote-managed tunnel 后，`PUT configurations` 配 ingress **不会自动创建 DNS 记录**，必须额外 `POST /zones/{zone_id}/dns_records` 建一条 proxied CNAME 指向 `{tunnel_id}.cfargotunnel.com`，否则域名解析不到隧道。Worker 里必须这三步都做。

7. **cloudflared run token 是窄权限凭据。** 它只能运行那一条 tunnel 的连接器，无法访问 Cloudflare 账号、修改 DNS 或查看其他 tunnel。把它发给用户是安全的。

8. **不碰现有 Docker 部署。** 仓库根目录的 `docker-compose.yml` / `README.md` / `.env.example` / `frpc/` 是另一条线，本计划全部新增文件落在 `app/`（App）和 `worker/`（签发服务）子目录下。

---

## 文件结构

```
worker/                               # Cloudflare Worker 签发服务（Task 0）
├── wrangler.toml                     # Worker 配置
├── src/
│   └── index.js                      # 单文件 Worker
└── README.md                         # 你的部署操作手册

app/                                  # 本地托盘 App
├── README.md                         # App 的构建/运行说明(Task 11)
├── requirements.txt                  # App 运行期依赖(Task 1)
├── requirements-build.txt            # 构建期依赖(pyinstaller 等)(Task 1)
├── pyproject.toml                    # 包元数据 + 入口(Task 1)
├── kiro_tray/
│   ├── __init__.py                   # 版本号 + pin 的上游 sha 常量(Task 1)
│   ├── __main__.py                   # 入口分发：tray / cli / --print-config(Task 9)
│   ├── paths.py                      # 跨平台 config/data/log 目录(Task 1)
│   ├── appconfig.py                  # TOML 配置读写 + 默认值(Task 3)
│   ├── gateway.py                    # 内嵌网关：设 env、chdir、线程跑 uvicorn(Task 4)
│   ├── cloudflared.py                # 下载 cloudflared + 子进程管理(Task 5)
│   ├── provision.py                  # 首启注册：调 Worker 拿 hostname + run_token(Task 5)
│   ├── supervisor.py                 # 编排 gateway + cloudflared 生命周期(Task 6)
│   ├── usage.py                      # 本地调用 /usage(Task 7)
│   ├── updates.py                    # 查 GitHub 最新 release + 版本比较(Task 13)
│   ├── tray.py                       # pystray 托盘 UI(Task 8)
│   ├── cli.py                        # 无托盘环境的 CLI 兜底(Task 9)
│   └── vendor/                       # 构建期生成，gitignored(Task 2)
│       ├── main.py                   #   ← 上游 + 已打补丁
│       └── kiro/...                  #   ← 上游 kiro 包 + 已打补丁
├── scripts/
│   ├── vendor_sync.py                # clone pin sha → 拷贝 → 打补丁(Task 2)
│   └── fetch_cloudflared.py          # 按平台/架构下载 cloudflared 二进制(Task 5)
├── patches/
│   ├── apply_aliases.py              # 从根 patches/ 适配：锚点改 vendored 路径(Task 2)
│   └── add_usage_endpoint.py         # 同上(Task 2)
├── resources/
│   ├── cloudflared/                  # 构建期下载，gitignored：cloudflared/<os>-<arch>/cloudflared[.exe](Task 5)
│   └── icon.png                      # 托盘图标源(Task 8)
├── packaging/
│   ├── kiro_tray.spec                # PyInstaller spec(Task 10)
│   └── make_dist.py                  # 构建后把产物打成 zip/dmg/tar 并算 sha256(Task 11)
└── tests/
    ├── test_paths.py
    ├── test_appconfig.py
    ├── test_cloudflared.py
    └── test_supervisor.py

仓库根(不在 app/ 或 worker/ 下，但属本计划)：
.github/workflows/build-app.yml       # 三平台 matrix 构建 + Release(Task 12)
```

---

## Task 0: ✅ Cloudflare Worker 签发服务

**Files:**
- Create: `worker/wrangler.toml`
- Create: `worker/src/index.js`
- Create: `worker/README.md`

**职责：** 这是你（管理员）独立部署和维护的服务，用户永远接触不到它的代码或 Cloudflare API Token。它只做一件事：App 首次启动时带着共享密钥和 username 来调它，它帮你完成在 Cloudflare 上建 tunnel、配 ingress、建 DNS CNAME 这三个操作，返回一个窄权限的 run token 和 hostname 给 App。

**Worker 需要的 Cloudflare API Token 权限（最小化）：**

在 [dash.cloudflare.com/profile/api-tokens](https://dash.cloudflare.com/profile/api-tokens) 创建 **Custom Token**，勾选：
- `Account / Cloudflare Tunnel / Edit`（建删 tunnel）
- `Zone / DNS / Edit`（限定 `botsonny.top` 这个 zone，建 CNAME 记录）

不要用 Global API Key。

**API 调用逻辑（Worker 内部）：**

```
1. POST /accounts/{CF_ACCOUNT_ID}/cfd_tunnel
   body: { name: "kg-<username>", config_src: "cloudflare" }
   → 返回 tunnel_id + run_token

2. PUT /accounts/{CF_ACCOUNT_ID}/cfd_tunnel/{tunnel_id}/configurations
   body: { config: { ingress: [
     { hostname: "kg-<username>.botsonny.top", service: "http://localhost:18000" },
     { service: "http_status:404" }
   ]}}
   → 配置 ingress（⚠️ 不会自动建 DNS 记录）

3. POST /zones/{CF_ZONE_ID}/dns_records
   body: { type: "CNAME", proxied: true,
           name: "kg-<username>.botsonny.top",
           content: "<tunnel_id>.cfargotunnel.com" }
   → 建 CNAME，proxied:true 必须，否则 HTTPS 不通

4. KV.put("user:<username>", JSON.stringify({ tunnel_id, hostname, created_at }))

5. 返回 { hostname: "kg-<username>.botsonny.top", run_token: "eyJ..." }
```

- [ ] **Step 1: 安装 wrangler**

```bash
npm install -g wrangler
wrangler --version   # 应显示 3.x
```

- [ ] **Step 2: 登录 Cloudflare**

```bash
wrangler login
# 浏览器打开，授权，完成后终端提示 Successfully logged in
```

- [ ] **Step 3: 建 KV namespace**

```bash
cd worker
wrangler kv namespace create PROVISION_KV
```

输出类似：

```
{ binding = "PROVISION_KV", id = "abc123..." }
```

记下 `id`，下一步填进 `wrangler.toml`。

- [ ] **Step 4: 写 `worker/wrangler.toml`**

```toml
name = "kiro-provision"
main = "src/index.js"
compatibility_date = "2025-01-01"

[[kv_namespaces]]
binding = "PROVISION_KV"
id = "<上一步拿到的 KV namespace id>"

# 自定义域名：把 Worker 绑到 botsonny.top 的子域名，而不是用默认 *.workers.dev。
# custom_domain = true 时，wrangler 会自动在 botsonny.top 这个 zone 里建好
# 路由和代理 DNS 记录（前提是该 zone 已在当前 Cloudflare 账号下，你的情况满足）。
[[routes]]
pattern = "kiro-gateway-provision.botsonny.top"
custom_domain = true
```

> 注：`kiro-gateway-provision.botsonny.top` 是一级子域名，落在 Universal SSL 的 `*.botsonny.top` 覆盖范围内，HTTPS 直接可用，无需额外证书。换成别的名字（如 `kiro-api.botsonny.top`）同理，只要保持单级子域名即可。

- [ ] **Step 5: 写 `worker/src/index.js`**

```javascript
// worker/src/index.js
// Cloudflare Worker: provision a per-user cloudflared tunnel.
//
// POST /provision
//   body: { shared_secret: string, username: string }
//   → 201 { hostname, run_token }  (first call)
//   → 200 { hostname, run_token: null, message: "already provisioned" }  (repeat)
//
// Required Worker Secrets (set via wrangler secret put):
//   SHARED_SECRET   — the secret distributed to users out-of-band
//   CF_API_TOKEN    — scoped: Tunnel:Edit + DNS:Edit (botsonny.top only)
//   CF_ACCOUNT_ID
//   CF_ZONE_ID
//   DOMAIN_SUFFIX   — e.g. "botsonny.top"
//   HOSTNAME_PREFIX — e.g. "kg"  → final hostname = kg-<username>.<DOMAIN_SUFFIX>

const CF_API = "https://api.cloudflare.com/client/v4";

async function cfFetch(env, path, method = "GET", body = null) {
  const opts = {
    method,
    headers: {
      "Authorization": `Bearer ${env.CF_API_TOKEN}`,
      "Content-Type": "application/json",
    },
  };
  if (body) opts.body = JSON.stringify(body);
  const res = await fetch(`${CF_API}${path}`, opts);
  const json = await res.json();
  if (!json.success) {
    throw new Error(`CF API ${method} ${path} failed: ${JSON.stringify(json.errors)}`);
  }
  return json.result;
}

async function provision(env, username) {
  const prefix = env.HOSTNAME_PREFIX || "kg";
  const hostname = `${prefix}-${username}.${env.DOMAIN_SUFFIX}`;
  const tunnelName = `${prefix}-${username}`;

  // 1. Create tunnel
  const tunnel = await cfFetch(
    env,
    `/accounts/${env.CF_ACCOUNT_ID}/cfd_tunnel`,
    "POST",
    { name: tunnelName, config_src: "cloudflare" }
  );
  const tunnelId = tunnel.id;
  const runToken = tunnel.token;

  // 2. Set ingress (does NOT auto-create DNS record)
  await cfFetch(
    env,
    `/accounts/${env.CF_ACCOUNT_ID}/cfd_tunnel/${tunnelId}/configurations`,
    "PUT",
    {
      config: {
        ingress: [
          { hostname, service: "http://localhost:18000" },
          { service: "http_status:404" },
        ],
      },
    }
  );

  // 3. Create proxied CNAME DNS record (must be proxied=true for HTTPS to work)
  await cfFetch(
    env,
    `/zones/${env.CF_ZONE_ID}/dns_records`,
    "POST",
    {
      type: "CNAME",
      name: hostname,
      content: `${tunnelId}.cfargotunnel.com`,
      proxied: true,
    }
  );

  // 4. Persist in KV
  await env.PROVISION_KV.put(
    `user:${username}`,
    JSON.stringify({ tunnel_id: tunnelId, hostname, created_at: new Date().toISOString() })
  );

  return { hostname, run_token: runToken };
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "POST" && url.pathname === "/provision") {
      let body;
      try {
        body = await request.json();
      } catch {
        return new Response(JSON.stringify({ error: "invalid JSON" }), { status: 400 });
      }

      const { shared_secret, username } = body || {};

      // Validate shared secret
      if (!shared_secret || shared_secret !== env.SHARED_SECRET) {
        return new Response(JSON.stringify({ error: "unauthorized" }), { status: 401 });
      }

      // Validate username: lowercase alphanumeric + hyphen, 1-32 chars
      if (!username || !/^[a-z0-9][a-z0-9-]{0,31}$/.test(username)) {
        return new Response(
          JSON.stringify({ error: "username must be lowercase alphanumeric/hyphen, 1-32 chars" }),
          { status: 400 }
        );
      }

      // Idempotency: already provisioned?
      const existing = await env.PROVISION_KV.get(`user:${username}`);
      if (existing) {
        const data = JSON.parse(existing);
        // Return hostname but NOT run_token (it was only returned once at creation)
        return new Response(
          JSON.stringify({ hostname: data.hostname, run_token: null, message: "already provisioned" }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }

      try {
        const result = await provision(env, username);
        return new Response(JSON.stringify(result), {
          status: 201,
          headers: { "Content-Type": "application/json" },
        });
      } catch (err) {
        return new Response(JSON.stringify({ error: err.message }), { status: 500 });
      }
    }

    return new Response("not found", { status: 404 });
  },
};
```

- [ ] **Step 6: 设置 Worker Secrets**

用 `wrangler secret put` 逐一写入，每条命令提示你输入值（不会出现在 shell 历史里）：

```bash
cd worker

wrangler secret put SHARED_SECRET
# 输入：你准备发给用户的共享密钥（建议 openssl rand -hex 20 生成）

wrangler secret put CF_API_TOKEN
# 输入：上面创建的 Custom Token（Tunnel:Edit + DNS:Edit 权限）

wrangler secret put CF_ACCOUNT_ID
# 输入：你的 Cloudflare Account ID（在 dash.cloudflare.com 右上角或任意域名概览页右栏）

wrangler secret put CF_ZONE_ID
# 输入：botsonny.top 的 Zone ID（域名概览页右栏 API 区域）

wrangler secret put DOMAIN_SUFFIX
# 输入：botsonny.top

wrangler secret put HOSTNAME_PREFIX
# 输入：kg
```

- [ ] **Step 7: 部署 Worker**

```bash
cd worker
wrangler deploy
```

因为 `wrangler.toml` 里配了 `routes` 自定义域名，首次 `deploy` 时 wrangler 会自动在 `botsonny.top` 这个 zone 下创建 `kiro-gateway-provision.botsonny.top` 的路由和所需 DNS 记录（域名已在你 Cloudflare 账号下，无需手工加）。输出末尾会显示类似：

```
Published kiro-provision (0.00 sec)
  https://kiro-gateway-provision.botsonny.top
  https://kiro-provision.<your-workers-dev-subdomain>.workers.dev
```

App 里的 `PROVISION_WORKER_URL` 填 `https://kiro-gateway-provision.botsonny.top` 即可。`workers.dev` 那个地址仍然可用，但自定义域名更干净、也便于以后换实现。

- [ ] **Step 8: 验证 Worker 正常工作**

```bash
curl -s -X POST https://kiro-gateway-provision.botsonny.top/provision \
  -H "Content-Type: application/json" \
  -d '{"shared_secret":"<你设的 SHARED_SECRET>","username":"testuser"}' | jq .
```

预期输出：

```json
{
  "hostname": "kg-testuser.botsonny.top",
  "run_token": "eyJ..."
}
```

再发一次（幂等测试）：

```json
{
  "hostname": "kg-testuser.botsonny.top",
  "run_token": null,
  "message": "already provisioned"
}
```

- [ ] **Step 9: 写 `worker/README.md`（你自己的操作手册）**

```markdown
# kiro-provision Worker

## 首次部署

1. `npm install -g wrangler && wrangler login`
2. `wrangler kv namespace create PROVISION_KV` → 把 id 填进 wrangler.toml
3. 按 src/index.js 注释逐一 `wrangler secret put <KEY>`
4. `wrangler deploy`

## Secrets 清单

| Secret | 说明 |
|---|---|
| SHARED_SECRET | 发给用户的一次性激活码，泄露了重新设一个即可 |
| CF_API_TOKEN | Custom Token：Tunnel:Edit + DNS:Edit(botsonny.top) |
| CF_ACCOUNT_ID | Cloudflare Account ID |
| CF_ZONE_ID | botsonny.top 的 Zone ID |
| DOMAIN_SUFFIX | botsonny.top |
| HOSTNAME_PREFIX | kg |

## 更新 SHARED_SECRET（换批用户时）

```bash
wrangler secret put SHARED_SECRET
wrangler deploy
```

## 查看已注册用户

```bash
wrangler kv key list --namespace-id <KV_NAMESPACE_ID>
wrangler kv key get --namespace-id <KV_NAMESPACE_ID> "user:alice"
```

## 注意事项

- run_token 只在 201 响应里返回一次，之后 KV 里不存它（避免 KV 成为 token 仓库）
- 吊销某用户：在 Zero Trust 控制台删 tunnel，DNS 记录也要手动删
- CF_API_TOKEN 永远不要提交到 git，只通过 `wrangler secret put` 存入
```

- [ ] **Step 10: 提交 Worker（不含 secrets）**

```bash
git add worker/
git commit -m "feat(worker): cloudflare tunnel provision worker"
```

---

## Task 1: ✅ 子项目骨架 + 跨平台目录

**Files:**
- Create: `app/kiro_tray/__init__.py`
- Create: `app/kiro_tray/paths.py`
- Create: `app/requirements.txt`
- Create: `app/requirements-build.txt`
- Create: `app/pyproject.toml`
- Create: `app/.gitignore`
- Test: `app/tests/test_paths.py`

- [ ] **Step 1: 写失败测试**

```python
# app/tests/test_paths.py
from kiro_tray import paths


def test_dirs_are_absolute_and_namespaced():
    cfg = paths.config_dir()
    data = paths.data_dir()
    log = paths.log_dir()
    for p in (cfg, data, log):
        assert p.is_absolute()
        assert "KiroTray" in str(p) or "kiro-tray" in str(p).lower()


def test_config_file_lives_in_config_dir():
    assert paths.config_file().parent == paths.config_dir()
    assert paths.config_file().name == "config.toml"


def test_ensure_dirs_creates_them(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_TRAY_HOME", str(tmp_path))
    monkeypatch.setattr(paths, "_OVERRIDE", None, raising=False)
    paths.ensure_dirs()
    assert paths.data_dir().exists()
    assert paths.log_dir().exists()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd app && python -m pytest tests/test_paths.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'kiro_tray'`

- [ ] **Step 3: 写 `paths.py`**

`KIRO_TRAY_HOME` 环境变量优先(便于测试和绿色便携模式)，否则用 `platformdirs`。

```python
# app/kiro_tray/paths.py
"""Cross-platform config/data/log directories for the Kiro tray app."""
from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_config_dir, user_data_dir, user_log_dir

_APP_NAME = "KiroTray"
_APP_AUTHOR = "KiroTray"


def _home_override() -> Path | None:
    raw = os.environ.get("KIRO_TRAY_HOME")
    return Path(raw).expanduser() if raw else None


def config_dir() -> Path:
    base = _home_override()
    return base / "config" if base else Path(user_config_dir(_APP_NAME, _APP_AUTHOR))


def data_dir() -> Path:
    base = _home_override()
    return base / "data" if base else Path(user_data_dir(_APP_NAME, _APP_AUTHOR))


def log_dir() -> Path:
    base = _home_override()
    return base / "logs" if base else Path(user_log_dir(_APP_NAME, _APP_AUTHOR))


def config_file() -> Path:
    return config_dir() / "config.toml"


def ensure_dirs() -> None:
    for d in (config_dir(), data_dir(), log_dir()):
        d.mkdir(parents=True, exist_ok=True)
```

- [ ] **Step 4: 写包元数据文件**

```python
# app/kiro_tray/__init__.py
"""Kiro Gateway tray app."""

__version__ = "0.1.0"

# Upstream jwadow/kiro-gateway commit this app vendors and patches against.
UPSTREAM_SHA = "a5292ca"
UPSTREAM_REPO = "https://github.com/jwadow/kiro-gateway.git"

# This app's own repo, used by the update checker (Task 13) to query the
# latest GitHub release. Format: "owner/repo".
GITHUB_REPO = "zhujunsan/kiro-gateway-deploy"
```

```
# app/requirements.txt
# App runtime deps
pystray
Pillow
platformdirs
tomli-w
httpx
# Gateway (vendored) runtime deps
fastapi
uvicorn[standard]
loguru
python-dotenv
tiktoken
```

```
# app/requirements-build.txt
-r requirements.txt
pyinstaller
pytest
pytest-asyncio
```

```toml
# app/pyproject.toml
[project]
name = "kiro-tray"
version = "0.1.0"
description = "Cross-platform tray app for kiro-gateway (no Docker)"
requires-python = ">=3.11"

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

```
# app/.gitignore
kiro_tray/vendor/
resources/cloudflared/
build/
dist/
release/
*.spec.bak
__pycache__/
.venv/
```

- [ ] **Step 5: 建 venv、装依赖、跑测试确认通过**

Run:
```bash
cd app
python3.11 -m venv .venv
. .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt pytest pytest-asyncio
python -m pytest tests/test_paths.py -v
```
Expected: 3 passed。

- [ ] **Step 6: 提交**

```bash
git add app/kiro_tray/__init__.py app/kiro_tray/paths.py app/requirements.txt app/requirements-build.txt app/pyproject.toml app/.gitignore app/tests/test_paths.py
git commit -m "feat(app): scaffold tray subproject with cross-platform paths"
```

---

## Task 2: ✅ Vendor 上游源码 + 构建期打补丁

**Files:**
- Create: `app/patches/apply_aliases.py`
- Create: `app/patches/add_usage_endpoint.py`
- Create: `app/scripts/vendor_sync.py`
- Test: 手动验收(脚本产物 + import 冒烟)

**说明:** 复用根目录 `patches/` 的两个脚本，只改两处：(a) 锚点路径从容器内 `/app/...` 改成 vendored 目录；(b) `EXTRA_ALIASES` 与 `/usage` 端点逻辑**原样照搬**(已在生产验证过)。

- [ ] **Step 1: 写 `app/patches/apply_aliases.py`**

```python
# app/patches/apply_aliases.py
"""Build-time patch: inject kiro-* model aliases into vendored source."""
import sys
from pathlib import Path

CONFIG_SENTINEL = "# >>> kiro-gateway custom aliases >>>"
RESOLVER_SENTINEL = "# kiro-gateway: alias-aware"

EXTRA_ALIASES = {
    "auto-kiro": "auto",
    "kiro-opus-4.8": "claude-opus-4.8",
    "kiro-opus-4.7": "claude-opus-4.7",
    "kiro-opus-4.6": "claude-opus-4.6",
    "kiro-sonnet-4.6": "claude-sonnet-4.6",
    "kiro-sonnet-4.5": "claude-sonnet-4.5",
    "kiro-haiku-4.5": "claude-haiku-4.5",
}


def patch_config(config: Path) -> None:
    src = config.read_text()
    if CONFIG_SENTINEL in src:
        print("[skip] config.py already patched")
        return
    if "MODEL_ALIASES" not in src:
        sys.exit("config.py: MODEL_ALIASES not found")
    block = [CONFIG_SENTINEL, "MODEL_ALIASES.update({"]
    block += [f'    "{a}": "{t}",' for a, t in EXTRA_ALIASES.items()]
    block += ["})", "# <<< kiro-gateway custom aliases <<<"]
    config.write_text(src.rstrip() + "\n\n" + "\n".join(block) + "\n")
    print("[ok] patched MODEL_ALIASES")


def patch_resolver(resolver: Path) -> None:
    src = resolver.read_text()
    if RESOLVER_SENTINEL in src:
        print("[skip] model_resolver.py already patched")
        return
    anchor = (
        "normalized = normalize_model_name(model_name)\n"
        "    internal = hidden_models.get(normalized, normalized)"
    )
    if anchor not in src:
        sys.exit("model_resolver.py: get_model_id_for_kiro body not found")
    replacement = (
        "from kiro.config import MODEL_ALIASES  " + RESOLVER_SENTINEL + "\n"
        "    model_name = MODEL_ALIASES.get(model_name, model_name)\n"
        "    normalized = normalize_model_name(model_name)\n"
        "    internal = hidden_models.get(normalized, normalized)"
    )
    resolver.write_text(src.replace(anchor, replacement, 1))
    print("[ok] patched get_model_id_for_kiro")


def main(vendor_root: Path) -> None:
    patch_config(vendor_root / "kiro" / "config.py")
    patch_resolver(vendor_root / "kiro" / "model_resolver.py")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
```

- [ ] **Step 2: 写 `app/patches/add_usage_endpoint.py`**

`ENDPOINT_CODE` 字符串与根 `patches/add_usage_endpoint.py` 的**完全一致**（逐字照搬，包含 `_usage_pick_auth` / `_usage_summary` / `_kiro_usage`）。只改外层定位逻辑。

```python
# app/patches/add_usage_endpoint.py
"""Build-time patch: expose GET /usage in vendored main.py."""
import sys
from pathlib import Path

SENTINEL = "# >>> kiro-gateway usage endpoint >>>"

ENDPOINT_CODE = '''
<<< 逐字粘贴根 patches/add_usage_endpoint.py 中 ENDPOINT_CODE 的全部内容 >>>
'''


def main(vendor_root: Path) -> None:
    main_py = vendor_root / "main.py"
    src = main_py.read_text()
    if SENTINEL in src:
        print("[skip] main.py /usage already patched")
        return
    if "app.include_router" not in src:
        sys.exit("main.py: app.include_router not found (unexpected structure)")
    main_py.write_text(src.rstrip() + "\n" + ENDPOINT_CODE)
    print("[ok] patched /usage endpoint")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
```

> 实现者注意：上面的 `ENDPOINT_CODE` 占位必须替换为根 `patches/add_usage_endpoint.py` 里那段完整字符串。用 Read 工具读根文件，原样复制，一个字符都不要改。

- [ ] **Step 3: 写 `app/scripts/vendor_sync.py`**

```python
# app/scripts/vendor_sync.py
"""Clone upstream at the pinned SHA, copy needed files, apply patches."""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kiro_tray import UPSTREAM_REPO, UPSTREAM_SHA  # noqa: E402

VENDOR = ROOT / "kiro_tray" / "vendor"
COPY_ITEMS = ["main.py", "kiro", "requirements.txt"]


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True)


def main() -> None:
    if VENDOR.exists():
        shutil.rmtree(VENDOR)
    VENDOR.mkdir(parents=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _run(["git", "clone", "--no-checkout", UPSTREAM_REPO, "src"], cwd=tmp_path)
        src = tmp_path / "src"
        _run(["git", "checkout", UPSTREAM_SHA], cwd=src)
        for item in COPY_ITEMS:
            s = src / item
            d = VENDOR / item
            if s.is_dir():
                shutil.copytree(s, d)
            else:
                shutil.copy2(s, d)

    from patches import apply_aliases, add_usage_endpoint  # noqa: E402
    apply_aliases.main(VENDOR)
    add_usage_endpoint.main(VENDOR)

    (VENDOR / "__init__.py").write_text("")
    print(f"[ok] vendored upstream {UPSTREAM_SHA} into {VENDOR}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 跑 vendor 脚本，确认三条 patch 输出**

Run: `cd app && python scripts/vendor_sync.py`

- [ ] **Step 5: 冒烟验证补丁**

```bash
cd app && python -c "
import sys; sys.path.insert(0, 'kiro_tray/vendor')
import kiro.config as c
assert 'kiro-opus-4.8' in c.MODEL_ALIASES
src = open('kiro_tray/vendor/main.py').read()
assert '/usage' in src
print('[ok] patches verified')
"
```

- [ ] **Step 6: 提交**

```bash
git add app/patches/ app/scripts/vendor_sync.py
git commit -m "feat(app): vendor + build-time patch upstream gateway at pinned sha"
```

---

## Task 3: ✅ TOML 配置读写

**Files:**
- Create: `app/kiro_tray/appconfig.py`
- Test: `app/tests/test_appconfig.py`

**配置模型（用户编辑的 `config.toml`）:**

```toml
[gateway]
profile_arn = "arn:aws:codewhisperer:us-east-1:000000000000:profile/XXXX"
proxy_api_key = "change-me"
port = 18000
api_region = "us-east-1"
kiro_creds_file = ""       # 留空则用默认 ~/.aws/sso/cache/kiro-auth-token.json
fake_reasoning = false

[cloudflare]
# 首启注册后由 App 自动写入，用户不需手填
hostname = ""              # e.g. kg-alice.botsonny.top
run_token = ""             # per-tunnel 窄权限 token
provision_url = ""         # Worker URL，首次激活时填入
```

- [ ] **Step 1: 写失败测试**

```python
# app/tests/test_appconfig.py
from kiro_tray import appconfig


def test_defaults_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_TRAY_HOME", str(tmp_path))
    cfg = appconfig.load()
    assert cfg.gateway.port == 18000
    assert cfg.cloudflare.hostname == ""
    assert cfg.cloudflare.run_token == ""
    assert appconfig.path().exists()


def test_edit_and_reload(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_TRAY_HOME", str(tmp_path))
    cfg = appconfig.load()
    cfg.gateway.proxy_api_key = "secret123"
    cfg.cloudflare.hostname = "kg-alice.botsonny.top"
    cfg.cloudflare.run_token = "eyJ_test"
    appconfig.save(cfg)
    again = appconfig.load()
    assert again.gateway.proxy_api_key == "secret123"
    assert again.cloudflare.hostname == "kg-alice.botsonny.top"
    assert again.cloudflare.run_token == "eyJ_test"


def test_to_env_maps_known_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_TRAY_HOME", str(tmp_path))
    cfg = appconfig.load()
    cfg.gateway.profile_arn = "arn:x"
    cfg.gateway.proxy_api_key = "k"
    env = appconfig.to_gateway_env(cfg)
    assert env["PROFILE_ARN"] == "arn:x"
    assert env["PROXY_API_KEY"] == "k"
    assert env["SERVER_HOST"] == "127.0.0.1"
    assert env["SERVER_PORT"] == "18000"
    assert env["FAKE_REASONING"] == "false"


def test_is_provisioned(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_TRAY_HOME", str(tmp_path))
    cfg = appconfig.load()
    assert appconfig.is_provisioned(cfg) is False
    cfg.cloudflare.hostname = "kg-alice.botsonny.top"
    cfg.cloudflare.run_token = "eyJ_test"
    assert appconfig.is_provisioned(cfg) is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd app && python -m pytest tests/test_appconfig.py -v`

- [ ] **Step 3: 写 `appconfig.py`**

```python
# app/kiro_tray/appconfig.py
"""Load/save the user-edited TOML config and map it to gateway env vars."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, asdict, field

import tomli_w

from . import paths


@dataclass
class GatewayCfg:
    profile_arn: str = ""
    proxy_api_key: str = "change-me"
    port: int = 18000
    api_region: str = "us-east-1"
    kiro_creds_file: str = ""
    fake_reasoning: bool = False


@dataclass
class CloudflareCfg:
    hostname: str = ""        # kg-<username>.botsonny.top, written by provision flow
    run_token: str = ""       # per-tunnel run token, written by provision flow
    provision_url: str = ""   # Worker URL, set by user once before first activation


@dataclass
class AppCfg:
    gateway: GatewayCfg = field(default_factory=GatewayCfg)
    cloudflare: CloudflareCfg = field(default_factory=CloudflareCfg)


def path():
    return paths.config_file()


def load() -> AppCfg:
    paths.ensure_dirs()
    p = path()
    if not p.exists():
        cfg = AppCfg()
        save(cfg)
        return cfg
    raw = tomllib.loads(p.read_text(encoding="utf-8"))
    return AppCfg(
        gateway=GatewayCfg(**{**asdict(GatewayCfg()), **(raw.get("gateway") or {})}),
        cloudflare=CloudflareCfg(**{**asdict(CloudflareCfg()), **(raw.get("cloudflare") or {})}),
    )


def save(cfg: AppCfg) -> None:
    paths.ensure_dirs()
    path().write_text(tomli_w.dumps(asdict(cfg)), encoding="utf-8")


def is_provisioned(cfg: AppCfg) -> bool:
    return bool(cfg.cloudflare.hostname and cfg.cloudflare.run_token)


def default_creds_file() -> str:
    from pathlib import Path
    return str(Path.home() / ".aws" / "sso" / "cache" / "kiro-auth-token.json")


def to_gateway_env(cfg: AppCfg) -> dict[str, str]:
    """Translate config into env vars the vendored gateway reads at import."""
    creds = cfg.gateway.kiro_creds_file or default_creds_file()
    return {
        "PROFILE_ARN": cfg.gateway.profile_arn,
        "PROXY_API_KEY": cfg.gateway.proxy_api_key,
        "KIRO_CREDS_FILE": creds,
        "KIRO_API_REGION": cfg.gateway.api_region,
        "SERVER_HOST": "127.0.0.1",
        "SERVER_PORT": str(cfg.gateway.port),
        "FAKE_REASONING": "true" if cfg.gateway.fake_reasoning else "false",
    }
```

- [ ] **Step 4: 跑测试确认通过（4 passed）**

- [ ] **Step 5: 提交**

```bash
git add app/kiro_tray/appconfig.py app/tests/test_appconfig.py
git commit -m "feat(app): TOML config with cloudflare section + provisioned check"
```

---

## Task 4: ✅ 内嵌网关(设 env → chdir → 线程跑 uvicorn)

**Files:**
- Create: `app/kiro_tray/gateway.py`
- Test: `app/tests/test_gateway.py`

**说明:** 严格按「先设 env、再 chdir、最后才 import vendored main」的顺序，原因见「关键约束」#1 #2。

- [ ] **Step 1: 写失败测试**

```python
# app/tests/test_gateway.py
from pathlib import Path
from kiro_tray import gateway, appconfig


def test_vendor_root_missing_raises(monkeypatch):
    monkeypatch.setattr(gateway, "_candidate_vendor_roots", lambda: [Path("/no/such")])
    try:
        gateway._vendor_root()
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "vendor" in str(e).lower()


def test_apply_env_sets_process_env(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_TRAY_HOME", str(tmp_path))
    cfg = appconfig.load()
    cfg.gateway.profile_arn = "arn:test"
    cfg.gateway.proxy_api_key = "k123"
    gateway._apply_env(cfg)
    import os
    assert os.environ["PROFILE_ARN"] == "arn:test"
    assert os.environ["PROXY_API_KEY"] == "k123"
    assert os.environ["SERVER_HOST"] == "127.0.0.1"
```

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 写 `gateway.py`**

```python
# app/kiro_tray/gateway.py
"""Run the vendored kiro-gateway in-process on a background thread.

CRITICAL ORDER (see plan 关键约束 #1/#2):
  1. set env vars   (config.py reads them at import time)
  2. os.chdir(data) (legacy mode rewrites credentials.json/state.json in CWD)
  3. add vendor/ to sys.path, THEN import main
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

from . import appconfig, paths
from .appconfig import AppCfg


def _candidate_vendor_roots() -> list[Path]:
    here = Path(__file__).resolve().parent
    roots = [here / "vendor"]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass) / "kiro_tray" / "vendor")
    return roots


def _vendor_root() -> Path:
    for r in _candidate_vendor_roots():
        if (r / "main.py").exists():
            return r
    raise RuntimeError(
        "vendored gateway not found; run scripts/vendor_sync.py before building. "
        f"looked in: {[str(r) for r in _candidate_vendor_roots()]}"
    )


def _apply_env(cfg: AppCfg) -> None:
    for k, v in appconfig.to_gateway_env(cfg).items():
        os.environ[k] = v


class GatewayThread:
    def __init__(self) -> None:
        self._server = None
        self._thread: threading.Thread | None = None

    def start(self, cfg: AppCfg) -> None:
        _apply_env(cfg)
        paths.ensure_dirs()
        os.chdir(paths.data_dir())
        vendor = _vendor_root()
        if str(vendor) not in sys.path:
            sys.path.insert(0, str(vendor))
        import uvicorn
        main = __import__("main")
        config = uvicorn.Config(
            app=main.app,
            host=os.environ["SERVER_HOST"],
            port=int(os.environ["SERVER_PORT"]),
            log_config=getattr(main, "UVICORN_LOG_CONFIG", None),
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True, name="kiro-gateway")
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())
```

- [ ] **Step 4: 跑测试确认通过**

- [ ] **Step 5: 提交**

```bash
git add app/kiro_tray/gateway.py app/tests/test_gateway.py
git commit -m "feat(app): embed vendored gateway on background uvicorn thread"
```

---

## Task 5: ✅ cloudflared 二进制下载 + 首启注册 + 子进程管理

**Files:**
- Create: `app/scripts/fetch_cloudflared.py`
- Create: `app/kiro_tray/provision.py`
- Create: `app/kiro_tray/cloudflared.py`
- Test: `app/tests/test_cloudflared.py`

**说明:**

cloudflared 官方在 GitHub releases 直接发布各平台二进制，命名规则：

| 平台 | 文件名 | 说明 |
|---|---|---|
| macOS arm64 | `cloudflared-darwin-arm64.tgz` | tar.gz，解出 `cloudflared` |
| macOS amd64 | `cloudflared-darwin-amd64.tgz` | 同上 |
| Linux amd64 | `cloudflared-linux-amd64` | 直接是二进制，无压缩 |
| Linux arm64 | `cloudflared-linux-arm64` | 同上 |
| Windows amd64 | `cloudflared-windows-amd64.exe` | 直接是 exe，无压缩 |

下载地址：`https://github.com/cloudflare/cloudflared/releases/latest/download/<filename>`

**首启注册流程（`provision.py`）:**

1. 读 Kiro token 文件（`~/.aws/sso/cache/kiro-auth-token.json`），提取 `email` 字段的 `@` 前缀作为 username，全部转小写、非字母数字转 `-`
2. 带 `shared_secret` + `username` 调 Worker 的 `POST /provision`
3. 响应 201 → 写 `hostname` + `run_token` 进 `config.toml`
4. 响应 200（already provisioned）→ hostname 已知但 run_token 丢失，提示用户联系管理员重新签发

- [ ] **Step 1: 写 `app/scripts/fetch_cloudflared.py`**

```python
# app/scripts/fetch_cloudflared.py
"""Download the official cloudflared binary for the current or specified platform."""
from __future__ import annotations

import io
import platform
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST_BASE = ROOT / "resources" / "cloudflared"
BASE_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download"


def _target() -> tuple[str, str]:
    """Return (os_name, arch) matching cloudflared release asset naming."""
    sysname = platform.system().lower()
    machine = platform.machine().lower()
    os_name = {"darwin": "darwin", "linux": "linux", "windows": "windows"}[sysname]
    arch = {
        "x86_64": "amd64", "amd64": "amd64",
        "arm64": "arm64", "aarch64": "arm64",
    }[machine]
    return os_name, arch


def fetch(os_name: str, arch: str) -> Path:
    dest_dir = DEST_BASE / f"{os_name}-{arch}"
    dest_dir.mkdir(parents=True, exist_ok=True)

    if os_name == "windows":
        filename = "cloudflared-windows-amd64.exe"
        dest = dest_dir / "cloudflared.exe"
    elif os_name == "darwin":
        filename = f"cloudflared-darwin-{arch}.tgz"
        dest = dest_dir / "cloudflared"
    else:
        filename = f"cloudflared-linux-{arch}"
        dest = dest_dir / "cloudflared"

    url = f"{BASE_URL}/{filename}"
    print(f"downloading {url}")
    data = urllib.request.urlopen(url).read()  # noqa: S310

    if filename.endswith(".tgz"):
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
            member = t.extractfile("cloudflared")
            dest.write_bytes(member.read())
    else:
        dest.write_bytes(data)

    if os_name != "windows":
        dest.chmod(0o755)

    print(f"[ok] {dest}")
    return dest


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--current-only"]
    if len(args) == 2:
        fetch(args[0], args[1])
    else:
        fetch(*_target())
```

- [ ] **Step 2: 写 `app/kiro_tray/provision.py`**

```python
# app/kiro_tray/provision.py
"""First-run registration: call the Cloudflare Worker to provision a tunnel."""
from __future__ import annotations

import json
import re
from pathlib import Path

import httpx

from . import appconfig
from .appconfig import AppCfg


def _read_kiro_email(cfg: AppCfg) -> str | None:
    """Extract email from Kiro SSO token file."""
    creds_file = cfg.gateway.kiro_creds_file or appconfig.default_creds_file()
    try:
        data = json.loads(Path(creds_file).read_text())
        return data.get("email") or data.get("Email")
    except Exception:
        return None


def _email_to_username(email: str) -> str:
    """Convert email prefix to a valid tunnel username."""
    prefix = email.split("@")[0].lower()
    # Replace non-alphanumeric chars with hyphens, collapse multiple hyphens
    username = re.sub(r"[^a-z0-9]+", "-", prefix).strip("-")
    return username[:32]  # max 32 chars


def run(cfg: AppCfg, shared_secret: str) -> tuple[str, str]:
    """Call the Worker and return (hostname, run_token).

    Raises RuntimeError on failure or if already provisioned (run_token lost).
    """
    if not cfg.cloudflare.provision_url:
        raise RuntimeError(
            "provision_url 未配置。请在 config.toml 的 [cloudflare] 段填入 Worker URL。\n"
            "示例：provision_url = \"https://kiro-gateway-provision.botsonny.top\""
        )

    email = _read_kiro_email(cfg)
    if not email:
        raise RuntimeError(
            "无法从 Kiro token 文件中读取 email。\n"
            "请确认已用 Kiro IDE 登录（~/.aws/sso/cache/kiro-auth-token.json 存在）。"
        )

    username = _email_to_username(email)
    url = cfg.cloudflare.provision_url.rstrip("/") + "/provision"

    resp = httpx.post(
        url,
        json={"shared_secret": shared_secret, "username": username},
        timeout=30,
    )

    if resp.status_code == 401:
        raise RuntimeError("共享密钥错误，请确认你输入的激活码正确。")

    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Worker 返回错误 {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    hostname = data["hostname"]
    run_token = data.get("run_token")

    if resp.status_code == 200 and not run_token:
        raise RuntimeError(
            f"此 username ({username}) 已注册，子域名为 {hostname}，\n"
            "但 run_token 已无法再次获取。请联系管理员重新签发。"
        )

    return hostname, run_token
```

- [ ] **Step 3: 写 `app/kiro_tray/cloudflared.py`**

```python
# app/kiro_tray/cloudflared.py
"""Locate the cloudflared binary and manage the cloudflared child process."""
from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path

from . import paths
from .appconfig import AppCfg


def _current_target() -> str:
    sysname = platform.system().lower()
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}[machine]
    return f"{sysname}-{arch}"


def _binary_name() -> str:
    return "cloudflared.exe" if sys.platform.startswith("win") else "cloudflared"


def _candidate_dirs() -> list[Path]:
    here = Path(__file__).resolve().parent.parent   # app/
    dirs = [here / "resources" / "cloudflared" / _current_target()]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(Path(meipass) / "resources" / "cloudflared" / _current_target())
    return dirs


def binary_path() -> Path:
    name = _binary_name()
    for d in _candidate_dirs():
        p = d / name
        if p.exists():
            return p
    raise RuntimeError(
        f"cloudflared binary not found for {_current_target()}; "
        f"run scripts/fetch_cloudflared.py. looked in {[str(d) for d in _candidate_dirs()]}"
    )


class CloudflaredProcess:
    """Runs `cloudflared tunnel run --token <run_token>` as a child process."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None

    def start(self, cfg: AppCfg) -> None:
        run_token = cfg.cloudflare.run_token
        if not run_token:
            raise RuntimeError("cloudflare.run_token 未设置，请先完成首启注册。")
        self._proc = subprocess.Popen(
            [str(binary_path()), "tunnel", "--no-autoupdate", "run", "--token", run_token],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    def is_alive(self) -> bool:
        return bool(self._proc and self._proc.poll() is None)
```

- [ ] **Step 4: 写失败测试**

```python
# app/tests/test_cloudflared.py
from pathlib import Path
from kiro_tray import cloudflared, appconfig


def test_binary_name_per_platform():
    import sys
    name = cloudflared._binary_name()
    if sys.platform.startswith("win"):
        assert name == "cloudflared.exe"
    else:
        assert name == "cloudflared"


def test_binary_path_missing_raises(monkeypatch):
    monkeypatch.setattr(cloudflared, "_candidate_dirs", lambda: [Path("/no/such")])
    try:
        cloudflared.binary_path()
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "cloudflared" in str(e).lower()


def test_provision_email_to_username():
    from kiro_tray.provision import _email_to_username
    assert _email_to_username("alice@example.com") == "alice"
    assert _email_to_username("john.doe@example.com") == "john-doe"
    assert _email_to_username("ALICE@example.com") == "alice"
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd app && python -m pytest tests/test_cloudflared.py -v`

- [ ] **Step 6: 下载本机 cloudflared 冒烟**

```bash
cd app && python scripts/fetch_cloudflared.py
```
Expected: 打印 `[ok] .../resources/cloudflared/<os>-<arch>/cloudflared`，文件存在且可执行。

- [ ] **Step 7: 提交**

```bash
git add app/scripts/fetch_cloudflared.py app/kiro_tray/cloudflared.py app/kiro_tray/provision.py app/tests/test_cloudflared.py
git commit -m "feat(app): cloudflared binary fetch + provision client + subprocess mgmt"
```

---

## Task 6: ✅ Supervisor（编排 gateway + cloudflared + 首启注册）

**Files:**
- Create: `app/kiro_tray/supervisor.py`
- Test: `app/tests/test_supervisor.py`

**说明:** Supervisor 是 UI 层唯一交互的对象。`start()` 前先检查是否已注册：
- 已注册（`config.toml` 有 hostname + run_token）→ 直接启动 gateway + cloudflared
- 未注册 → 调用 `needs_provision_callback`（由 tray/cli 注入），阻塞等用户输入共享密钥，完成注册后再启动

```python
# app/kiro_tray/supervisor.py
"""Orchestrate gateway + cloudflared and handle first-run provisioning."""
from __future__ import annotations

import time
from typing import Callable

import httpx

from . import appconfig
from .appconfig import AppCfg
from .gateway import GatewayThread
from .cloudflared import CloudflaredProcess


class Supervisor:
    def __init__(self, gateway=None, tunnel=None) -> None:
        self.gateway = gateway or GatewayThread()
        self.tunnel = tunnel or CloudflaredProcess()
        self._cfg: AppCfg | None = None
        # Injected by tray/cli: called when provisioning is needed.
        # Signature: (cfg: AppCfg) -> str   (returns shared_secret entered by user)
        self.provision_callback: Callable[[AppCfg], str] | None = None

    def _load(self) -> AppCfg:
        self._cfg = appconfig.load()
        return self._cfg

    def _wait_healthy(self, timeout: int = 30) -> bool:
        cfg = self._cfg or self._load()
        url = f"http://127.0.0.1:{cfg.gateway.port}/health"
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if httpx.get(url, timeout=3).status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            time.sleep(1)
        return False

    def _ensure_provisioned(self, cfg: AppCfg) -> None:
        """Run provision flow if not yet registered. Updates cfg and saves."""
        if appconfig.is_provisioned(cfg):
            return
        if self.provision_callback is None:
            raise RuntimeError(
                "未完成首启注册，且没有注入 provision_callback。\n"
                "请先在 config.toml 填写 [cloudflare] provision_url，"
                "然后重新启动 App 完成激活。"
            )
        shared_secret = self.provision_callback(cfg)
        from . import provision
        hostname, run_token = provision.run(cfg, shared_secret)
        cfg.cloudflare.hostname = hostname
        cfg.cloudflare.run_token = run_token
        appconfig.save(cfg)

    def start(self) -> bool:
        cfg = self._load()
        self._ensure_provisioned(cfg)
        self.gateway.start(cfg)
        healthy = self._wait_healthy()
        self.tunnel.start(cfg)
        return healthy

    def stop(self) -> None:
        self.tunnel.stop()
        self.gateway.stop()

    def restart(self) -> bool:
        self.stop()
        return self.start()

    def status(self) -> dict[str, str]:
        cfg = self._cfg or self._load()
        provisioned = appconfig.is_provisioned(cfg)
        return {
            "gateway": "running" if self.gateway.is_alive() else "stopped",
            "tunnel": "running" if self.tunnel.is_alive() else "stopped",
            "hostname": cfg.cloudflare.hostname if provisioned else "(未注册)",
        }
```

- [ ] **Step 1: 写测试**

```python
# app/tests/test_supervisor.py
from kiro_tray import supervisor, appconfig


class _FakeGateway:
    def __init__(self): self.started = False
    def start(self, cfg): self.started = True
    def stop(self): self.started = False
    def is_alive(self): return self.started


class _FakeTunnel:
    def __init__(self): self.started = False
    def start(self, cfg): self.started = True
    def stop(self): self.started = False
    def is_alive(self): return self.started


def _make_sup(monkeypatch, tmp_path, provisioned=True):
    monkeypatch.setenv("KIRO_TRAY_HOME", str(tmp_path))
    cfg = appconfig.load()
    if provisioned:
        cfg.cloudflare.hostname = "kg-test.botsonny.top"
        cfg.cloudflare.run_token = "eyJ_test"
        appconfig.save(cfg)
    s = supervisor.Supervisor(gateway=_FakeGateway(), tunnel=_FakeTunnel())
    monkeypatch.setattr(s, "_wait_healthy", lambda timeout=30: True)
    return s


def test_start_provisioned(monkeypatch, tmp_path):
    s = _make_sup(monkeypatch, tmp_path, provisioned=True)
    s.start()
    assert s.gateway.is_alive() is True
    assert s.tunnel.is_alive() is True
    assert s.status()["hostname"] == "kg-test.botsonny.top"


def test_start_not_provisioned_no_callback_raises(monkeypatch, tmp_path):
    s = _make_sup(monkeypatch, tmp_path, provisioned=False)
    try:
        s.start()
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "注册" in str(e) or "provision" in str(e).lower()


def test_start_not_provisioned_with_callback(monkeypatch, tmp_path):
    s = _make_sup(monkeypatch, tmp_path, provisioned=False)

    def fake_provision(cfg):
        cfg.cloudflare.hostname = "kg-cb.botsonny.top"
        cfg.cloudflare.run_token = "eyJ_cb"
        appconfig.save(cfg)
        raise StopIteration("mock provision complete")

    # Patch provision.run to avoid real HTTP call
    import kiro_tray.provision as pmod
    monkeypatch.setattr(pmod, "run", lambda cfg, secret: ("kg-cb.botsonny.top", "eyJ_cb"))
    s.provision_callback = lambda cfg: "fake-secret"
    s.start()
    assert s.gateway.is_alive() is True


def test_stop(monkeypatch, tmp_path):
    s = _make_sup(monkeypatch, tmp_path)
    s.start()
    s.stop()
    assert s.gateway.is_alive() is False
    assert s.tunnel.is_alive() is False
```

- [ ] **Step 2: 跑测试（注意 test_start_not_provisioned_with_callback 可能需微调）**

Run: `cd app && python -m pytest tests/test_supervisor.py -v`

- [ ] **Step 3: 提交**

```bash
git add app/kiro_tray/supervisor.py app/tests/test_supervisor.py
git commit -m "feat(app): supervisor with provision flow + cloudflared orchestration"
```

---

## Task 7: ✅ 本地 /usage 查询

**Files:**
- Create: `app/kiro_tray/usage.py`

```python
# app/kiro_tray/usage.py
"""Query the gateway's own GET /usage endpoint on localhost."""
from __future__ import annotations

import httpx

from . import appconfig


def fetch(timeout: float = 30.0) -> dict:
    cfg = appconfig.load()
    url = f"http://127.0.0.1:{cfg.gateway.port}/usage"
    headers = {"Authorization": f"Bearer {cfg.gateway.proxy_api_key}"}
    resp = httpx.get(url, headers=headers, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"/usage returned {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def format_summary(data: dict) -> str:
    sub = data.get("subscription") or "unknown"
    lines = [f"订阅: {sub}"]
    for b in data.get("breakdowns") or []:
        used = b.get("used", 0)
        limit = b.get("limit", 0)
        lines.append(f"  用量: {used} / {limit}")
    if not data.get("breakdowns"):
        lines.append("  (无用量明细)")
    return "\n".join(lines)


def format_menu_line(data: dict) -> str:
    """One-liner for the tray menu's quota row, e.g. "1732.9 / 1000".

    Uses the first breakdown. Returns "无数据" when there is none.
    """
    breakdowns = data.get("breakdowns") or []
    if not breakdowns:
        return "无数据"
    b = breakdowns[0]
    return f"{b.get('used', 0)} / {b.get('limit', 0)}"
```

- [ ] **Step 1: 写文件**
- [ ] **Step 2: 提交**

```bash
git add app/kiro_tray/usage.py
git commit -m "feat(app): local /usage query helper"
```

---

## Task 8: ✅ 托盘 UI(pystray)

**Files:**
- Create: `app/kiro_tray/tray.py`
- Create: `app/resources/icon.png`

**说明:**
- 复制项拆成三行：`_local_url()` 返回 `http://127.0.0.1:<port>/v1`，`_tunnel_url()` 返回 `https://<hostname>/v1`，「复制 Gateway 密码」直接取 `cfg.gateway.proxy_api_key`
- 更新提醒行置顶（接线见 Task 13），仅在有新版本时出现
- 首启注册通过 `provision_callback` 注入给 Supervisor，托盘里用 `icon.notify` 提示用户输入共享密钥——但 pystray 没有输入框，所以走 `simpledialog`（Tk）或退化成提示「请在终端输入」
- 实际上最简单的方式：在 `__main__.py` 启动前先检查是否已注册，未注册时跳出一个输入对话框

**菜单布局（从上到下，`---` 为分隔线）：**

```
🔔 有新版本 v0.2.0，点击下载   (仅在有更新时出现，置顶；点击打开 Release 页)
---                            (随更新行一起出现/隐藏，pystray 自动折叠首行分隔线)
网关    本地 kiro gateway        (只读，显示运行状态)
隧道    cloudflare tunnel        (只读，显示运行状态)
---
额度    1732.9 / 1000            (打开菜单即异步请求；未就绪显示「加载中…」)
---
打开配置文件
打开日志目录
---
复制本地 URL                    (http://127.0.0.1:<port>/v1)
复制 Tunnel URL                 (https://<hostname>/v1)
复制 Gateway 密码                (cfg.gateway.proxy_api_key)
---
启动 / 重启                      (运行中显示「重启」，调 sup.restart())
停止
退出
```

> 更新行的接线见 Task 13；此处仅占位说明它在菜单中的位置（置顶 + 分隔线）。

**额度行的 loading 机制（关键）：** pystray 菜单项的文案可以是一个**可调用对象**，每次菜单显示时都会重新求值。所以额度行的文案函数读一个缓存变量：

- 缓存为空 → 显示「额度  加载中…」，**同时**丢一个后台线程去 `usage.fetch()`，结果写回缓存
- 后台线程拿到结果后调 `icon.update_menu()` 触发菜单刷新，下次显示即为真实数字
- 每次打开菜单都重新触发一次后台拉取（保证数字新鲜），但 UI 立刻用上一次的缓存值渲染，不阻塞
- 格式：`used / limit`（如 `1732.9 / 1000`）；多个 breakdown 时显示第一个，或合计

**状态行文案：** 网关行恒为「网关　本地 kiro gateway」+ 状态（running/stopped），隧道行恒为「隧道　cloudflare tunnel」+ 状态。状态用后缀或图标区分。

- [x] **Step 1: 写 `tray.py`**

```python
# app/kiro_tray/tray.py
"""System-tray / menu-bar UI via pystray."""
from __future__ import annotations

import threading
import webbrowser
from pathlib import Path

from . import appconfig, paths, usage
from .supervisor import Supervisor


class TrayUnavailable(RuntimeError):
    pass


def _load_icon():
    from PIL import Image
    icon_path = Path(__file__).parent.parent / "resources" / "icon.png"
    if icon_path.exists():
        return Image.open(icon_path)
    return Image.new("RGB", (64, 64), (60, 120, 220))


def _local_url(cfg) -> str:
    return f"http://127.0.0.1:{cfg.gateway.port}/v1"


def _tunnel_url(cfg) -> str:
    if cfg.cloudflare.hostname:
        return f"https://{cfg.cloudflare.hostname}/v1"
    return ""


def _base_url(cfg) -> str:
    # 启动通知里优先报 tunnel 地址，没有就退回本地。
    return _tunnel_url(cfg) or _local_url(cfg)


def _ask_shared_secret(cfg) -> str:
    """Prompt user for shared secret. Tries Tk dialog, falls back to print."""
    try:
        import tkinter as tk
        from tkinter import simpledialog
        root = tk.Tk()
        root.withdraw()
        secret = simpledialog.askstring(
            "Kiro Tray - 首次激活",
            f"请输入激活码（共享密钥）\nWorker: {cfg.cloudflare.provision_url}",
            parent=root,
        )
        root.destroy()
        if not secret:
            raise RuntimeError("用户取消了激活。")
        return secret
    except ImportError:
        raise RuntimeError("无法弹出输入框，请改用 CLI 模式完成首次激活（kiro-tray --cli）。")


class _UsageCache:
    """Thread-safe cache for the /usage result, rendered by the menu line.

    State machine: None (never fetched) -> "loading" -> text | error.
    Each time the menu opens we kick a background refresh, but the line
    renders immediately from whatever is cached so the UI never blocks.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._text: str | None = None      # None = never fetched
        self._inflight = False

    def display(self) -> str:
        with self._lock:
            return self._text if self._text is not None else "加载中…"

    def refresh(self, icon) -> None:
        with self._lock:
            if self._inflight:
                return
            self._inflight = True
            if self._text is None:
                self._text = "加载中…"

        def _work():
            try:
                data = usage.fetch()
                text = usage.format_menu_line(data)   # e.g. "1732.9 / 1000"
            except Exception:
                text = "获取失败"
            with self._lock:
                self._text = text
                self._inflight = False
            try:
                icon.update_menu()                    # re-render with fresh value
            except Exception:
                pass

        threading.Thread(target=_work, daemon=True).start()


def run() -> None:
    """Start the tray loop. Raises TrayUnavailable if no backend works."""
    try:
        import pystray
    except Exception as e:
        raise TrayUnavailable(str(e))

    sup = Supervisor()
    sup.provision_callback = _ask_shared_secret
    usage_cache = _UsageCache()

    def _notify(icon, title, msg):
        try:
            icon.notify(msg, title)
        except Exception:
            pass

    def on_start_or_restart(icon, _item):
        # Same menu slot: "启动" when stopped, "重启" when already running.
        restarting = sup.status()["gateway"] == "running"
        def _work():
            try:
                if restarting:
                    sup.restart()
                    verb = "已重启"
                else:
                    sup.start()
                    verb = "已启动"
                cfg = appconfig.load()
                _notify(icon, "Kiro Tray", f"{verb}\n{_tunnel_url(cfg)}")
            except Exception as e:
                _notify(icon, "Kiro Tray 错误", str(e)[:200])
            icon.update_menu()
        threading.Thread(target=_work, daemon=True).start()

    def on_stop(icon, _item):
        sup.stop()
        _notify(icon, "Kiro Tray", "网关已停止")
        icon.update_menu()

    def _copy(icon, value, label):
        try:
            import pyperclip
            pyperclip.copy(value)
            _notify(icon, "Kiro Tray", f"已复制{label}: {value}")
        except Exception:
            _notify(icon, label, value)

    def on_copy_local_url(icon, _item):
        cfg = appconfig.load()
        _copy(icon, _local_url(cfg), "本地 URL")

    def on_copy_tunnel_url(icon, _item):
        cfg = appconfig.load()
        _copy(icon, _tunnel_url(cfg), "Tunnel URL")

    def on_copy_password(icon, _item):
        cfg = appconfig.load()
        _copy(icon, cfg.gateway.proxy_api_key, "Gateway 密码")

    def on_open_config(_icon, _item):
        webbrowser.open(paths.config_file().as_uri())

    def on_open_logs(_icon, _item):
        webbrowser.open(paths.log_dir().as_uri())

    def on_quit(icon, _item):
        sup.stop()
        icon.stop()

    # --- status line text callables (re-evaluated each time menu shows) ---
    def gateway_line(_item):
        s = sup.status()
        return f"网关　本地 kiro gateway　[{s['gateway']}]"

    def tunnel_line(_item):
        s = sup.status()
        return f"隧道　cloudflare tunnel　[{s['tunnel']}]"

    # The usage line's text callable doubles as the refresh trigger: pystray
    # re-evaluates every item's text each time the menu is opened, so reading
    # the line kicks a background refresh. display() returns the cached value
    # instantly (or "加载中…"), and the background thread calls
    # icon.update_menu() when fresh data arrives.
    def usage_line(_item):
        usage_cache.refresh(icon)
        return f"额度　{usage_cache.display()}"

    # Start/restart is one menu item with dynamic text: shows "重启" when the
    # gateway is already running (calls sup.restart()), "启动" otherwise.
    def start_line(_item):
        return "重启" if sup.status()["gateway"] == "running" else "启动"

    # --- update notice (Task 13): only shown when a newer release exists ---
    # _update_info is filled by a background check kicked off at startup
    # (see updates.check below). Default None = nothing to show.
    _update = {"info": None}

    def _kick_update_check():
        def _work():
            try:
                from . import updates
                info = updates.check()
                if info.update_available:
                    _update["info"] = info
                    icon.update_menu()
            except Exception:
                pass  # silent failure, never bother the user
        threading.Thread(target=_work, daemon=True).start()

    def update_visible(_item) -> bool:
        return _update["info"] is not None

    def update_line(_item) -> str:
        info = _update["info"]
        return f"🔔 有新版本 {info.latest}，点击下载" if info else ""

    def on_update(icon, _item):
        info = _update["info"]
        if info:
            webbrowser.open(info.release_url)

    menu = pystray.Menu(
        # Update notice goes first; the separator below it auto-collapses when
        # the notice is hidden (pystray drops leading/adjacent separators).
        pystray.MenuItem(update_line, on_update, visible=update_visible),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(gateway_line, None, enabled=False),
        pystray.MenuItem(tunnel_line, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(usage_line, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("打开配置文件", on_open_config),
        pystray.MenuItem("打开日志目录", on_open_logs),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("复制本地 URL", on_copy_local_url),
        pystray.MenuItem("复制 Tunnel URL", on_copy_tunnel_url),
        pystray.MenuItem("复制 Gateway 密码", on_copy_password),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(start_line, on_start_or_restart),
        pystray.MenuItem("停止", on_stop),
        pystray.MenuItem("退出", on_quit),
    )

    icon = pystray.Icon("kiro-tray", _load_icon(), "Kiro Gateway", menu)
    threading.Thread(target=sup.start, daemon=True).start()
    _kick_update_check()        # startup check; updates.check() handles 24h caching
    icon.run()
```

- [x] **Step 2: 生成占位图标**

```bash
cd app && python -c "
from PIL import Image; import os
os.makedirs('resources', exist_ok=True)
Image.new('RGB', (64, 64), (60, 120, 220)).save('resources/icon.png')
print('icon written')
"
```

- [x] **Step 3: 提交**

```bash
git add app/kiro_tray/tray.py app/resources/icon.png
git commit -m "feat(app): pystray tray UI with cloudflare base url + provision dialog"
```

---

## Task 9: ✅ 入口分发 + CLI 兜底

**Files:**
- Create: `app/kiro_tray/cli.py`
- Create: `app/kiro_tray/__main__.py`

**说明:** CLI 模式下首启注册通过 `input()` 读取共享密钥，比 Tk 对话框更直接，在无图形界面环境下是首选路径。

- [x] **Step 1: 写 `cli.py`**

```python
# app/kiro_tray/cli.py
"""Headless fallback when no tray is available (typically Ubuntu/GNOME)."""
from __future__ import annotations

import signal
import sys
import threading

from . import appconfig, paths, usage
from .supervisor import Supervisor


def _base_url(cfg) -> str:
    if cfg.cloudflare.hostname:
        return f"https://{cfg.cloudflare.hostname}/v1"
    return f"http://127.0.0.1:{cfg.gateway.port}/v1"


def _ask_shared_secret_cli(cfg) -> str:
    print("\n=== Kiro Tray 首次激活 ===")
    print(f"Worker URL: {cfg.cloudflare.provision_url or '(未设置，请先填 config.toml)'}")
    print("请输入激活码（共享密钥）：", end="", flush=True)
    secret = input().strip()
    if not secret:
        raise RuntimeError("激活码为空，已取消。")
    return secret


def run() -> int:
    cfg = appconfig.load()
    sup = Supervisor()
    sup.provision_callback = _ask_shared_secret_cli

    print("Kiro Gateway (CLI 模式)")
    print(f"  配置文件: {paths.config_file()}")
    print(f"  日志目录: {paths.log_dir()}")
    print("  启动中...")

    try:
        sup.start()
    except Exception as e:
        print(f"  启动失败: {e}", file=sys.stderr)
        return 1

    cfg = appconfig.load()  # reload after potential provision
    print(f"  Base URL: {_base_url(cfg)}")
    print("  按 Ctrl-C 退出；输入 u + 回车查额度。")

    stop = threading.Event()

    def _sig(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    def _input_loop():
        for line in sys.stdin:
            if line.strip().lower() == "u":
                try:
                    print(usage.format_summary(usage.fetch()))
                except Exception as e:
                    print(f"查额度失败: {e}")
            if stop.is_set():
                break

    threading.Thread(target=_input_loop, daemon=True).start()
    stop.wait()
    print("\n  停止中...")
    sup.stop()
    return 0
```

- [x] **Step 2: 写 `__main__.py`**

```python
# app/kiro_tray/__main__.py
"""Entry dispatch: tray by default, CLI fallback, plus --print-config."""
from __future__ import annotations

import argparse
import os
import sys

from . import appconfig, paths


def _has_display() -> bool:
    if sys.platform.startswith("linux"):
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return True


def main() -> int:
    parser = argparse.ArgumentParser(prog="kiro-tray")
    parser.add_argument("--cli", action="store_true", help="force headless CLI mode")
    parser.add_argument("--print-config", action="store_true",
                        help="print config file path and exit")
    args = parser.parse_args()

    if args.print_config:
        appconfig.load()
        print(paths.config_file())
        return 0

    if not args.cli and _has_display():
        try:
            from . import tray
            tray.run()
            return 0
        except tray.TrayUnavailable as e:
            print(f"[tray unavailable: {e}] 退化到 CLI 模式", file=sys.stderr)

    from . import cli
    return cli.run()


if __name__ == "__main__":
    raise SystemExit(main())
```

- [x] **Step 3: 冒烟验证入口**

```bash
cd app && python -m kiro_tray --print-config
# Expected: 打印 config.toml 路径

cd app && python -m kiro_tray --cli
# Expected: 若 config.toml 已填 provision_url + profile_arn，完成注册并打印 Base URL
```

- [x] **Step 4: 提交**

```bash
git add app/kiro_tray/cli.py app/kiro_tray/__main__.py
git commit -m "feat(app): entry dispatch with tray + CLI fallback, provision via input()"
```

---

## Task 10: ⬜ PyInstaller spec

**Files:**
- Create: `app/packaging/kiro_tray.spec`

**说明:** 把 vendored gateway、cloudflared 二进制、icon 打进包。`frpc/` 子目录已改为 `cloudflared/`。

- [ ] **Step 1: 写 `app/packaging/kiro_tray.spec`**

```python
# app/packaging/kiro_tray.spec
# Run from app/ dir: pyinstaller packaging/kiro_tray.spec
# Prereq (CI does this): python scripts/vendor_sync.py && python scripts/fetch_cloudflared.py
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

APP = Path(SPECPATH).resolve().parent          # app/
VENDOR = APP / "kiro_tray" / "vendor"
RES = APP / "resources"

# --- platform → cloudflared subdir + exe name ---
if sys.platform.startswith("win"):
    _cf_sub, _cf_exe, _name = "windows-amd64", "cloudflared.exe", "KiroTray"
    _console = False
elif sys.platform == "darwin":
    import platform as _pf
    _arch = "arm64" if _pf.machine() == "arm64" else "amd64"
    _cf_sub, _cf_exe, _name = f"darwin-{_arch}", "cloudflared", "KiroTray"
    _console = False
else:
    _cf_sub, _cf_exe, _name = "linux-amd64", "cloudflared", "kiro-tray"
    _console = True

datas = [
    (str(VENDOR), "vendor"),
    (str(RES / "cloudflared" / _cf_sub / _cf_exe), f"resources/cloudflared/{_cf_sub}"),
    (str(RES / "icon.png"), "resources"),
]
binaries = []
hiddenimports = []

for pkg in ("tiktoken", "tiktoken_ext", "uvicorn", "websockets", "httptools", "fastapi", "pystray"):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hiddenimports += h

hiddenimports += collect_submodules("tiktoken_ext")

a = Analysis(
    [str(APP / "kiro_tray" / "__main__.py")],
    pathex=[str(APP), str(VENDOR)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "hypothesis"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name=_name,
    console=_console,
    icon=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name=_name)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{_name}.app",
        icon=None,
        bundle_identifier="dev.kiro.tray",
        info_plist={"LSUIElement": True},
    )
```

> 注意：`tkinter` 加入了 `excludes`，但 tray.py 里 `_ask_shared_secret` 导入了 `tkinter`。PyInstaller 里加入 excludes 会让 tkinter import 在打包时不报错（因为有 try/except），但实际运行时 tkinter 不可用，会走 `ImportError` 分支提示「请用 CLI 模式」。这是预期行为——macOS 系统 Python 自带 tkinter，如果想让 macOS 支持对话框，把 `tkinter` 从 excludes 里去掉。

- [ ] **Step 2: 本地冒烟**

```bash
cd app
python scripts/vendor_sync.py
python scripts/fetch_cloudflared.py
pyinstaller packaging/kiro_tray.spec --noconfirm
./dist/KiroTray/KiroTray --print-config  # macOS
```

- [ ] **Step 3: 提交**

```bash
git add app/packaging/kiro_tray.spec
git commit -m "build(app): PyInstaller spec collecting vendored gateway + cloudflared"
```

---

## Task 11: ⬜ 产物打包脚本 + App README

**Files:**
- Create: `app/packaging/make_dist.py`
- Create: `app/README.md`

- [ ] **Step 1: 写 `app/packaging/make_dist.py`**

```python
# app/packaging/make_dist.py
from __future__ import annotations

import hashlib
import platform
import shutil
import sys
import tarfile
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
DIST = APP / "dist"
OUT = APP / "release"
sys.path.insert(0, str(APP))
from kiro_tray import __version__ as VER  # noqa: E402


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    OUT.mkdir(exist_ok=True)
    if sys.platform == "darwin":
        arch = "arm64" if platform.machine() == "arm64" else "amd64"
        out = OUT / f"KiroTray-{VER}-macos-{arch}.zip"
        shutil.make_archive(str(out.with_suffix("")), "zip", DIST, "KiroTray.app")
    elif sys.platform.startswith("win"):
        out = OUT / f"KiroTray-{VER}-windows-amd64.zip"
        shutil.make_archive(str(out.with_suffix("")), "zip", DIST, "KiroTray")
    else:
        out = OUT / f"kiro-tray-{VER}-linux-amd64.tar.gz"
        with tarfile.open(out, "w:gz") as tar:
            tar.add(DIST / "kiro-tray", arcname="kiro-tray")

    digest = _sha256(out)
    (out.parent / (out.name + ".sha256")).write_text(f"{digest}  {out.name}\n")
    print(f"[ok] {out.name}  sha256={digest}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 写 `app/README.md`**

````markdown
# Kiro Tray App

把 kiro-gateway 跑成 Mac / Windows / Linux 的本地托盘小工具，无需 Docker。
进程内跑网关，子进程跑 cloudflared 把本机网关经 Cloudflare 网络暴露为
`https://kg-<你的用户名>.botsonny.top/v1`，供 Cursor 直接使用。

> 与仓库根目录的 Docker 部署是两条独立的线，互不影响。

## 用户怎么用（拿到发布包后）

1. 从 GitHub Releases 下载对应平台的包并解压：
   - macOS: `KiroTray-<ver>-macos-<arch>.zip` → 解出 `KiroTray.app`，拖入 Applications
   - Windows: `KiroTray-<ver>-windows-amd64.zip` → 解出 `KiroTray/KiroTray.exe`
   - Linux: `kiro-tray-<ver>-linux-amd64.tar.gz` → 解出 `kiro-tray/kiro-tray`

2. 确保本机已用 Kiro IDE 登录过（存在 `~/.aws/sso/cache/kiro-auth-token.json`）。

3. 首次运行 → 菜单选「打开配置文件」（或 `kiro-tray --print-config`），
   在 `config.toml` 里填写：

   ```toml
   [gateway]
   profile_arn = "arn:aws:codewhisperer:us-east-1:<account>:profile/<id>"
   proxy_api_key = "<自己生成一个强随机串>"

   [cloudflare]
   provision_url = "https://kiro-gateway-provision.botsonny.top"
   ```

4. 重新启动 App → 弹出激活码输入框（托盘模式）或命令行提示（CLI 模式），
   输入管理员发给你的激活码，App 自动完成注册，`hostname` 和 `run_token` 自动写入 `config.toml`。

5. 注册完成后 App 自动启动网关和隧道。从托盘菜单「复制 Tunnel URL」和
   「复制 Gateway 密码」，填进 Cursor → Settings → Models → OpenAI API Key & Base URL：
   - **API Key**: 托盘「复制 Gateway 密码」（即 `config.toml` 里的 `proxy_api_key`）
   - **Base URL**: 托盘「复制 Tunnel URL」（即 `https://kg-<你的用户名>.botsonny.top/v1`）
   - 本机调试也可用「复制本地 URL」（`http://127.0.0.1:<port>/v1`）

6. 以后每次开机启动 App 即可，无需再输激活码。

## 开发者怎么构建

```bash
cd app
python3.11 -m venv .venv && . .venv/bin/activate
pip install -r requirements-build.txt
python scripts/vendor_sync.py           # 拉上游 pin sha + 打补丁
python scripts/fetch_cloudflared.py     # 下载当前平台 cloudflared
pyinstaller packaging/kiro_tray.spec --noconfirm
python packaging/make_dist.py           # 产物在 app/release/
```

三平台发布包由 GitHub Actions matrix 自动构建，打 `app-v*` tag 即触发。

## 配置项（config.toml）

| 段 | 键 | 说明 |
|---|---|---|
| gateway | profile_arn | CodeWhisperer profile ARN（必填） |
| gateway | proxy_api_key | 客户端 Bearer key（必填，建议强随机） |
| gateway | port | 本机监听端口，默认 18000 |
| gateway | api_region | 默认 us-east-1 |
| gateway | kiro_creds_file | 留空用默认 SSO 缓存路径 |
| gateway | fake_reasoning | 是否注入思考标签，默认 false |
| cloudflare | provision_url | Worker URL（首次填写，此后不用改） |
| cloudflare | hostname | 自动写入，勿手改 |
| cloudflare | run_token | 自动写入，勿手改 |
````

- [ ] **Step 3: 提交**

```bash
git add app/packaging/make_dist.py app/README.md
git commit -m "build(app): dist packaging script + app README"
```

---

## Task 12: ⬜ GitHub Actions 三平台 matrix 构建 + Release

**Files:**
- Create: `.github/workflows/build-app.yml`

- [ ] **Step 1: 写 `.github/workflows/build-app.yml`**

```yaml
name: build-app

on:
  push:
    tags: ["app-v*"]
  pull_request:
    paths: ["app/**", ".github/workflows/build-app.yml"]
  workflow_dispatch:

permissions:
  contents: write

jobs:
  build:
    name: build (${{ matrix.os }})
    runs-on: ${{ matrix.os }}
    strategy:
      fail-fast: false
      matrix:
        os: [macos-latest, windows-latest, ubuntu-latest]
    defaults:
      run:
        working-directory: app
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install Linux GUI/tray deps
        if: runner.os == 'Linux'
        run: |
          sudo apt-get update
          sudo apt-get install -y libgirepository1.0-dev gir1.2-appindicator3-0.1 \
            gobject-introspection libcairo2-dev pkg-config
        working-directory: ${{ github.workspace }}

      - name: Install build deps
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements-build.txt

      - name: Vendor upstream + patch
        run: python scripts/vendor_sync.py

      - name: Fetch cloudflared (current platform)
        run: python scripts/fetch_cloudflared.py --current-only

      - name: Run tests
        run: python -m pytest -q

      - name: Build with PyInstaller
        run: pyinstaller packaging/kiro_tray.spec --noconfirm

      - name: Package dist
        run: python packaging/make_dist.py

      - name: Upload artifact
        uses: actions/upload-artifact@v4
        with:
          name: kiro-tray-${{ matrix.os }}
          path: app/release/*

  release:
    name: publish release
    needs: build
    if: startsWith(github.ref, 'refs/tags/app-v')
    runs-on: ubuntu-latest
    steps:
      - name: Download all artifacts
        uses: actions/download-artifact@v4
        with:
          path: artifacts

      - name: Create GitHub Release
        uses: softprops/action-gh-release@v2
        with:
          files: artifacts/**/*
          generate_release_notes: true
```

- [ ] **Step 2: 提交并触发**

```bash
git add .github/workflows/build-app.yml
git commit -m "ci(app): three-platform matrix build + release on app-v* tag"
git push -u origin HEAD
```

- [ ] **Step 3: 打 tag 验证 Release**

```bash
git tag app-v0.1.0
git push origin app-v0.1.0
```

---

## Task 13: ✅ 更新提醒（GitHub Release 版本检查）

**Files:**
- Create: `app/kiro_tray/updates.py`
- Test: `app/tests/test_updates.py`
- Modify: `app/kiro_tray/tray.py`（菜单加「检查更新」结果行）
- Modify: `app/kiro_tray/cli.py`（启动时打印一次更新提示）

**说明（行为约定，严格按此实现）：**
- **何时查：** App 启动时查一次；之后每 **24 小时**查一次。
- **缓存：** 结果（最新版本号 + 查询时间戳）写到 data 目录的 `update_check.json`。下次启动若距上次查询不足 24h，直接读缓存、不发网络请求。
- **静默失败：** 任何网络/解析错误都吞掉，不打扰用户、不弹错；菜单要么显示「已是最新」要么不显示更新行。
- **不自动下载：** 仅在菜单栏多一行「🔔 有新版本 vX.Y.Z，点击下载」，点击用浏览器打开 Release 页。绝不自动替换二进制。
- **数据源：** `GET https://api.github.com/repos/<owner>/<repo>/releases/latest`，读 `tag_name`（形如 `app-v0.2.0`）。仓库常量取自 `kiro_tray.GITHUB_REPO`。

- [ ] **Step 1: 写失败测试**

```python
# app/tests/test_updates.py
from kiro_tray import updates


def test_parse_version_strips_prefix():
    assert updates._parse_version("app-v0.2.0") == (0, 2, 0)
    assert updates._parse_version("v1.2.3") == (1, 2, 3)
    assert updates._parse_version("0.1.0") == (0, 1, 0)


def test_is_newer():
    assert updates._is_newer("0.1.0", "0.2.0") is True
    assert updates._is_newer("0.2.0", "0.2.0") is False
    assert updates._is_newer("0.2.0", "0.1.9") is False
    assert updates._is_newer("0.1.0", "1.0.0") is True


def test_cache_roundtrip_and_ttl(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_TRAY_HOME", str(tmp_path))
    # No cache yet -> should_check True
    assert updates._should_check() is True
    updates._write_cache(latest="0.2.0")
    # Just wrote -> within TTL -> should_check False
    assert updates._should_check() is False
    cached = updates._read_cache()
    assert cached["latest"] == "0.2.0"


def test_check_uses_cache_when_fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_TRAY_HOME", str(tmp_path))
    updates._write_cache(latest="9.9.9")
    # fresh cache -> no HTTP call, returns cached latest
    def _boom(*a, **k):
        raise AssertionError("should not hit network when cache is fresh")
    monkeypatch.setattr(updates.httpx, "get", _boom)
    info = updates.check(current="0.1.0")
    assert info.latest == "9.9.9"
    assert info.update_available is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd app && python -m pytest tests/test_updates.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'kiro_tray.updates'`

- [ ] **Step 3: 写 `updates.py`**

```python
# app/kiro_tray/updates.py
"""Lightweight update check against GitHub Releases.

Behavior (see plan Task 13):
  - check once on startup, then at most once per 24h (cached on disk)
  - cache file: <data_dir>/update_check.json
  - all failures are swallowed silently (never bother the user)
  - never auto-downloads; UI just surfaces a "new version" menu line
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import GITHUB_REPO, __version__, paths

_TTL_SECONDS = 24 * 60 * 60
_CACHE_NAME = "update_check.json"
_RELEASE_API = "https://api.github.com/repos/{repo}/releases/latest"
_RELEASE_PAGE = "https://github.com/{repo}/releases/latest"


@dataclass
class UpdateInfo:
    current: str
    latest: str | None
    update_available: bool
    release_url: str


def _cache_file() -> Path:
    return paths.data_dir() / _CACHE_NAME


def _parse_version(tag: str) -> tuple[int, int, int]:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", tag or "")
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _is_newer(current: str, latest: str) -> bool:
    return _parse_version(latest) > _parse_version(current)


def _read_cache() -> dict | None:
    try:
        return json.loads(_cache_file().read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_cache(latest: str | None) -> None:
    try:
        paths.ensure_dirs()
        _cache_file().write_text(
            json.dumps({"latest": latest, "checked_at": time.time()}),
            encoding="utf-8",
        )
    except Exception:
        pass


def _should_check() -> bool:
    cached = _read_cache()
    if not cached:
        return True
    return (time.time() - cached.get("checked_at", 0)) >= _TTL_SECONDS


def _fetch_latest() -> str | None:
    url = _RELEASE_API.format(repo=GITHUB_REPO)
    resp = httpx.get(url, timeout=8, headers={"Accept": "application/vnd.github+json"})
    if resp.status_code != 200:
        return None
    return resp.json().get("tag_name")


def check(current: str | None = None, force: bool = False) -> UpdateInfo:
    """Return update info. Uses cache unless stale (or force=True).

    Never raises: on any failure returns update_available=False.
    """
    current = current or __version__
    release_url = _RELEASE_PAGE.format(repo=GITHUB_REPO)
    try:
        if force or _should_check():
            latest = _fetch_latest()
            if latest is not None:
                _write_cache(latest)
            else:                       # failed fetch: fall back to cache if any
                latest = (_read_cache() or {}).get("latest")
        else:
            latest = (_read_cache() or {}).get("latest")
    except Exception:
        latest = (_read_cache() or {}).get("latest")

    available = bool(latest) and _is_newer(current, latest)
    return UpdateInfo(current=current, latest=latest,
                      update_available=available, release_url=release_url)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd app && python -m pytest tests/test_updates.py -v`
Expected: 4 passed。

- [x] **Step 5: 接线进托盘菜单（`tray.py`）**

**注意：Task 8 的 `tray.py` 代码已包含完整的更新行接线**（`_update`/`_kick_update_check`/`update_line`/`update_visible`/`on_update`，菜单第一行 + `_kick_update_check()` 在 `icon.run()` 前调用）。这里只是说明其行为约定，实现 Task 8 时照其代码即可，无需重复添加：

- App 启动后丢一个后台线程调 `updates.check()`（内部 24h 缓存），有更新才把 info 存进可变容器并 `icon.update_menu()`。
- 菜单用 `MenuItem(text_callable, on_click, visible=visible_callable)`：`visible` 回调返回是否有更新，只有有更新时这一行才显示。
- 放在**菜单第一行**（最显眼），下面跟一条分隔线；无更新时该行隐藏、分隔线被 pystray 自动折叠。

> 注：pystray 的 `visible` 接受可调用对象，返回 False 时该项不渲染。这样「无更新时菜单无额外噪音、有更新时顶部多一行」自然成立。

- [x] **Step 6: 接线进 CLI（`cli.py`）**

CLI 启动打印 Base URL 之后，加一次性提示（静默失败）：

```python
# 片段：加在 cli.run() 打印 Base URL 之后
try:
    from . import updates
    info = updates.check()
    if info.update_available:
        print(f"  🔔 有新版本 {info.latest}：{info.release_url}")
except Exception:
    pass
```

- [x] **Step 7: 提交**

```bash
git add app/kiro_tray/updates.py app/tests/test_updates.py app/kiro_tray/tray.py app/kiro_tray/cli.py
git commit -m "feat(app): lightweight GitHub release update check (startup + 24h cache)"
```

---

## Task 14: ⬜ Homebrew tap + cask（macOS 一键安装）

**Files:**
- Create: `homebrew-tap/Casks/kiro-tray.rb`（在一个**独立的** `homebrew-<tap>` GitHub 仓库里，不在本仓库）
- Create: `homebrew-tap/README.md`
- Modify: `.github/workflows/build-app.yml`（Release 后自动 bump cask 的 version + sha256）

**说明（务必先读，关系到能不能用）：**
- 我们的产物是 GUI `.app`，对应 Homebrew **Cask**（不是 formula）。
- 用**自建 tap**：一个名为 `homebrew-<tap>` 的普通 GitHub 仓库（如 `<owner>/homebrew-kiro`）。用户：
  ```bash
  brew tap <owner>/kiro          # 对应仓库 <owner>/homebrew-kiro
  brew install --cask kiro-tray
  ```
- **与「不签名」的硬冲突（关键）：** Homebrew 默认给下载物打 quarantine 标记。未签名+未公证的 `.app` 经 `brew install --cask` 装上后，双击仍会被 Gatekeeper 拦。新版 Homebrew **不允许** cask 用 `quarantine false` 自行关闭。所以未签名期间，cask 仍可用，但要让用户首次执行一条 `xattr` 去隔离命令（写进 tap README）。签名+公证就绪后，这条限制自动消失，cask 结构无需改动。
- 因此本 Task **现在就建好 tap + cask + CI 自动 bump**，未签名期间靠 README 的 `xattr` 提示兜底；属于「就绪即顺滑」。

- [ ] **Step 1: 建独立 tap 仓库并写 cask 文件**

在 GitHub 上新建仓库 `<owner>/homebrew-kiro`（名字必须以 `homebrew-` 开头）。放入 `Casks/kiro-tray.rb`：

```ruby
# Casks/kiro-tray.rb
cask "kiro-tray" do
  version "0.1.0"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"

  url "https://github.com/<owner>/<repo>/releases/download/app-v#{version}/KiroTray-#{version}-macos-arm64.zip"
  name "Kiro Tray"
  desc "Local tray app for kiro-gateway"
  homepage "https://github.com/<owner>/<repo>"

  # 我们只发 Apple Silicon 包；若以后出 Intel 包，可用 on_arm/on_intel 分支不同 url+sha256。
  depends_on macos: ">= :big_sur"

  app "KiroTray.app"

  # 未签名期间：装完首次需要去掉 quarantine，否则 Gatekeeper 拦截。
  # 签名+公证就绪后可删除这段 caveats。
  caveats <<~EOS
    本 App 暂未签名/公证。首次使用前请执行一次：
      xattr -dr com.apple.quarantine "#{appdir}/KiroTray.app"
    或在「系统设置 → 隐私与安全性」点「仍要打开」。
  EOS
end
```

> 注意：cask 的 `version` 与 Task 12 的 Release tag 命名 `app-v<version>` 对应；`url` 里的产物名与 Task 11 `make_dist.py` 产出的 `KiroTray-<ver>-macos-arm64.zip` 必须逐字一致。

- [ ] **Step 2: 写 tap README**

```markdown
# homebrew-kiro

Homebrew tap for Kiro Tray.

## 安装

```bash
brew tap <owner>/kiro
brew install --cask kiro-tray
```

## 首次打开（App 暂未签名）

装完后首次需去掉隔离标记：

```bash
xattr -dr com.apple.quarantine "/Applications/KiroTray.app"
```

或右键 App →「打开」，在弹窗里确认一次。签名+公证完成后，此步骤不再需要。

## 升级

```bash
brew update && brew upgrade --cask kiro-tray
```
```

- [ ] **Step 3: CI 自动 bump cask（Release 后）**

在 `.github/workflows/build-app.yml` 的 `release` job 之后追加一个 job，发完 Release 自动更新 tap 仓库里的 cask。需要一个有 tap 仓库写权限的 PAT，存为本仓库 secret `TAP_REPO_TOKEN`。

```yaml
  bump-cask:
    name: bump homebrew cask
    needs: release
    if: startsWith(github.ref, 'refs/tags/app-v')
    runs-on: ubuntu-latest
    steps:
      - name: Compute version + sha256
        id: meta
        run: |
          VERSION="${GITHUB_REF_NAME#app-v}"
          echo "version=$VERSION" >> "$GITHUB_OUTPUT"
          ASSET="KiroTray-$VERSION-macos-arm64.zip"
          URL="https://github.com/${{ github.repository }}/releases/download/app-v$VERSION/$ASSET"
          curl -fL "$URL" -o "$ASSET"
          echo "sha256=$(shasum -a 256 "$ASSET" | cut -d' ' -f1)" >> "$GITHUB_OUTPUT"

      - name: Checkout tap repo
        uses: actions/checkout@v4
        with:
          repository: <owner>/homebrew-kiro
          token: ${{ secrets.TAP_REPO_TOKEN }}
          path: tap

      - name: Update cask
        run: |
          CASK="tap/Casks/kiro-tray.rb"
          sed -i -E "s/version \"[^\"]+\"/version \"${{ steps.meta.outputs.version }}\"/" "$CASK"
          sed -i -E "s/sha256 \"[^\"]+\"/sha256 \"${{ steps.meta.outputs.sha256 }}\"/" "$CASK"
          cd tap
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add Casks/kiro-tray.rb
          git commit -m "kiro-tray ${{ steps.meta.outputs.version }}"
          git push
```

> 说明：`GITHUB_REF_NAME` 形如 `app-v0.2.0`，剥掉 `app-v` 前缀得到 `version`。`TAP_REPO_TOKEN` 需对 `<owner>/homebrew-kiro` 有 `contents:write`（fine-grained PAT 即可）。只发 arm64 包，所以只 bump 一个 sha256；以后加 Intel 包要相应扩展。

- [ ] **Step 4: 本地验证 cask 语法（可选）**

```bash
brew tap <owner>/kiro
brew install --cask kiro-tray
# 或仅校验文件：brew audit --cask --tap <owner>/kiro kiro-tray
```
Expected: 安装成功，`/Applications/KiroTray.app` 存在；按 caveats 执行 xattr 后可正常打开。

- [ ] **Step 5: 提交**（在 tap 仓库 + 本仓库各自提交）

```bash
# tap 仓库
git add Casks/kiro-tray.rb README.md && git commit -m "feat: kiro-tray cask"
# 本仓库
git add .github/workflows/build-app.yml
git commit -m "ci(app): auto-bump homebrew cask after release"
```

---

## Self-Review

**Spec coverage（对照需求逐条）：**
- 不用 Docker、本地可运行 → Task 4（进程内 uvicorn）+ Task 9（入口）✅
- 缩到托盘/菜单栏 → Task 8（pystray）✅
- Win / Mac / Linux 都支持 → Task 12（三平台 matrix）+ Task 8/9（Linux CLI 兜底）✅
- 隧道用 Cloudflare → Task 5（cloudflared 子进程）+ Task 0（Worker 签发）✅
- 「打开即用、填 Kiro token、一次激活码」→ Task 6（provision_callback）+ Task 5（provision.py）✅
- 管理员不给大权限 key → Worker Secrets 只存服务端，用户只拿 per-tunnel run_token ✅
- 自动签发子域名 → Task 0 Worker 三步 API（建 tunnel + 配 ingress + 建 CNAME）✅
- 配置走文件 → Task 3（config.toml）✅
- Linux CLI fallback → Task 9（`TrayUnavailable` → cli）✅
- GitHub Actions 出包 → Task 12 ✅
- 复用现有别名 + /usage 补丁 → Task 2（vendor + patch）+ Task 7（usage）✅
- 更新提醒（启动查一次 + 24h 周期 + 缓存 + 静默失败）→ Task 13（updates.py + 菜单行接线）✅
- Homebrew 安装（`brew tap` + `brew install --cask`）→ Task 14（自建 tap + cask + CI 自动 bump）✅

**关键跨 Task 约定（实现时必须遵守）：**
1. Task 2 的 `add_usage_endpoint.py` 中 `ENDPOINT_CODE` 必须逐字复制根 `patches/add_usage_endpoint.py` 的完整字符串，不可改写。
2. Task 10 spec 的 `_candidate_dirs()`（cloudflared.py）和 `_candidate_vendor_roots()`（gateway.py）均已处理 `sys._MEIPASS`（PyInstaller 冻结态），与 spec 的数据布局一致，实现时不要破坏这个关系。
3. Task 0 Worker 返回的 `run_token` 只在 201 首次响应里出现，Task 6 Supervisor 的 provision 流程必须在拿到 201 时立即写入 `config.toml`，否则 token 永久丢失（Worker KV 里不存 token）。

---

## 执行交接

计划已完整写完，共 Task 0（Worker）+ Task 1-12（App）+ Task 13（更新提醒）+ Task 14（Homebrew tap + cask）。

**执行顺序建议：**
1. 先独立完成 Task 0（Worker 部署），拿到 Worker URL 和 SHARED_SECRET
2. 再按 Task 1 → 12 顺序实现 App，Task 5 的 provision.py 用真实 Worker URL 做端到端验证

两种执行方式：
1. **Subagent 驱动（推荐）** —— 每个 Task 派一个全新 subagent 实现，Task 之间来 review，上下文干净
2. **Inline 执行** —— 当前会话里按 Task 顺序批量执行，到检查点停下来 review
