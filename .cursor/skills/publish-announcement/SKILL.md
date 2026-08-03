---
name: publish-announcement
description: >-
  向 Kiro Gateway 托盘公告栏发布/修改/下架线上公告（Cloudflare D1 `announcements` 表）。
  当用户说「发公告」「发个公告」「更新公告」「下架公告」「删公告」「改公告」等时务必使用。
  交互式收集字段后，用 wrangler 写入远程 D1，并 bump 缓存世代 + deploy Worker 使变更尽快生效。
---

# 发布托盘公告

在 `kiro-gateway-deploy` 仓库的 `worker/` 目录操作。公告展示在托盘菜单「新版提醒」下方，最多 5 条。

## 触发场景

- 发新公告 / 改已有公告 / 下架 / 删除 / 查看线上公告列表

## 前置

1. 工作目录：`worker/`（有 `wrangler.toml`）
2. 已 `wrangler login`，能操作远程库 `kiro-telemetry`
3. 需要立刻让客户端看到时：改完 D1 后 **必须** bump `ANNOUNCEMENT_CACHE_GEN` 并 `wrangler deploy`（边缘缓存 TTL 5 分钟；客户端本地还有 1 小时 TTL）

## 交互收集（发新 / 改公告）

用 AskQuestion（或对话）按需收集，**不要一次甩出全部字段**。最少只要 `body`。

### 必问

| 字段 | 说明 |
|------|------|
| `body` | 菜单行文字。单行；过长会被客户端截到约 120 字。长内容放 `url` |

改已有公告 / 下架时：先 `SELECT` 列出 `id`，让用户指定要操作的 `id`（不要再收集业务 `key`）。

### 建议追问（有默认就跳过）

用 AskQuestion 分组问，默认值写在选项里：

1. **样式**：`level`（info/warning/critical，默认 info）+ 是否 `dimmed`（默认否）
2. **点击**：是否有 `url`（http/https）；有则再问 URL。无 URL 时点击无反应，**不因此置灰**
3. **时间窗**：是否限制起止；要则问 `starts_at`/`ends_at`（Unix 秒或可转成秒的日期，UTC；起点含、终点不含；NULL=不限）
4. **定向**（可选）：`min_version`/`max_version`；`target_platforms`（macos/windows/linux，逗号分隔，空=全平台）
5. **排序**：`priority`（整数，越大越靠前，默认 0）
6. **行尾小字**：`tag`（可选）

确认摘要后再写库。用户说「就这些」则其余用默认。

### 不要问 / 已删除的字段

`key`（已改为自增 `id`）、`target_usernames`、`exclude_usernames`、`rollout_percent` —— 禁止再写。

## 写库命令

在 `worker/` 下执行。字符串内单引号加倍（`''`）。

### 新建（纯 INSERT，不要 UPSERT；不要写 id）

```bash
wrangler d1 execute kiro-telemetry --remote --command "<SQL>"
```

示例：

```sql
INSERT INTO announcements (
  body, tag, url, level, priority, dimmed, enabled,
  starts_at, ends_at, min_version, max_version, target_platforms,
  updated_at
) VALUES (
  '公告正文',
  NULL,                          -- tag
  'https://example.com',         -- url，可 NULL
  'info',                        -- info | warning | critical
  0,                             -- priority
  0,                             -- dimmed: 0 正常 / 1 置灰
  1,                             -- enabled
  NULL,                          -- starts_at
  NULL,                          -- ends_at
  NULL,                          -- min_version
  NULL,                          -- max_version
  NULL,                          -- target_platforms
  CAST(strftime('%s','now') AS INTEGER)
);
```

插入后立刻回看拿到 `id`：

```sql
SELECT id, body, enabled, priority
  FROM announcements
 ORDER BY id DESC
 LIMIT 5;
```

### 下架（保留行）

```sql
UPDATE announcements
   SET enabled=0, updated_at=CAST(strftime('%s','now') AS INTEGER)
 WHERE id=N;
```

### 删除

```sql
DELETE FROM announcements WHERE id=N;
```

### 改文案

```sql
UPDATE announcements
   SET body='新的文案', updated_at=CAST(strftime('%s','now') AS INTEGER)
 WHERE id=N;
```

### 列表

```sql
SELECT id, enabled, dimmed, level, priority, tag, body,
       datetime(starts_at,'unixepoch') AS starts,
       datetime(ends_at,'unixepoch') AS ends,
       min_version, max_version, target_platforms, url
  FROM announcements
 ORDER BY priority DESC, id;
```

日期转 Unix 秒可用：`CAST(strftime('%s','2026-08-10 00:00:00') AS INTEGER)`（按 UTC 理解）。

## 立刻生效（写库后必做）

1. 打开 `worker/src/announcements.js`，把 `ANNOUNCEMENT_CACHE_GEN` **加 1**
2. `wrangler deploy`
3. 用 SELECT 或 POST `/announcements` 冒烟确认
4. 告诉用户：客户端最多还要等约 1 小时本地 TTL（或重启 App / 删掉 `~/Library/Application Support/KiroGatewayTray/announcements.json` 后重开）

## 表结构备忘

当前列：`id`(INTEGER PK AUTOINCREMENT), `body`, `tag`, `url`, `level`, `priority`, `dimmed`, `enabled`, `starts_at`, `ends_at`, `min_version`, `max_version`, `target_platforms`, `created_at`, `updated_at`。

更全的模板句见 `worker/announcements.example.sql`；匹配语义见 `worker/src/announcements.js` 文件头注释。

## 完成后回复

简短说明：写了哪条 `id`、主要文案、是否已 deploy、客户端大概何时能看到。
