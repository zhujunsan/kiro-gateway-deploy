# Kiro Gateway Tray App

把 kiro-gateway 跑成 Mac / Windows / Linux 的本地托盘小工具，无需 Docker。
进程内跑网关，子进程跑 cloudflared 把本机网关经 Cloudflare 网络暴露为
`https://kg-<你的用户名>.example.com/v1`，供 Cursor 直接使用。

> 与 `docker/` 目录下的 Docker Compose 部署是两条独立的线，互不影响。

## 如何使用

**前置条件**：本机已用 Kiro IDE 登录过（存在 `~/.aws/sso/cache/kiro-auth-token.json`）。

1. 从 GitHub Releases 下载对应平台的安装包：
   - macOS: `KiroGatewayTray-<ver>-macos-arm64.dmg` → 打开 DMG，拖入 Applications
   - Windows: `KiroGatewayTray-<ver>-windows-amd64-setup.exe` → 双击运行安装向导
   - Linux: `kiro-gateway-tray-<ver>-linux-x86_64.AppImage` → `chmod +x`，双击或直接运行

   > macOS 可用 Homebrew（本仓库即 tap）：
   > `brew tap zhujunsan/kiro-gateway-deploy https://github.com/zhujunsan/kiro-gateway-deploy && brew trust zhujunsan/kiro-gateway-deploy && brew install --cask kiro-gateway-tray`。
   > 新版 Homebrew 默认拒绝第三方 tap，报 `untrusted tap` 时先跑 `brew trust zhujunsan/kiro-gateway-deploy`。
   > 未签名期间首次打开需 `xattr -dr com.apple.quarantine "/Applications/KiroGatewayTray.app"`。

2. 首次运行 App → 自动弹出引导对话框（托盘模式弹窗 / CLI 模式命令行提示），
   只需填两项：
   - **Provision 服务地址**：管理员提供的隧道签发 URL（已填过则不再问）
   - **激活码**：管理员发给你的共享密钥

   其余配置全自动完成，无需手动编辑 `config.toml`：
   - `profile_arn`、`api_region` 自动从 Kiro token 文件读取
   - `proxy_api_key`（Gateway 密码）自动生成强随机串
   - 注册成功后 `hostname`、`run_token` 自动写入配置

3. 注册完成后 App 自动启动网关和隧道。从托盘菜单复制凭据，
   填进 Cursor → Settings → Models → OpenAI API Key & Base URL：
   - **API Key**: 托盘「复制 Gateway 密码」（自动生成的 `proxy_api_key`）
   - **Base URL**: 托盘「复制 Tunnel URL」（即 `https://kg-<你的用户名>.example.com/v1`）
   - 本机调试也可用「复制本地 URL」（`http://127.0.0.1:<port>/v1`）

4. 以后每次开机启动 App 即可，无需再输激活码。

> 需要手动改配置时，托盘菜单「打开配置文件」或 `kiro-gateway-tray --print-config` 可定位 `config.toml`。

## 开发者怎么构建

```bash
cd app
python3.11 -m venv .venv && . .venv/bin/activate
pip install -r requirements-build.txt
python scripts/vendor_sync.py           # 拉上游 fork（已内置 kiro-* 别名 + /usage）
python scripts/fetch_cloudflared.py     # 下载当前平台 cloudflared
python scripts/compress_cloudflared.py  # 可选：UPX 压缩 cloudflared（macOS 自动跳过）
pyinstaller packaging/kiro_gateway_tray.spec --noconfirm
python packaging/make_dist.py           # 产物在 app/release/
```

> `compress_cloudflared.py` 用 UPX 把 cloudflared 二进制就地压缩（Linux/Windows 约减 ~50%），
> cloudflared 以独立子进程运行，压缩不影响主程序。macOS 自动跳过（UPX 在 Apple Silicon 上不稳定且会破坏签名），
> 未装 upx 时也会友好跳过。CI 在发布构建时会自动执行这一步。

`make_dist.py` 会根据当前平台产出对应安装包，需预装打包工具：
- macOS → `.dmg`，需 `brew install create-dmg`
- Windows → `-setup.exe`，需安装 [Inno Setup 6](https://jrsoftware.org/isdl.php)
- Linux → `.AppImage`，需 `appimagetool`（放到 `app/build/appimagetool-x86_64.AppImage`）

三平台发布包由 GitHub Actions matrix 自动构建（CI 会自动装好上述工具），打 `v*` tag 即触发，
构建完自动创建 GitHub Release 并上传安装包。

## 配置项（config.toml）

通常无需手动编辑，引导流程会自动填好。下表供进阶调整参考。

| 段 | 键 | 说明 |
|---|---|---|
| gateway | profile_arn | CodeWhisperer profile ARN，自动从 Kiro token 读取 |
| gateway | proxy_api_key | 客户端 Bearer key，首次运行自动生成强随机串 |
| gateway | port | 本机监听端口，默认 64005 |
| gateway | api_region | 自动从 profile_arn 推断，默认 us-east-1 |
| gateway | kiro_creds_file | 留空用默认 SSO 缓存路径 |
| cloudflare | provision_url | 隧道签发服务 URL（首次引导填写，此后不用改） |
| cloudflare | hostname | 自动写入，勿手改 |
| cloudflare | run_token | 自动写入，勿手改 |
| cloudflare | protocol | 隧道协议，`http2`（默认，避开 UDP 封锁）或 `quic` |
| cloudflare | metrics_port | cloudflared 本地 metrics 端口，托盘探测 `/ready` 判断隧道是否连通，默认 20241；端口被占用时可改 |
| gateway_extra | FAKE_REASONING | 是否注入伪造的思考标签，默认 "false" |
| gateway_extra | AUTO_TRIM_PAYLOAD | 超限时按字节自动裁剪请求体；默认 "false"，让超限请求直接返回 400 context_length_exceeded，由客户端（如 Cursor）自行压缩上下文重试。注意 Kiro 上下文上限按 token 算（约 200k），字节裁剪无法可靠对齐 |
| gateway_extra | KIRO_MAX_PAYLOAD_BYTES | 仅在 AUTO_TRIM_PAYLOAD="true" 时生效的字节裁剪阈值；默认模板已不写此项（默认不裁剪），需要兜底裁剪时再手动加 |
| gateway_extra | TRUNCATION_RECOVERY | 响应被截断时自动通知模型续写，默认 "true" |
| gateway_extra | WEB_SEARCH_ENABLED | 是否自动注入 web_search 工具，默认 "false" |
| gateway_extra | FIRST_TOKEN_TIMEOUT | 等待首个 token 的超时秒数，默认 "30" |
| gateway_extra | FIRST_TOKEN_MAX_RETRIES | 首 token 超时后的重试次数，默认 "3" |
| gateway_extra | STREAMING_READ_TIMEOUT | 流式响应分块间的读超时秒数（须大于 FIRST_TOKEN_TIMEOUT），默认 "300" |
