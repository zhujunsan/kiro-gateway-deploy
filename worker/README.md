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

## 更新 SHARED_SECRET（换批用户时）

```bash
wrangler secret put SHARED_SECRET   # 可用 openssl rand -hex 16 生成一个新激活码
wrangler deploy
```

## 注意事项

- run_token 只在 201 响应里返回一次，Worker 本身不存储任何状态（Cloudflare API 是唯一数据源）
- 吊销某用户：在 Zero Trust 控制台删 tunnel，DNS 记录也要手动删
- CF_API_TOKEN 永远不要提交到 git，只通过 `wrangler secret put` 存入
