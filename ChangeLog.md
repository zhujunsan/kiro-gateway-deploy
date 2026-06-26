# Changelog

## v0.2.8 (2026-06-26)

**Fixed**
- Windows/非 macOS 平台托盘实色图标此前沿用了 macOS 模板图标的大透明留白，导致任务栏显示尺寸偏小。现将实色图标内边距从 12.5% 调整为 5%，在缩放到任务栏尺寸后与其他应用图标视觉大小更一致。

## v0.2.7 (2026-06-26)

**Fixed**
- macOS Apple Silicon 上安装包打开时报「文件已损坏，无法打开」：DMG 内的 `.app` 此前完全未签名，配合 `com.apple.quarantine` 隔离标记会被 Gatekeeper 判定为「已损坏」。`make_dist.py` 新增 ad-hoc（identity `-`）代码签名 `_codesign_adhoc()`，在图标 catalog 安装之后、`create-dmg` 打包之前对 `.app` 签名（先单独签内层 cloudflared 子进程，再 `--deep` 整体签名并 `--verify --strict` 校验），把硬性「已损坏」拦截降级为可右键打开的「身份不明开发者」提示。
- macOS DMG 内混入多余的图标编译中间目录 `_icon_build`：`macos_icon.install_into_app()` 此前把 `Assets.car` 等中间产物编译到 `app_path.parent`，会泄漏进 create-dmg 的 staging 目录与最终 DMG。改为用隔离的 `tempfile.mkdtemp()` 临时目录编译并在 `finally` 清理，确保 staging 目录只含 `KiroGatewayTray.app`。

**Changed**
- 更新 macOS 安装说明（`README.md`、`app/README.md`）：说明现采用临时（ad-hoc）签名而非付费签名/公证，不再出现「已损坏」，但首次打开仍会提示「身份不明的开发者」，并给出右键 App →「打开」或 `xattr -dr com.apple.quarantine` 两种打开方式（Homebrew 安装已自动处理隔离标记）。

## v0.2.6 (2026-06-26)

**Fixed**
- 托盘"重启"网关偶发端口被占用、启动失败：修复重启时序并增加兜底。`gateway.py` 的 `stop()` 在 `terminate()` 超时 `kill()` 后补充二次 `wait()`，确保返回前旧进程确实退出；新增 `wait_port_free()` 以「能否 bind 目标端口」轮询探测端口释放（用 `SO_REUSEADDR`，避免被无害的 TIME_WAIT 误卡）。`supervisor.py` 的 `restart()` 在 `stop()` 与 `start()` 之间等待端口释放（上限 10 秒），超时则记 warning 后照常启动，避免被外部进程卡死。

## v0.2.5 (2026-06-26)

**Fixed**
- 网关工具调用改为符合 OpenAI 规范的增量流式下发：此前会把整个工具调用 `arguments`（如约 33KB 的 Write 计划）作为单个超大 SSE 块一次性发出，超大 `data:` 行容易被中间层（如 cloudflared）缓冲/截断，导致 Cursor 收到工具调用后卡死、不执行工具。现改为开场 delta 仅带 id/name 与空 arguments，随后按 1024 字符切片逐片下发 arguments，避免任何单行过大。

**Changed**
- 同步上游网关源码 SHA 至 `92f3ca9`（含增量流式工具调用修复），并将 `docker/docker-compose.yml` 镜像 tag 更新为 `ghcr.io/zhujunsan/kiro-gateway:main-92f3ca9`。

## v0.2.4 (2026-06-26)

**New**
- macOS 26（Tahoe）Liquid Glass 应用图标：新增 `AppIcon.icon` 图标合成器源（含 `Foreground.svg` 矢量字形与 `icon.json` 清单），打包时由 actool 编译为 `Assets.car` 资源目录并写入 `.app`，旧版 macOS 自动回退到 `icon.icns`。
- Windows 托盘图标随任务栏深/浅色主题自动适配：新增 `theme_watcher.py`，通过 `RegNotifyChangeKeyValue` 监听注册表 `SystemUsesLightTheme`（带 5 秒轮询兜底），主题切换时实时重绘图标。

**Changed**
- 托盘图标渲染区分平台：macOS 继续使用系统自动着色的模板镂空图标，Windows/其它平台改用自带对比度的实色图标，并按任务栏主题反转配色，避免"黑底黑图"看不清。
- 遥测移除 `credits_used` / `credits_used_sum` 字段：经实证上游网关从不返回 metering，该字段恒为 NULL 无意义，已从客户端采集/聚合/上报、Worker 落库/卷动/查询、D1 两张表及设计文档全链路移除。
- 遥测 rollup 表不再落库 `schema_version`（保留上报 body 顶层的协议握手位，便于未来按版本路由）。
- macOS 打包流程更新：`kiro_gateway_tray.spec` 在 BUNDLE 后调用 `macos_icon.install_into_app` 安装图标目录并写入 `CFBundleIconName`；`make_dist.py` 在打 DMG 前再执行一次作为兜底。

**Fixed**
- Windows 上启动内嵌网关进程时会弹出多余的空白控制台窗口：`gateway.py` 与 `proc_guard.py` 补充 `CREATE_NO_WINDOW` 标志。

## v0.2.3 (2026-06-26)

**Changed**
- Linux 首次配置改用系统原生对话框（zenity，缺失时回退 kdialog），不再依赖 tkinter
- 彻底移除 tkinter 依赖，开发环境与打包发行版的对话框行为现在保持一致
- 优化 Windows 输入对话框布局，OK/取消按钮紧贴输入框，去除底部多余留白

**Fixed**
- Linux 桌面环境下首次配置因 tkinter 被排除而无法弹出输入框的问题

## v0.2.2 (2026-06-26)

**Fixed**
- Windows 上 PyInstaller 打包后首次运行无反应的问题（tkinter 未打包导致输入框无法弹出）
- CLI 模式下 stdin 不可用时静默退出，现在会给出明确错误提示

**Changed**
- Windows 输入对话框从 VB InputBox 升级为 WinForms 版本，支持多行输入和密码掩码
- 启动失败时在 Windows 上弹出 MessageBox 显示错误原因，不再静默退出

## v0.2.1 (2026-06-25)

**Changed**
- 升级内嵌 cloudflared 二进制至 2026.6.1

## v0.2.0 (2026-06-25)

**New**
- 新增完整遥测系统（`telemetry.py`）：ASGI 中间件在网关层透明采集 AI 请求（模型、token 用量、延迟），使用 10 分钟时间桶聚合后批量上报至 Worker `/telemetry` 接口；上报失败时自动持久化到本地 `pending.jsonl`，下次批次自动重试，零数据丢失。
- `appconfig.py` 新增 `TelemetryCfg` dataclass，`TELEMETRY_URL` 由 `provision_url` 自动推导，无需手动配置；同步扩展 `AppConfig` 加载与持久化以携带遥测字段。
- `provision.py` 从 `/provision` 响应解析 `telemetry_secret`，新增 `refresh_telemetry_secret()` 方法，支持 provision 后单独刷新密钥。
- `supervisor.py` 在完成 provision 后自动将 `telemetry_secret` 写入本地 config，确保重启后密钥可用。
- `gateway.py` 在网关 ASGI 应用启动时用 `TelemetryMiddleware` 包装，遥测采集对网关逻辑完全透明。
- Worker（`worker/src/index.js`）新增 `/telemetry` 数据接收、`/telemetry-secret` 密钥下发、`/q/*` 查询路由，以及 scheduled cron 任务每小时将原始事件聚合写入 `usage_daily` 表。
- 新增 `worker/schema.sql`：D1 遥测数据库 DDL，包含 `telemetry_events`（原始事件）和 `usage_daily`（按日/模型聚合）两张表。
- `worker/wrangler.toml`：绑定 D1 数据库（`TELEMETRY_DB`）并配置 cron 触发器（`0 * * * *`）。
- `platform_compat.py` 补充遥测相关 mock，提升平台隔离性。
- 新增测试 `test_telemetry.py`，覆盖中间件采集、批量上报、本地 pending 重试全流程；更新 `test_appconfig.py`（新增 `TelemetryCfg` 测试）和 `test_supervisor.py`（provision 后 telemetry_secret 保存验证）。

**Changed**
- `tray.py` 联动调整，适配遥测配置初始化与启动流程。
- Worker `secrets.json.example` 补充 `TELEMETRY_HMAC_SECRET` 示例字段。

**Documentation**
- 新增 `docs/2026-06-25-telemetry-design.md`：遥测系统完整设计文档，含架构图、数据流、安全机制与部署说明。
- 更新 `docs/cloudflare-setup.md`：补充 D1 建库、`schema.sql` 初始化、`telemetry-secret` 配置等操作步骤。
- 更新 `worker/README.md`：新增遥测接口说明与 cron 聚合说明。

## v0.1.26 (2026-06-25)

**Fixed**
- 托盘「📊 额度」长时间不刷新：macOS 上 pystray 菜单是静态 `NSMenu`，打开菜单不会重新执行标签回调，额度仅在重绘时刷新；网关稳定运行、状态不再跳变后就再无刷新触发，数字会冻结数小时。新增后台定时刷新循环（网关运行时每 60 秒刷新一次额度并触发重绘），使额度近实时跟随账户用量，不再依赖手动打开菜单。

## v0.1.25 (2026-06-25)

**Changed**
- 网关子进程改为通过独立环境变量传递配置，不再写入托盘父进程的 `os.environ`，避免 `PROXY_API_KEY`/`PROFILE_ARN` 等密钥长期驻留并泄漏给后续派生的子进程。
- `usage` 模块抽出统一的鉴权请求辅助函数，`/usage` 与 `/v1/models` 复用同一套错误处理，非 200 时均带状态码与响应体片段便于排查。

**Fixed**
- 本地回环探测（网关 `/health`、`/usage`、`/v1/models` 及 cloudflared `/ready`）的 httpx 客户端禁用环境代理（`trust_env=False`）：httpx 不像 requests 那样自动跳过 localhost，配置了系统/公司代理且 `NO_PROXY` 未含 `127.0.0.1` 时会被代理劫持，导致网关明明在运行却显示连接中/获取失败。访问 GitHub 的更新检查仍走代理不变。
- 网关子进程早期 stdout/stderr 重定向到日志目录下 `gateway-bootstrap.log`：子进程在导入 vendored 网关前的崩溃（vendor 缺失、env 错误、依赖导入失败）此前会消失在无窗口 App 看不到的 stderr 中，现可落盘排查。

## v0.1.24 (2026-06-25)

**Changed**
- 首次初始化的三个引导对话框改用原生 Cocoa 输入框：profileArn 一步改为可换行的多行输入框，长 ARN 完整可见、便于核对，不再因单行折行而看不清是否正确。
- 三步输入新增格式校验，不通过会保留已填内容并提示原因要求重填：Provision 地址校验 `http(s)://` 前缀，激活码校验非空，profileArn 按 `arn:aws:codewhisperer:<region>:<12位账号>:profile/<id>` 严格匹配（可容忍前后空格/换行）。
- 更新检查节流由 10 分钟放宽到 4 小时，并在抓取失败（含 GitHub 匿名接口 60 次/小时限流）时也刷新检查时间，避免每次打开菜单都重试耗尽配额。

**Fixed**
- 升级后版本行不再误显示「高于发布版」：缓存记录写入时的应用版本，版本不匹配时强制重新检查；版本号与最新发布一致时统一显示「已是最新」。
- macOS Homebrew Cask 安装后自动执行去隔离（`xattr -dr com.apple.quarantine`），无需用户手动敲命令即可绕过 Gatekeeper 拦截。

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
