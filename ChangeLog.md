# Changelog

## v0.1.23 (2026-06-24)

**Fixed**
- 同步上游网关至 `a185a41`，补充 `claude-opus-4.8` 到 FALLBACK_MODELS，使别名 `kiro-o-4.8` 正常出现在模型列表中。
- 空内容占位符从零宽空格改为普通空格，修复部分客户端对零宽字符的兼容问题。

## v0.1.22 (2026-06-24)

**Fixed**
- 同步上游网关至 `5dcb04a`，根治 Cursor 客户端 "(empty placeholder)" 污染循环问题（上游从源头阻断空内容块在多轮对话中反复传播）。

## v0.1.21 (2026-06-24)

**Fixed**
- 同步上游网关至 `f6198c3`，修复 Cursor 客户端在某些场景下显示 `(empty placeholder)` 的问题（上游清洗了流式响应中异常的空内容块）。

## v0.1.20 (2026-06-24)

**Fixed**
- macOS 开机自启登录项恢复显示 `KiroGatewayTray`：v0.1.18 改用 `open -a <bundle>` 启动导致登录项名称变成 `open`（launchd 从 `ProgramArguments[0]` 推断条目名）；现回退到直接运行 App 内可执行文件，登录项正确显示应用名。`AssociatedBundleIdentifiers` 键仅对已签名 App 有效，一并移除。

## v0.1.19 (2026-06-24)

**New**
- 托盘「当前版本」菜单行新增更新状态后缀：根据本地缓存与 GitHub 最新发布对比，显示「检查中…」「可升级 X.Y.Z」「已是最新」或「高于发布版 X.Y.Z」（本地构建版本高于线上时）。新增 `updates.peek_cached()`，缓存命中时首次打开菜单即可立即显示，无需等待后台网络请求。

**Changed**
- 同步上游网关镜像 tag 至 `main-27a36ee`（`docker/docker-compose.yml`），并将 `UPSTREAM_SHA` 同步至对应 commit。该版本清洗 Cursor 发出的含换行符复合工具 ID（`call_<uuid>\nfc_<hash>`），避免 Kiro 返回 `REQUEST_BODY_INVALID` / Invalid tool use format 导致客户端卡住。
- 更新检查日志细化：记录是否真正发起 GitHub 查询、当前/最新版本与是否有更新，并仅在最新版本号变化时才触发菜单重绘，减少无谓刷新。

## v0.1.18 (2026-06-24)

**Changed**
- macOS 开机自启改善「登录项」显示：LaunchAgent 改为通过 `open -a` 启动 `.app` 包（而非包内裸可执行文件），并在 plist 中加入 `AssociatedBundleIdentifiers`，使「系统设置 → 通用 → 登录项」尽量显示应用名称与图标，而非通用 exec 图标。注意：应用当前未签名，图标为尽力而为，「身份不明的开发者」提示需 Apple 签名/公证后才会消失；升级后需重新勾选一次开机自启以重写 plist。

## v0.1.17 (2026-06-24)

**New**
- 新增开机自启支持：托盘菜单加入「🚀 开机自启」开关，跨平台实现（macOS LaunchAgent、Windows 注册表 Run 项、Linux XDG autostart），免管理员权限（附单元测试）。
- 托盘「复制模型名」菜单将 `kiro*` 别名单独分组到「别名（Cursor 内使用）」分隔区，与原始模型名区分展示。
- 托盘默认开启失败请求抓包与详细日志：`DEBUG_MODE=errors`（仅在请求失败时把请求体/响应落盘到日志目录下 `debug_logs/`，正常请求零额外开销）+ `LOG_LEVEL=DEBUG`，便于排查 Cursor 报错（如 "Invalid tool use format"）。已存在的 `config.toml` 在加载时自动回填这两项默认值。

**Changed**
- 网关日志保留份数由 3 提升至 5（`rotation=2 MB`，约 10MB 上限），配合详细日志保留更多排查历史。
- Cloudflare provision 向导步骤提示由 `(1/2)(2/2)` 更正为 `(1/3)(2/3)(3/3)`，与实际三步（Worker 地址 → 激活码 → Profile ARN）一致。
- macOS 应用 bundle identifier 调整为 `top.botsonny.kiro-gateway-tray`。

## v0.1.16 (2026-06-23)

**New**
- 守护进程新增 cloudflared 隧道断连自动重启：隧道存活但 `/ready` 探测持续不通超过 60s 时，自动 stop/start 重建隧道，避免长时间卡在 connecting/disconnected 状态需手动干预（附单元测试）。

**Changed**
- 托盘默认配置不再按字节自动裁剪超大请求：`AUTO_TRIM_PAYLOAD` 默认改为 `"false"` 并移除 `KIRO_MAX_PAYLOAD_BYTES`。Kiro 上下文上限按 token 计（约 200k），字节阈值无法可靠对齐；超限时网关直接返回 `400 context_length_exceeded`，交由客户端（如 Cursor）自行压缩上下文重试。已存在的 `config.toml` 不受影响。
- 同步上游网关镜像 tag 至 `main-67a1a94`（`docker/docker-compose.yml`），并将 `UPSTREAM_SHA` 同步至对应 commit。该版本将 Kiro 的 `CONTENT_LENGTH_EXCEEDS_THRESHOLD` 规范化为标准 `context_length_exceeded`（OpenAI 400 invalid_request_error）及 Anthropic invalid_request_error 形状，便于客户端识别上下文溢出。
- README（`app/`）同步上述裁剪相关配置说明。

## v0.1.15 (2026-06-18)

**Changed**
- Homebrew cask 的未签名提示由 `caveats` 改为 `postflight`：仅在 App 成功安装后才打印去隔离命令，安装失败时不再显示。
- README（根目录与 `app/`）补充新版 Homebrew 默认拒绝第三方 tap 的处理方式，安装命令追加 `brew trust zhujunsan/kiro-gateway-deploy`。

**Fixed**
- 发布工作流安装 `create-dmg` 前先 `brew trust` 预装的无关 tap，消除 Homebrew 5.2/6.0 的 "not trusted" 告警。

## v0.1.14 (2026-06-18)

**New**
- 本仓库现在同时是一个 Homebrew tap：根目录新增 `Casks/kiro-gateway-tray.rb`（区分 arm64 / Intel 两套 url + sha256），macOS 用户可经 `brew tap zhujunsan/kiro-gateway-deploy https://github.com/zhujunsan/kiro-gateway-deploy && brew install --cask kiro-gateway-tray` 安装。
- 新增 `app/scripts/bump_cask.py`：按 `on_arm` / `on_intel` 块分别改写 cask 的 `version` 与 `sha256`，并校验哈希格式、缺锚点即报错，附带单元测试。
- 发布流程新增 `bump-cask` CI job：发版后自动从 Release 拉取两个 DMG 的 `.sha256`，改写 cask 并提交推回默认分支，cask 哈希无需手工维护。

**Changed**
- README（根目录与 `app/`）补充 Homebrew 安装、未签名首次打开去隔离、升级命令说明。
- 同步上游网关镜像 tag 至 `main-e974e17`（`docker/docker-compose.yml`）。

## v0.1.13 (2026-06-18)

**Changed**
- 健康探测重构为单一共享状态机：后台轮询与「打开菜单时的即时探测」不再各自维护一份计数器互相覆盖 `_gw_health`，改由 `probe_now()` 与后台循环共用同一组状态，并以 state/probe/cfg 三把锁保护，避免并发读写状态撕裂或重复读盘。
- 网关已停止时健康探测自动退避到宽松间隔（15s），不再固定每 3s 空转唤醒，降低空闲时 CPU 占用。
- 托盘菜单逻辑重构为 `TrayApp` 类，并新增 `_ThrottleGate` 节流闸：打开菜单触发的健康探测与更新检查被收敛为「至多一个在途线程 + 最小间隔」，避免一次重绘风暴衍生出多个重复后台线程。
- 更新检查频率由「启动一次 + 每 24h」改为「启动与打开菜单时触发，最多每 10 分钟一次」，菜单上的新版本提示更及时。

**Fixed**
- 修复 `async_cache` 的 cooldown 防护：此前节流依赖缓存值非空，当 fetch 合法返回空值时节流失效，会让「刷新→重绘→再刷新」反馈环在每次菜单重绘时都真发请求；同时修正以 `0.0` 作为「从未刷新」哨兵导致开机后首次刷新可能被单调时钟误判为「冷却中」而被吞。
- 退出与停止时正确释放资源：托盘退出调用 `Supervisor.close()` 关闭健康探测用的 httpx 连接池，CLI 停止时同样释放；`usage` 模块的连接池注册 `atexit` 关闭，避免连接池存活到解释器关闭阶段。
- `provision` 首启注册时 `kiro-auth-token.json` 只读取一次并复用，不再为每次用户标识查找重复读盘。
- 网关子进程改用 `importlib.import_module("main")` 代替 `__import__`，行为更明确。

## v0.1.12 (2026-06-18)

**New**
- 新增 `proc_guard` 模块，对 cloudflared 子进程做孤儿进程防护：进程 PID 落盘，下次启动前清扫上次残留且确为 cloudflared 的进程（含 PID 复用校验）；Linux 用 `PR_SET_PDEATHSIG`、Windows 用 Job Object（kill-on-close）实现父进程退出即连带终止子进程。
- 新增 `notify` 模块统一桌面通知：macOS 改用进程内 `NSUserNotificationCenter` 投递，使通知横幅显示本应用的图标与名称，而非「脚本编辑器」；非 macOS 或不可用时回退到 pystray 原生通知。

**Changed**
- 全应用品牌名统一为「Kiro Gateway Tray」：托盘菜单、CLI 首启提示、单实例提示、Windows 安装包（Inno Setup）与 Linux 桌面项均同步更名。

**Fixed**
- cloudflared metrics 端口被占用时不再导致隧道启动失败：配置端口可用则沿用，被占用时自动回退到空闲端口；托盘 `/ready` 探测改用 cloudflared 实际绑定的端口，避免回退后探测打到错误端口而误报未连通。此前 App 重启遇到残留进程占用 20241 端口会让隧道静默退出，外部无法访问。

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
