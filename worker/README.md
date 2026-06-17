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
