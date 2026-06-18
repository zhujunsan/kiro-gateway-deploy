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

## v0.1.10 (2026-06-18)

**Changed**
- 同步上游 fork 到 `a368339`：Anthropic 模型别名改用 `kiro-o` / `kiro-s` / `kiro-h` 代号，规避 Cursor 对 `opus`/`sonnet`/`haiku` 的特殊路由；README 别名表与说明同步更新。
- 重新生成 `uv.lock`，更新 Python 依赖到最新可解析版本。
- CI：macOS runner 迁移到 `macos-15` / `macos-15-intel`。

## v0.1.9 (2026-06-18)

**Changed**
- CI：新增 macOS x64（Intel，`macos-13` runner）构建矩阵，补充 Intel 架构安装包产物。

## v0.1.8 (2026-06-18)

**Fixed**
- 修复 v0.1.7 遗留的两个测试失败：`provision` 错误消息补回 `clientIdHash`，并将失效的 profile_arn 优先级测试改写为按用户 clientId 的新逻辑。

## v0.1.7 (2026-06-18)

**Changed**
- `provision` 注册请求（访问 Worker）从主线程移到后台启动线程，填完表单后托盘图标立即出现，不再等待网络请求；图标创建即预设「启动中」状态。
- 网关未就绪时跳过 `/usage` 与 `/v1/models` 请求，菜单直接显示当前状态文字。
- CI：release actions 升级到兼容 Node 24 的版本。

**Fixed**
- 修复多用户共用同一 tunnel 的 bug：改用每个用户 SSO 缓存里各不相同的 `clientId`（而非全组织共用的 `clientIdHash`）生成 tunnel 名，避免一人重新注册导致他人 `run_token` 失效报 "Tunnel not found"。
- macOS 菜单重绘路由到主线程，防止崩溃。

## v0.1.6 (2026-06-18)

**Added**
- 托盘菜单新增「当前版本」显示行。

**Changed**
- 统一 gateway URL 拼接（新增 `gateway_origin()`，单一来源），加固 cloudflared 连接状态检测：匹配串提为命名常量并标注其依赖英文 stdout 的脆弱性。
- cloudflared 输出改为轮转日志（2MB×3）。

## v0.1.5 (2026-06-18)

**Changed**
- 依赖收敛进 `pyproject.toml`（hatchling），版本号单一来源（`pyproject` 动态读 `__version__`）。
- 新增 `log.py`，父进程写独立轮转日志 `tray.log`，散落的 `print` 改用 logger。
- 健康检查稳定后从 3s 退避到 15s 减少空闲唤醒；supervisor 与 usage 各复用一个 `httpx.Client` 连接池。

## v0.1.4 (2026-06-17)

**Changed**
- 网关由进程内线程改为独立子进程运行（re-exec 自身 + 隐藏的 `--run-gateway` 子命令）：重启即换全新解释器、总是重新读取配置，根治改端口/`profile_arn` 后重启不生效的问题；网关崩溃不再带崩托盘 UI。

## v0.1.3 (2026-06-17)

**Changed**
- 重构 `tray.py`，拆分为 icon / macos_menu / dialogs / platform_compat；抽出公共 `AsyncRefreshCache` 合并 usage/models 缓存并消除竞态。
- config 增加内存缓存 + 写时失效；落盘后 `chmod 0600` 保护明文密钥；cloudflared 由 `latest` 改为 pin 版本 + sha256 校验。

**Fixed**
- Windows 单例锁不再因顶层 `import fcntl` 崩溃（按平台分支）。
- 持久化 `shared_secret`，修复已注册用户改端口后 `update-port` 静默失效。
- 健康检查连续失败 5 次翻为 error 态，不再永远卡在 starting。
- `provision`/`update-port` 增加退避重试（5xx 重试，4xx 立即返回）。
- 修复网关重启时 loguru 文件 sink 重复添加导致的句柄泄漏，以及 `osascript` 反斜杠转义缺失的注入隐患。

## v0.1.2 (2026-06-17)

**Changed**
- CI：actions 升级到 v6 消除 Node 20 弃用警告；用 UPX 压缩 cloudflared 以减小发布包体积。

## v0.1.1 (2026-06-17)

**Changed**
- CI：matrix 显式声明 os+arch，PR 跳过打包仅 tag/dispatch 触发完整打包，权限收紧到 job 级，锁定 Python 3.11.9，元数据注入抽成 `scripts/inject_metadata.py` 并补单测。
- App：appconfig 增加读缓存、密钥文件 0600 权限；跨平台单实例锁（Windows 用 msvcrt）；`supervisor.register` 拆分便于主线程调用。
- Worker：移除 KV，改以 Cloudflare API 为唯一数据源。

## v0.1.0 (2026-06-17)

**Added**
- 首个发布版本：把 kiro-gateway 打包成 Mac / Windows / Linux 原生托盘 App（DMG / Inno Setup exe / AppImage），进程内跑网关、子进程跑 cloudflared 经 Cloudflare Tunnel 暴露公网。
- `kiro-provision` Cloudflare Worker：用激活码自动签发隧道与 DNS，返回 `hostname` / `run_token`。
- 引导对话框 + TOML 配置，依赖已内置 Cursor 改动（`kiro-*` 别名、`GET /usage` 等）的上游 fork。
- 三平台安装包由 GitHub Actions 打 `v*` tag 自动构建并发 Release。

**Fixed**
- 打包修复：DMG 只包含 `.app`，移除多余的 COLLECT 文件夹；Windows 构建注入元数据步骤指定 `shell: bash`。
