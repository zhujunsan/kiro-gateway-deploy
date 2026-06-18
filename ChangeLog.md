# Changelog

## v0.1.11 (2026-06-18)

**Changed**
- 隧道连接状态改用 cloudflared 的 metrics `/ready` 端点探测，不再解析日志文本；连上后托盘菜单立即刷新，不用重新打开菜单。
- 新增可配置的 `[cloudflare] metrics_port`（默认 20241），用于 `/ready` 探测，端口被占用时可改。
- 托盘菜单：退出图标改为 `⏏️`，版本号下方新增分隔符，「当前版本」一行可点击跳转到对应 GitHub Release。
- 文档与目录重构：Docker Compose 部署相关文件（`docker-compose.yml`、`.env.example`）移入独立的 `docker/` 目录并配套新的 `docker/README.md`；根 README 改以原生托盘 App 为主线，Docker 降级为可选部署方式。
- 发版自动化：GitHub Release 说明改为从 `ChangeLog.md` 对应版本段落自动提取（新增 `app/scripts/extract_changelog.py`，CI release job 据此填充）。

**Fixed**
- 端口同步：Worker `/update-port` 现返回实际生效端口与 `changed`，客户端据此回写 `registered_port`，避免误填非法端口导致漏同步。
