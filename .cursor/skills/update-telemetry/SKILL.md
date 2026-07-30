---
name: update-telemetry
description: >-
  Kiro Gateway 本地遥测观测更新工作流。当用户说「更新 telemetry」「更新遥测」「更新下数据」「帮我更新下数据」、
  或提到 local-telemetry / usage-observations / UPDATE-WORKFLOW 时务必使用。
  流程：跑 refresh.py → 用 Cursor Sentry MCP（不是本地 token）拉 Sentry → 更新 usage-observations.md。
---

# 更新本地遥测与观测文档

工作目录：`/Users/san/project/kiro-gateway-deploy/local-telemetry`（已 gitignore）。

权威细节见同目录 `UPDATE-WORKFLOW.md`；本 skill 只钉死 Agent 必须遵守的顺序与 **Sentry 必须走 MCP**。

## 触发场景

- 「更新 telemetry / 更新遥测 / 再更新下数据」
- `@local-telemetry/UPDATE-WORKFLOW.md` 并要求更新数据
- 维护 `usage-observations.md` 观测表

## 标准步骤（按序）

### 1. 跑 `refresh.py`

```bash
cd /Users/san/project/kiro-gateway-deploy/local-telemetry
python3 refresh.py
```

会并行：D1→MySQL、Cloudflare Observability、（若有 token）`sentry_errors.py`，再生成 `report-latest.*`。

**不要**因为日志出现 `[warn] Sentry skipped: missing worker/.sentry-auth-token` 就停——这是预期；下一步用 MCP 补 Sentry。

### 2. Sentry：必须用 Cursor MCP（铁律）

Agent 会话里 **默认用 Sentry MCP**，不要依赖、也不要要求用户先放 `worker/.sentry-auth-token`。

1. `GetMcpTools` 确认 `user-sentry`（或当前环境里的 Sentry MCP）为 `ready`；若 `needsAuth` 则先 `mcp_auth`。
2. 用 MCP 拉项目 `san` / `kiro-gateway-tray`（region `https://us.sentry.io`）：
   - `search_issues`：`query=is:unresolved`，`period` 按水位选 `7d`/`30d`，`limit` 足够大
   - 对 P1 / 新增 / 高 events 的 Issue 再 `get_sentry_resource`（`resourceType=issue`）
3. 根据结果**手写/覆盖**本地产物（与脚本格式兼容）：
   - `sentry-errors-report.md`
   - `sentry-errors-todo.md`（只改 `BEGIN/END MANAGED SENTRY ERROR TODOS` 之间）
   - `sentry-errors-state.json`：更新 `last_successful_update_at`（UTC）、`active_issue_count`、`"source": "sentry-mcp"`
4. 分类口径与 CF 侧对齐：预期噪声（精确模型 ID/订阅错误）、需复核 4xx、可行动 5xx/网络/代码 bug；`client_disconnect` 标低优先级。
5. 把 Sentry 要点写进 `usage-observations.md` 的「观察」与「核心结论」，**禁止**再写「本轮缺 token 跳过」除非 MCP 也失败。

Token 文件路径仅作 **CI/无头脚本** 可选增强：有则 `refresh.py` 会跑 `sentry_errors.py`；Agent 仍应以 MCP 结果为准做最终报告（MCP 可补细节与根因）。

### 3. 加速：先跑 `update_observations.py`，再手补

```bash
python3 update_observations.py
# 可选：--dry-run / --md-only / --canvas-only
```

脚本从 `report-latest.json` + CF/Sentry 报告自动：

- 补丁 `usage-observations.md` 累计/自然日/版本表，并写入 `BEGIN/END AUTO OBSERVATIONS` 草稿
- 同步 canvas `usage-observations.canvas.tsx` 的 `BEGIN/END AUTO-GENERATED DATA` 常量（成本三图保持「不含过路费 Gateway vs Cursor Teams」；月估算默认近 **7** 个完整 UTC 自然日，并写 `MONTHLY_14` 对照）

然后 Agent **手补**定性部分（必做）：

| 位置 | 说明 |
|---|---|
| 二·补 观察 bullets | 用 AUTO 草稿改写；含 CF + **Sentry MCP** 要点 |
| 二·补·二 解读 / 二·补·三 | 核对 AUTO 数字已进正文；补一句解读 |
| 三、核心结论 | 滚动改写（勿留过期数字） |
| canvas Callout | 确认 `AUTO_META.callout` 合理 |

版本列必须用「最近活跃桶」的 `app_version`，禁止 `MAX(app_version)`。活跃人数口径：`requests>0`。

### 4. 回复用户

中文、简洁：观测时刻、累计、当日/进行中、CF 事故数、Sentry 要点（含 Issue 短 ID）、版本稳定性一句；并提一句「已跑 update_observations.py」。

## 禁止事项

- 不要因为缺 `.sentry-auth-token` 就跳过 Sentry 整段
- 不要把 MCP 失败假报成「零 Issue」；写明失败原因
- 不要提交 `local-telemetry/`（gitignore；含密码与状态）
- 不要打印任何 token / auth 头原值

## 故障速查

| 现象 | 处理 |
|---|---|
| `Sentry skipped: missing ...token` | **正常** → 走 MCP |
| MCP `needsAuth` / 未连接 | `mcp_auth` 或让用户连 Sentry MCP 后再拉 |
| MCP `fetch failed` | 缩小 query / 重试；仍失败则在报告写「MCP 查询失败」，水位不推进 |
| D1 / MySQL / wrangler 失败 | 按 `UPDATE-WORKFLOW.md` §8 |
