# 技术设计：使用情况遥测（Telemetry）

日期：2026-06-25
状态：**设计定稿，待实现**
目标读者：本项目维护者

---

## 一、目标与非目标

### 目标

做**公司内部使用情况统计**，回答这几个问题：

- 有多少人在用本项目（按稳定的匿名标识去重计数）。
- 每个人用了多少：请求次数、token 用量、用了哪些模型。
- 使用趋势与节奏：按时间看每人 / 全体的用量变化、活跃时段。

### 非目标

- **不做 per-request 明细**。单次请求的延迟分布、单条请求大小等不是目标，不单独存。
- **不做精确计费**。token 是网关侧估算（见第四节），用于"谁用得多、趋势如何"足够，不等于精确账单。
- **不采集账户额度 / 花费快照**（曾考虑的"管线 B"已砍掉）。理由：`/usage` 端点反映的是账户级累计额度，可能被本项目以外的其他使用途径影响而变动，单独记录归因不清、意义不大。
- **不上报任何 prompt / 响应正文**。只上报大小、计数、token 数等聚合量。

---

## 二、关键约束（来自现有代码）

1. **网关是 vendored 上游 fork**。`app/scripts/vendor_sync.py` 在构建时按 `COPY_ITEMS = ["main.py", "kiro", "requirements.txt"]` 整目录覆盖拉取，pin 在 `UPSTREAM_SHA`。**绝不能改 vendor 内代码**，改了下次同步即丢。
2. **网关是 tray 拉起的子进程**。`app/kiro_gateway_tray/gateway.py:run_gateway_blocking()` 在子进程里 `import main` 拿到 `main.app`，再交给 uvicorn。**这是不动 vendor 的注入点**：包一层 ASGI 中间件即可。
3. **子进程继承父进程 env**（`GatewayProcess.start()` 里 `env = dict(os.environ)`）。遥测配置可经 `appconfig.to_gateway_env()` 注入子进程。
4. **已有 per-user 匿名标识**：`provision._get_username()` = clientId 的 SHA1 前 12 位，URL-safe、稳定、跨重启一致。

---

## 三、已定决策

| 项 | 决策 | 备注 |
|---|---|---|
| 存储 | **Cloudflare D1** | 你的体量稳在永久免费额度（$0），精确 SQL 聚合适合"按人统计" |
| 用户标识 | **匿名哈希**（沿用 `_get_username()`） | 能区分"不同的人"，但反查不到真人；需要时可离线补 `哈希→人名` 对照表 |
| 开关 | **强制开启、不可关** | 覆盖率 100% |
| 采集粒度 | **本地按时间桶聚合后上报**，非 per-request | 写入量与请求频率解耦 |
| 桶宽 | **10 分钟**（`bucket_seconds` 可配，5 分钟亦可） | 见第六节容量测算 |
| token 来源 | 直接采网关给出的 usage 三件套，不自行 tiktoken | 网关只回 token 数，不回精度标记（见第四节） |

---

## 四、Token / usage 数据事实（已核对上游源码）

核对对象：`../kiro-gateway`，HEAD `a185a41`，与 `UPSTREAM_SHA` 一致。

1. **流式 final chunk 无条件带 usage**。`kiro/streaming_openai.py:418-444`，`final_chunk` 里的 `usage` 三件套是无条件 yield 的，**不依赖客户端传 `stream_options.include_usage`**。所以每个**成功完成**的请求都能拿到 `prompt_tokens / completion_tokens / total_tokens`。
2. **token 是网关侧估算，不是上游真值**：
   - `completion_tokens`：永远 tiktoken（cl100k_base × Claude 校正 1.15，见 `kiro/tokenizer.py`）。
   - `prompt_tokens / total_tokens`：上游回了 `contextUsagePercentage` 时按"百分比 × 模型上限"反算（`calculate_tokens_from_context_usage`，较准）；没回时退回纯 tiktoken；都没有则为 0。
   - **注意**：网关把精度来源（`prompt_source`/`total_source` = `API Kiro`/`tiktoken`）只写进 `logger.debug`，**不放进响应 `usage`**。因此客户端无法区分这条 token 是反算的还是估算的 —— 故**不采集 `token_source` 维度**（曾设想的 `tok_src_*` 列已砍掉，见决策记录）。
3. **`credits_used` 上游随缘有，但本项目不采集**。`kiro/parsers.py:410-411` 仅当上游 SSE 出现 `{"usage":...}` 事件时才解析，`final_chunk` 里 `if metering_data:` 才带 `credits_used`。经本地日志实证：上游网关从不返回 metering，该字段恒为 NULL，没有意义，**已从全链路移除**（客户端采集/聚合/上报、Worker 落库/卷动/查询、D1 两张表均不再有 `credits_used` / `credits_used_sum`）。
4. 非流式（`collect_stream_response`）内部跑同一套流式解析，结论一致。

---

## 五、架构

```
管线A（每请求采集 + 本地聚合）
  Cursor ──tunnel──▶ uvicorn（网关子进程）
                       └─ TelemetryMiddleware（包在 main.app 外，不碰 vendor）
                            每请求：记 model/status/字节，提取 usage（SSE/JSON）
                            ▼ 不直接上报，先在内存累加进当前打开的桶
                       本地内存聚合（按 username, model, app_version, bucket）
                            ▲ 定时线程每 bucket_seconds 关闭过期桶并上报
                            ▼ 桶关闭即尝试上报；失败才落盘到 pending.jsonl
                  POST /telemetry ──▶ Cloudflare Worker ──▶ D1（usage_rollup）
   （Authorization: Bearer 鉴权）                              │ 卷动
                                                       D1（usage_daily 日聚合）
                                                              │
查询侧：Grafana(Infinity) ──▶ Cloudflare Access ──▶ 查询 Worker(只读,缓存60min) ──▶ D1
```

要点：

- **中间件严格旁路、零缓冲转发**：在 ASGI `send` 包装器里逐 chunk 原样转发，同时累计响应字节、在结束时回看最后一个非 `[DONE]` 的 `data:` chunk 提取 usage。绝不缓冲整个响应（会破坏流式体验）。
- **采集失败永不影响请求**：全程 try/except 吞掉，遥测异常只记日志。
- **本地先聚合**：每请求只在内存里对当前打开的桶做累加，不产生上报行，也不落盘。

### 中间件实现要点（实现时易错，必须遵守）

1. **只对对话端点采集**：仅 `POST /v1/chat/completions` 与 `POST /v1/messages` 进入采集逻辑。`/health`、`/usage`、`/v1/models`、`OPTIONS` 等一律直通，否则会产生大量 `model=null` 噪声行。
2. **request body 必须回放**：ASGI 的 `receive` 是一次性流，读 body 取 `model`/`request_bytes` 后，下游 app 将再也读不到。实现上把已读的 body 缓存，构造一个新的 `receive` 把同样的 `http.request` 事件回放给内层 app（注意 `more_body` 分帧）。这是 ASGI 中间件最经典的 bug 源。
3. **SSE 与 JSON 两种响应都要解析 usage**：
   - 流式（`stream=true`）：响应是 SSE，usage 在末尾 `data:` chunk（`[DONE]` 前一个）。
   - 非流式（`stream=false`，Cursor 的探测/校验请求常见）：响应是单个 JSON，usage 在 body 的 `.usage`。
   - 中间件按 `Content-Type`（`text/event-stream` vs `application/json`）分流，**两条都要能提取**，只处理 SSE 会漏掉非流式请求。
4. **`model` 兜底与归一**：
   - body 解析失败 / 无 `model` 字段 → 聚合键的 `model` 记为 `"unknown"`，绝不为 null。
   - 优先取**请求体**的 `model`（响应里的 model 可能被网关改写）。
   - 是否把 `kiro-*` 别名归一到真实模型名：默认**保留原始值**（用户视角的真实用法），如需合并在查询侧做映射，避免在采集端丢失信息。
5. **字节口径**：`request_bytes` = 读到的请求 body 字节数；`response_bytes` = 转发的下行 body 累计字节数（含 SSE 框架字节）。不含 HTTP 头，口径在文档固定，避免日后对不上。

---

## 六、本地聚合与容量

### 聚合键与累加值

一行聚合 = 一个 `(username, model, app_version, bucket_start)` 组合：

| 维度键 | 含义 |
|---|---|
| `username` | 匿名哈希 |
| `model` | 模型 ID，如 `claude-sonnet-4` |
| `app_version` | tray 版本，如 `0.1.24` |
| `bucket_start` | 时间桶起点（Unix 秒，对齐到 `bucket_seconds`，默认 600） |

| 累加值 | 含义 |
|---|---|
| `requests` | 请求总数 |
| `successes` | 成功完成数（拿到 final usage） |
| `errors` | 失败数（超时 / 上游错误 / 客户端断开） |
| `prompt_tokens_sum` | 输入 token 之和（仅累加非空） |
| `completion_tokens_sum` | 输出 token 之和 |
| `total_tokens_sum` | 总 token 之和 |
| `request_bytes_sum` | 请求体字节之和 |
| `response_bytes_sum` | 响应体字节之和 |
| `ttft_ms_sum` / `ttft_count` | 首包延迟累计（首个非空 body 字节）；平均 TTFT = sum / count |
| `generation_ms_sum` / `generation_count` | 流式生成窗累计（首 token → 响应结束，仅 SSE） |
| `generation_completion_tokens_sum` | 计入生成窗的 completion tokens；token/s = 该值 / (generation_ms_sum/1000) |
| `estimated_credits` | 估算 Credit 消耗（本机 `/usage` 模型段 diff；**NULL=未上报/未知，0=测得零消耗**；Kiro 计费有延迟） |
| `credit_estimate_segments` | 成功结算的 Credit 段数（NULL=未上报） |
| `credit_estimate_missing_segments` | 未能结算的段数（采样失败 / 负 diff / 月度重置；NULL=未上报） |

### Credit 分段估算（账户级 `/usage` → 模型桶）

上游 SSE 从不返回 per-request `credits_used`，因此改为：

1. 首次见到模型 A：后台调本机 `GET /usage`，取各 `breakdowns[].used` 总和作起点（不计费）。
2. 模型切到 B：再采一次；`当前值 − A 起点` 记入 A 的当前 10 分钟桶，同一读数作为 B 起点。
3. 每次上报前 checkpoint：再采一次结算当前模型，终点继续作为下一段起点。
4. 采样在独立后台线程串行执行，**不阻塞**真实请求；负 diff / `nextDateReset` 变化 / 采样失败 → `credit_estimate_missing_segments++`，不伪造 0 消耗。

金额（$0.04/Credit、人民币）只在本地 `report.py` / 看板层换算，不上报 D1。

### 容量测算（活跃 10h = 600 分钟，人均同时段约 2 个模型）

| 桶宽 | 每人/天行数 | 50 人/天 | 100 人/天 | D1 Free 写上限 |
|---|---|---|---|---|
| 10 分钟 | ~120 | 6 千 | 1.2 万 | **10 万行/天** |
| 5 分钟 | ~240 | 1.2 万 | 2.4 万 | 10 万行/天 |

**写入量只与"人数 × 桶数 × 模型数"挂钩，与请求频率无关**：一个人 10 分钟内请求 1 次或 1000 次，都只产生 1 行。自动化工具高频刷量也不会撑爆。采用 **10 分钟桶**，100 人仅 1.2 万行/天，留足余量。

存储：每行约 150–250 字节，百万行约 200 MB，远低于 5 GB 上限。

---

## 七、数据库 Schema（D1）

```sql
CREATE TABLE IF NOT EXISTS usage_rollup (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  bucket_start        INTEGER NOT NULL,   -- Unix 秒，对齐到 bucket_seconds
  bucket_seconds      INTEGER NOT NULL,   -- 桶宽，默认 600
  username            TEXT    NOT NULL,   -- 匿名哈希
  model               TEXT    NOT NULL,
  app_version         TEXT    NOT NULL,
  requests            INTEGER NOT NULL DEFAULT 0,
  successes           INTEGER NOT NULL DEFAULT 0,
  errors              INTEGER NOT NULL DEFAULT 0,
  prompt_tokens_sum   INTEGER NOT NULL DEFAULT 0,
  completion_tokens_sum INTEGER NOT NULL DEFAULT 0,
  total_tokens_sum    INTEGER NOT NULL DEFAULT 0,
  request_bytes_sum   INTEGER NOT NULL DEFAULT 0,
  response_bytes_sum  INTEGER NOT NULL DEFAULT 0,
  ttft_ms_sum         INTEGER NOT NULL DEFAULT 0,
  ttft_count          INTEGER NOT NULL DEFAULT 0,
  generation_ms_sum   INTEGER NOT NULL DEFAULT 0,
  generation_count    INTEGER NOT NULL DEFAULT 0,
  generation_completion_tokens_sum INTEGER NOT NULL DEFAULT 0,
  estimated_credits   REAL,              -- NULL=未知；0=测得零
  credit_estimate_segments INTEGER,
  credit_estimate_missing_segments INTEGER,
  received_at         INTEGER NOT NULL,   -- Worker 落库时间
  UNIQUE (bucket_start, bucket_seconds, username, model, app_version)
);

CREATE INDEX IF NOT EXISTS idx_rollup_bucket   ON usage_rollup (bucket_start);
CREATE INDEX IF NOT EXISTS idx_rollup_user     ON usage_rollup (username, bucket_start);

-- 日聚合表：Worker 定时把 usage_rollup 卷成"天 × user × model"，供看板默认查询，
-- 把单次扫描行数从"窗口内全部 10 分钟桶"压到"窗口天数 × 用户 × 模型"级（见第十二节）。
CREATE TABLE IF NOT EXISTS usage_daily (
  day                 TEXT    NOT NULL,   -- YYYY-MM-DD（UTC）
  username            TEXT    NOT NULL,
  model               TEXT    NOT NULL,
  requests            INTEGER NOT NULL DEFAULT 0,
  successes           INTEGER NOT NULL DEFAULT 0,
  errors              INTEGER NOT NULL DEFAULT 0,
  prompt_tokens_sum   INTEGER NOT NULL DEFAULT 0,
  completion_tokens_sum INTEGER NOT NULL DEFAULT 0,
  total_tokens_sum    INTEGER NOT NULL DEFAULT 0,
  request_bytes_sum   INTEGER NOT NULL DEFAULT 0,
  response_bytes_sum  INTEGER NOT NULL DEFAULT 0,
  ttft_ms_sum         INTEGER NOT NULL DEFAULT 0,
  ttft_count          INTEGER NOT NULL DEFAULT 0,
  generation_ms_sum   INTEGER NOT NULL DEFAULT 0,
  generation_count    INTEGER NOT NULL DEFAULT 0,
  generation_completion_tokens_sum INTEGER NOT NULL DEFAULT 0,
  estimated_credits   REAL,
  credit_estimate_segments INTEGER,
  credit_estimate_missing_segments INTEGER,
  PRIMARY KEY (day, username, model)
);

CREATE INDEX IF NOT EXISTS idx_daily_day ON usage_daily (day);
```

`UNIQUE` 约束让 Worker 端用 `INSERT ... ON CONFLICT DO UPDATE` 做**幂等覆盖**（last-write-wins，见第八节）：客户端重复上报同一个桶（重试 / 跨重启续传）写入的是同一份终值，覆盖多次结果不变，不会重复计数。

> 上表是 **D1（服务端）** 的结构。**客户端本地不建库**：运行时聚合在内存，仅把"上报失败的已关闭桶"以每行一条 JSON 的形式追加到 `pending.jsonl`（字段同 `rows` 元素，见第八节），无 `received_at`、无标记位。详见第十节。

---

## 八、Worker 接口

在现有 `worker/`（或独立 Worker）加一条路由：

```
POST /telemetry
  headers: { Authorization: "Bearer <TELEMETRY_SECRET>", Content-Type: "application/json" }
  body: { schema_version, rows: [ {bucket_start, bucket_seconds,
          username, model, app_version, requests, successes, errors,
          prompt_tokens_sum, completion_tokens_sum, total_tokens_sum,
          request_bytes_sum, response_bytes_sum,
          ttft_ms_sum, ttft_count,
          generation_ms_sum, generation_count, generation_completion_tokens_sum,
          estimated_credits, credit_estimate_segments, credit_estimate_missing_segments} , ... ] }
  → 200 { ok: true, accepted: N }
  → 401 { error: "unauthorized" }   // 密钥缺失或不匹配
```

### 鉴权（预共享密钥）

- **预共享密钥 `TELEMETRY_SECRET`**：客户端与 Worker 双方持有的同一个密钥。客户端每次上报在 `Authorization: Bearer` 头里带上；Worker 用它校验，**不匹配直接 401**，别人无法瞎打。
- **放 HTTP 头、不放 body**：避免密钥混进 JSON 正文，也便于和现有 `/provision` 的 `shared_secret`（body 内）区分。
- **独立于 `SHARED_SECRET`**：不复用激活码——后者会随用户批次轮换，复用会导致换激活码时遥测一起断；两者职责也不同（一个签发隧道、一个收遥测）。
- **恒定时间比较**：Worker 端用恒定时间比较（如先比长度再逐字节异或累加），避免计时侧信道。`A !== B` 这种短路比较仅作为最低要求的兜底。
- **轮换**：`wrangler secret put TELEMETRY_SECRET` 换新值并 `wrangler deploy`；客户端无需发版——下次上报 401 后经 `/telemetry-secret` 自动拉到新值（见下"密钥分发与轮换"）。
- **传输安全**：上报走 HTTPS（隧道/Worker 自带 TLS），密钥不会明文过网。

> 局限（可接受）：密钥随客户端分发，本质是"够用的准入门槛"而非强身份。它能挡住公网随机扫描和无凭证的瞎打；但持有客户端的人理论上能取出密钥伪造上报。对"公司内部使用统计"这个场景，这个强度足够；真要防内部伪造需做每用户签名，当前不做。

### 密钥分发与轮换（provision 首次下发 + 轻量刷新端点）

`TELEMETRY_SECRET` 不写死在客户端代码里。分发与轮换链路：

1. **首次下发**：客户端激活（`/provision`）成功时，Worker 在响应里附带当前 `telemetry_secret`，客户端写入本地 `config.toml` 的 `[telemetry].secret`。
2. **轮换刷新**：当上报 `/telemetry` 返回 401（说明本地密钥已过期），客户端调用轻量端点 `/telemetry-secret` 拉取最新密钥，写回 config 后用新密钥重试。
3. **节流**：刷新最多 **60 秒一次**（客户端记 `last_secret_refresh` 时间戳，401 触发前检查间隔）；刷新期间上报照常落 `pending.jsonl` 不丢。

```
POST /telemetry-secret
  body: { shared_secret, username }     ← 用激活码鉴权（同 /provision），不用 TELEMETRY_SECRET
  → 200 { telemetry_secret }            ← 只回当前密钥，绝不重建隧道
  → 401 { error: "unauthorized" }
```

> **为什么刷新端点用 `shared_secret` 而非 `TELEMETRY_SECRET`**：刷新的前提就是本地 `TELEMETRY_SECRET` 已失效（401）。若刷新也要求该密钥就会死锁。`shared_secret`（激活码）是客户端长期持有、与遥测密钥轮换解耦的凭据，且已持久化在 `cloudflare.shared_secret`。用它鉴权不降低安全性：`TELEMETRY_SECRET` 本就是分发到所有客户端的准入门槛、非强身份，用共用激活码保护它强度匹配。

> **为什么不复用 `/provision` 刷新**：现有 `/provision` 是"幂等重建"——每次调用都会删旧 tunnel、重建并换 `run_token`。拿它刷遥测密钥会推倒重建用户隧道（短暂断连 + DNS 重建），代价过重。`/telemetry-secret` 只返回密钥、不碰任何隧道资源。

### 端点鉴权矩阵

| 端点 | 鉴权 | 说明 |
|---|---|---|
| `/provision`、`/update-port` | `shared_secret`（body，现有） | 不变 |
| `/provision` 响应 | — | 额外附带 `telemetry_secret`（首次下发） |
| `/telemetry`（上报） | `TELEMETRY_SECRET`（`Authorization: Bearer` 头） | 失效→401→走刷新 |
| `/telemetry-secret`（刷新） | `shared_secret`（body） | 只回密钥，不重建隧道，60s 节流 |
| `/q/*`（查询） | Cloudflare Access Service Token（边缘） | 见第十二节 |

### 落库（原子化、幂等：last-write-wins）

客户端在**桶关闭后**才上报该桶的**完整终值**（关闭后不再有新数据进该桶）。因此服务端按"覆盖"语义落库，而不是累加：

```sql
INSERT INTO usage_rollup (bucket_start, bucket_seconds, username, model, app_version,
                          requests, successes, errors,
                          prompt_tokens_sum, completion_tokens_sum, total_tokens_sum,
                          request_bytes_sum, response_bytes_sum,
                          ttft_ms_sum, ttft_count,
                          generation_ms_sum, generation_count, generation_completion_tokens_sum,
                          estimated_credits, credit_estimate_segments, credit_estimate_missing_segments,
                          received_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(bucket_start, bucket_seconds, username, model, app_version)
DO UPDATE SET
  requests = excluded.requests,
  successes = excluded.successes,
  errors = excluded.errors,
  prompt_tokens_sum = excluded.prompt_tokens_sum,
  completion_tokens_sum = excluded.completion_tokens_sum,
  total_tokens_sum = excluded.total_tokens_sum,
  request_bytes_sum = excluded.request_bytes_sum,
  response_bytes_sum = excluded.response_bytes_sum,
  ttft_ms_sum = excluded.ttft_ms_sum,
  ttft_count = excluded.ttft_count,
  generation_ms_sum = excluded.generation_ms_sum,
  generation_count = excluded.generation_count,
  generation_completion_tokens_sum = excluded.generation_completion_tokens_sum,
  estimated_credits = excluded.estimated_credits,
  credit_estimate_segments = excluded.credit_estimate_segments,
  credit_estimate_missing_segments = excluded.credit_estimate_missing_segments,
  received_at = excluded.received_at;
```

- **原子**：单条 `INSERT ... ON CONFLICT DO UPDATE` 在 D1（SQLite）里是语句级原子操作，不会出现读-改-写撕裂；一个请求内的多行用 D1 batch 一次事务提交；D1 对同一数据库的写入本身串行化。
- **幂等（关键）**：用"覆盖"而非"累加"。客户端重试会发来同一个桶的同一份终值，覆盖写多次结果不变 → **不会重复计数**。这正是为什么前面坚持"桶关闭后才上报终值"。
- **为什么不用累加**：`SET x = x + excluded.x` 会让"上报成功但响应丢失→客户端重发"的场景把同一个桶加两次，造成翻倍。本设计里同一个 `(bucket, username, model, app_version)` 键只由单一来源产生单一终值，没有"多来源各报一部分"的需求，所以不需要累加。

> 若未来真出现"同键多来源各报增量"（如同一用户多机共用同一 `username`），覆盖会相互打架。届时需引入每次上报的去重令牌（`batch_id`）+ 累加，或给 `username` 加机器维度，使键回到单一来源。当前不做。

> 配置：`wrangler.toml` 增加 `[[d1_databases]]` binding；`TELEMETRY_SECRET` 经 `wrangler secret put` 注入（绝不进 git）。

---

## 九、客户端实现拆解（待实现）

1. **`app/kiro_gateway_tray/telemetry.py`（新增）**
   - `TelemetryMiddleware`：ASGI 中间件，旁路采集（端点过滤 + body 回放 + SSE/JSON 双路 usage 提取，见第五节）+ 在内存里把每请求累加进当前打开的桶。
   - 运行时聚合：内存 dict 累加当前打开的桶；与定时线程共享，切桶/摘取时用一把轻量锁保护（见第十节）。
   - 定时线程：每 `bucket_seconds`（默认 600s）唤醒，关闭并上报已过期的桶（不靠请求驱动）。
   - 失败落盘：仅当上报失败时，把已关闭的桶追加进 `pending.jsonl`（标准库 `json`，零额外依赖，无 SQLite/WAL）。
   - `Uploader`：按第十节的可靠性策略上报；失败指数退避；本地保留 30 天，超期在重写时丢弃。
   - lifespan：透传内层 app 的 startup/shutdown，并在 shutdown 阶段 flush 所有内存桶（优雅退出保证落盘）。
   - `from_env()`：读 `TELEMETRY_URL / TELEMETRY_SECRET / TELEMETRY_USERNAME / APP_VERSION / TELEMETRY_BUCKET_SECONDS`。
2. **`app/kiro_gateway_tray/gateway.py`**：子进程入口把 `main.app` 包一层中间件再交给 uvicorn（仅当 `TELEMETRY_URL` 存在）。
3. **`app/kiro_gateway_tray/appconfig.py`**：加 `[telemetry]` 段（`endpoint_url`、`secret`、`bucket_seconds`、`flush_interval`、`max_retention_days`=30）；`to_gateway_env()` 注入对应 env，并把 `_get_username()` 的结果作为 `TELEMETRY_USERNAME` 传入。
4. **`worker/`（复用现有 `kiro-provision` worker，方案 A，部署步骤见第十三节）**：
   - 上报路由 `POST /telemetry` + D1 binding + DDL；`TELEMETRY_SECRET`。
   - 查询路由 `/q/*`（只读、参数化固定查询）+ 前置 Cloudflare Access Service Token 鉴权（见第十二节）。
   - Cron Trigger：定时把 `usage_rollup` 卷成 `usage_daily` 日聚合表（降低看板读额度）。
5. **Grafana**：用 Infinity 插件接查询路由（JSON over HTTPS + `CF-Access-Client-Id/Secret` 头），出"独立用户数 / 每人 token 趋势 / 模型分布 / 活跃时段"看板。查询默认走 `usage_daily`，Worker 侧缓存 TTL 60 分钟（见第十二节）。

---

## 十、上报与落盘可靠性策略

遥测采集发生在网关子进程内，进程随 tray 的启停/重启而退出。运行时聚合放在内存，**只有"上报失败的已关闭桶"才需要落盘续传** —— 因此本地用一个追加式 `pending.jsonl` 即可，不引入 SQLite。

> 存储载体：`<app data dir>/telemetry/pending.jsonl`，每行一条 JSON（字段同第八节 `rows` 元素）。读写都用标准库 `json`，无第三方依赖、无 WAL、无 schema 迁移。

### 桶的关闭由独立定时线程驱动（不靠请求触发）

**关键**：桶的关闭与上报由一个**独立后台线程**驱动，每 **10 分钟**（与 `bucket_seconds` 对齐）唤醒一次，扫描内存中所有 `bucket_start` 已落在当前时间桶之前的桶，将其**关闭并上报**。

> 为什么不能靠请求驱动：若只在"下一个请求发现跨桶了"时关闭旧桶，一个用户当天最后一个桶要等他**下次上线**才会上报，延迟可达数小时甚至跨天。定时线程保证桶在关闭后最多一个检查周期内上报。

线程模型：中间件在 uvicorn 事件循环里更新内存 dict，定时线程读取/摘取已关闭桶 —— 两者访问同一 dict，需用一把轻量锁保护"摘取并清空已关闭桶"这一步（中间件侧只在切桶/累加时短暂持锁）。

### 进程退出时的落盘保证

| 退出方式 | 触发者 | 能否 flush | 处理 |
|---|---|---|---|
| 优雅退出（SIGTERM） | `Supervisor.stop()` 先发 `terminate()` | **能** | 经 ASGI **lifespan shutdown** 钩子收尾所有内存桶并尝试上报；失败落盘 |
| 强杀（SIGKILL） | `terminate()` 超时 10s 后 `kill()` | **不能** | 内存中未上报桶丢失（最多每用户一个当前桶）；已落盘的 `pending.jsonl` 不受影响，下次启动续传 |

实现：中间件需正确**透传内层 app 的 lifespan 事件**（vendor `main.py` 自带 `lifespan=lifespan`），并在 `shutdown` 阶段附加自己的 flush。优雅退出路径下当前桶可保证落盘/上报；SIGKILL 是设计上接受的丢失边界（见文末说明），无法避免。

### 上报与落盘规则

1. **桶关闭即上报**：定时线程关闭一个桶（或退出 flush 收尾）后立即尝试 `POST /telemetry`。成功的桶**根本不落盘**。
2. **上报失败一律落盘**：上报失败的桶**追加一行**到 `pending.jsonl`（append 是最简单、最不易损坏的写），等待后续重试，**绝不丢**。
3. **重试 + 顺序**：
   - **新数据优先上报**：刚关闭的桶先发，保证近期数据最快可见。
   - **落盘的历史数据后进先出（LIFO）补传**：积压的桶在新数据之后、按"最新落盘的先补"的顺序重试。
   - 失败按指数退避重试，不阻塞新数据上报。
   - 补传成功后，把 `pending.jsonl` **整体重写**（读出全部 → 去掉已成功的与超期的 → 写回）。文件仅数 KB–MB，整体重写成本可忽略，无需增量删除机制。
4. **幂等兜底**：所有上报都靠第七/八节的 `ON CONFLICT DO UPDATE` **覆盖**（last-write-wins）。即便"已上报但响应丢失"导致重复发送同一份终值，覆盖也不会重复计数 —— 所以 JSONL 这种"非原子删除（靠重写）"完全安全。
5. **保留与清理**：本地落盘数据**最多保留 30 天**，超期的行在下次重写时丢弃（这类长期发不出去的数据，通常意味着 Worker 配置失效，价值已很低）。

> 可接受的边界（重申）：仅在 **SIGKILL/硬崩溃** 时丢失内存里那个未关闭桶的增量（最多一个用户一个 10 分钟桶）。优雅退出（SIGTERM）经 lifespan flush 不丢。

### 30 天落盘占用测算（单台机器）

每台机器只存本机用户自己**上报失败**的桶（正常联网时通常为空）：

| 场景 | 行数/天 | 30 天行数 | `pending.jsonl` 占用 |
|---|---|---|---|
| 典型（10h 活跃 × 2 模型） | ~120 | ~3,600 | **~1 MB** |
| 重度（16h 活跃 × 4 模型） | ~384 | ~11,500 | **~3 MB** |

即使长期断网攒满 30 天，纯文本也只在 **个位数 MB**，可忽略。

---

## 十一、隐私与合规

- 强制开启，无开关。
- 上报内容白名单：时间桶、匿名哈希、模型、版本、计数、字节数之和、token 数之和。
- **绝不上报**：prompt 内容、响应正文、文件路径、IP、任何可直接定位个人的明文。
- `username` 为不可逆哈希；如需落到真人，离线维护对照表，不写进代码或上报链路。

---

## 十二、查询侧（Grafana 接入）与读额度

### 接入方式（已查实）

D1 **没有原生 Grafana datasource**。官方推荐、也是本设计采用的路径：

```
Grafana(Infinity) ──HTTPS──▶ Cloudflare Access ──▶ 查询 Worker ──▶ D1
                  CF-Access-Client-Id/Secret 头      只读 SELECT
```

- **查询 Worker**：暴露只读查询端点（如 `POST /q/...`），内部对 D1 跑**预定义的参数化聚合 `SELECT`**。**绝不把任意 SQL 透传给前端**——前端只能传白名单参数（时间范围、可选 username），SQL 模板在 Worker 里写死。既防注入，也防有人借 datasource 跑全表扫把读额度打爆。
- **Grafana 用 Infinity 插件**：JSON datasource，`POST` 到查询 Worker，默认查 `usage_daily`。

### 鉴权：Cloudflare Access Service Token（方案 A）

查询 Worker 前挂一层 **Cloudflare Access（Zero Trust）**，签发一个 **Service Token**（一对 `Client-Id` / `Client-Secret`）：

- Grafana Infinity 在 datasource 的自定义请求头里带：
  - `CF-Access-Client-Id: <client-id>`
  - `CF-Access-Client-Secret: <client-secret>`
- 校验发生在 **Cloudflare 边缘**：没带或不对的请求**根本到不了 Worker**，Worker 代码无需任何鉴权逻辑。
- **轮换 / 吊销**在 Zero Trust 控制台操作，不改代码、不重新 deploy。
- 选它的理由：本项目已在 Cloudflare 生态内（Worker + Tunnel），鉴权与业务代码彻底分离，是平台级强度，运维最省心。

> 与上报侧的区别：上报 `POST /telemetry` 用预共享 `TELEMETRY_SECRET`（要分发到几十台客户端，没法用 Access）。查询侧只服务你一处 Grafana，用 Access Service Token 更合适。**两套凭据完全独立（读写分离）**：上报密钥泄露不影响查询、反之亦然。

### 查询侧安全红线

1. **凭据存 Grafana 加密字段，不进 dashboard JSON**：Infinity 的 header/secret 是 datasource 级加密配置；不要写进 panel 查询或导出的看板 JSON，否则导出即泄露。
2. **Worker 只开放参数化固定查询，拒绝非 SELECT**：查询 Worker 的 D1 binding 在代码层只允许 SELECT，与上报 Worker 的写入路径职责分离。
3. **Infinity 配 Allowed Hosts**：限定到查询 Worker 域名，避免 datasource 被借去打别的地址。
4. **最小权限的 Service Token**：Access 策略只放行这一个查询应用，不复用到其他服务。

### 读额度测算（D1 Free = 500 万行读/天）

D1 每个查询返回 `meta.rows_read`（= **扫描**行数，非返回行数），按此计费。

**表规模**：100 人 × ~120 行/天 ≈ 1.2 万行/天；30 天累积 ≈ 36 万行，一年 ≈ 440 万行。

**单次看板查询成本**：一个"近 30 天按 user/天聚合"的查询，带 `bucket_start` 时间范围 + 索引，扫描 ≈ 窗口行数 ≈ 36 万行。

**真正的放大来源是 Grafana 自动刷新 × panel 数 × 看板人数**。裸查询会迅速放大到数亿行/天，**远超 500 万/天 Free 上限**。因此必须做两件事解耦：

1. **查询 Worker 加结果缓存**（KV / Cache API）：把"D1 读次数"与"刷新次数/看板人数"解耦——每个固定查询每个 TTL 周期只真打 D1 一次。
2. **看板默认查日聚合表 `usage_daily`**（Worker 定时把 `usage_rollup` 卷成 `天 × username × model`）：30 天窗口仅 100 人 × 30 × ~3 模型 ≈ **9 千行/次**，单次扫描压到千级。

**测算（缓存命中后真实打到 D1 的量）**，假设固定查询集约 10 个：

| 缓存 TTL | 每天真实命中 D1 次数 | × 9 千行/次 | 对 500 万/天 |
|---|---|---|---|
| 5 分钟 | 10 × 288 ≈ 2,880 | ~2,600 万 | 超 |
| 30 分钟 | 10 × 48 ≈ 480 | ~430 万 | **接近、可控** |
| 60 分钟 | 10 × 24 ≈ 240 | ~216 万 | **稳在 Free 内** |

**定调**：查 `usage_daily` + 查询 Worker 缓存 **TTL 60 分钟**，可稳在 Free 额度内（内部统计看板没必要分钟级实时）。若确需高频实时，升级 Workers Paid（$5/月含 250 亿行读/月）即彻底无忧。

> 写额度不受影响：上报写入 100 人 ≈ 1.2 万行/天，远低于 10 万/天；`usage_daily` 卷动每天再写几千行，可忽略。

> 待实现验证：用真实 `meta.rows_read` 校准上述估算（见第十四节），据此最终定 TTL 与索引。

---

## 十三、Cloudflare 部署操作（复用现有 `kiro-provision` worker）

遥测**复用现有 worker**（方案 A：签发与遥测聚合在同一个 worker），不新建 worker / 域名。相对现有部署（见 `cloudflare-setup.md`），增量如下。全部留在 **Free 额度**，无需付费。

### 增量服务总览

| # | 服务 | 用途 | 费用 |
|---|---|---|---|
| 1 | **D1 数据库** ×1 | 存 `usage_rollup` + `usage_daily` | Free |
| 2 | 现有 worker **加路由 + binding + cron** | `/telemetry` 写入、`/q/*` 只读查询、定时卷 `usage_daily` | Free |
| 3 | **Cloudflare Access（Zero Trust）** | 查询路由前置鉴权 + 签发 Service Token | Free（50 seats 内） |

### 操作步骤

**1) 创建 D1 数据库并建表**

```bash
cd worker
wrangler d1 create kiro-telemetry
# 输出里的 database_id 填进 wrangler.toml
wrangler d1 execute kiro-telemetry --remote --file=./schema.sql   # 第七节的 DDL
```

**2) `wrangler.toml` 增加 D1 binding 与 Cron Trigger**

```toml
[[d1_databases]]
binding = "TELEMETRY_DB"
database_name = "kiro-telemetry"
database_id = "<上一步输出的 id>"

[triggers]
crons = ["7 * * * *"]   # 每小时把 usage_rollup 卷成 usage_daily（错峰到第 7 分钟）
```

**3) 设置上报密钥**

```bash
wrangler secret put TELEMETRY_SECRET    # 客户端上报用，独立于 SHARED_SECRET，可用 openssl rand -hex 32 生成
```

**4) 部署带新路由的 worker**

`src/index.js` 内新增：`POST /telemetry`（校验 `TELEMETRY_SECRET`，写 `usage_rollup`）、`GET|POST /q/*`（参数化只读查询，读 `usage_daily`）、`scheduled()` 处理 cron 卷动。然后：

```bash
wrangler deploy
```

> 路由说明：`/telemetry` 自己校验 `TELEMETRY_SECRET`；`/q/*` **不**自校验，由下面的 Access 在边缘挡。两类路径在同一个 worker、同一个 `custom_domain` 下，靠 path 区分。

**5) 配置 Cloudflare Access 保护查询路径**

在 **Zero Trust 控制台**：

1. **Access → Applications → Add a self-hosted application**：
   - Application domain 填 `kiro-gateway-provision.example.com`，**Path 限定为 `/q`**（只保护查询路径，`/telemetry` 与 `/provision` 不受影响）。
2. **Policy**：建一条 `Service Auth` 策略，Action = `Service Auth`，规则匹配下一步要建的 Service Token。
3. **Access → Service Tokens → Create**：得到 `Client-Id` 与 `Client-Secret`（Secret 只显示一次）。
4. 把这对凭据填进 Grafana Infinity datasource 的自定义请求头（`CF-Access-Client-Id` / `CF-Access-Client-Secret`），详见第十二节。

> 验证：不带 token 访问 `/q/...` 应被 Access 拦在边缘（跳转/403，到不了 worker）；带正确 token 才放行。

### 运维补充

- **D1 额度监控**：D1 dashboard → Metrics 看 rows read/written；或 GraphQL Analytics API。接近 Free 上限时按第十二节调 TTL 或升级 Paid。
- **轮换**：`TELEMETRY_SECRET` 用 `wrangler secret put` 重设并 `deploy` + 客户端配置同步；查询 Service Token 在 Zero Trust 控制台轮换/吊销（不动代码）。
- **换域名**：查询同样走现有 provision 的 `custom_domain`，无新增域名操作。

---

## 十四、待办验证清单（实现阶段）

- [x] `credits_used` / `credits_used_sum`（上游 SSE metering）已从全链路移除（上游恒不返回，字段恒 NULL）。
- [x] 改为账户级 `GET /usage` 模型段 diff → `estimated_credits`（rollup/daily）；金额仅在报告层按 $0.04/Credit 换算。
- [ ] 确认 SSE usage chunk 跨 TCP 包分帧时尾部缓冲提取的可靠性。
- [ ] 非流式（`stream=false`）响应能从 JSON body 正确提取 usage。
- [ ] ASGI request body 回放正确：下游 app 仍能读到完整 body（含分帧 `more_body`）。
- [ ] 端点过滤：仅 `/v1/chat/completions`、`/v1/messages` 采集，其余直通。
- [ ] 客户端断开（`GeneratorExit`）路径记 `errors` 且不崩。
- [ ] 定时线程能关闭并上报"无后续请求"的空闲桶（不靠请求驱动）。
- [ ] 优雅退出（SIGTERM）经 lifespan shutdown 能 flush 当前桶；SIGKILL 路径不影响已落盘数据。
- [ ] 中间件正确透传内层 app 的 lifespan 事件（startup/shutdown 不被吞）。
- [ ] `pending.jsonl` 续传幂等性（重复上报同桶不重复计数）与重写后不丢未发数据。
- [ ] 鉴权（上报）：错误/缺失 `TELEMETRY_SECRET` 返回 401；密钥仅在 `Authorization` 头、不落日志、不进 body。
- [ ] 鉴权（查询）：未带/错误 Cloudflare Access Service Token 的请求被边缘拦截（到不了 Worker）；凭据存 Grafana 加密字段、不进看板 JSON。
- [ ] 查询侧：用真实 `meta.rows_read` 校准读额度测算，定查询 Worker 缓存 TTL 与是否上 `usage_daily`。
- [ ] D1 Free 额度监控（写入接近 10 万行/天、读接近 500 万行/天时告警）。
