# Kiro Tray App

把 kiro-gateway 跑成 Mac / Windows / Linux 的本地托盘小工具，无需 Docker。
进程内跑网关，子进程跑 cloudflared 把本机网关经 Cloudflare 网络暴露为
`https://kg-<你的用户名>.botsonny.top/v1`，供 Cursor 直接使用。

> 与仓库根目录的 Docker 部署是两条独立的线，互不影响。

## 用户怎么用（拿到发布包后）

1. 从 GitHub Releases 下载对应平台的包并解压：
   - macOS: `KiroTray-<ver>-macos-<arch>.zip` → 解出 `KiroTray.app`，拖入 Applications
   - Windows: `KiroTray-<ver>-windows-amd64.zip` → 解出 `KiroTray/KiroTray.exe`
   - Linux: `kiro-tray-<ver>-linux-amd64.tar.gz` → 解出 `kiro-tray/kiro-tray`

2. 确保本机已用 Kiro IDE 登录过（存在 `~/.aws/sso/cache/kiro-auth-token.json`）。

3. 首次运行 → 菜单选「打开配置文件」（或 `kiro-tray --print-config`），
   在 `config.toml` 里填写：

   ```toml
   [gateway]
   profile_arn = "arn:aws:codewhisperer:us-east-1:<account>:profile/<id>"
   proxy_api_key = "<自己生成一个强随机串>"

   [cloudflare]
   provision_url = "https://kiro-gateway-provision.botsonny.top"
   ```

4. 重新启动 App → 弹出激活码输入框（托盘模式）或命令行提示（CLI 模式），
   输入管理员发给你的激活码，App 自动完成注册，`hostname` 和 `run_token` 自动写入 `config.toml`。

5. 注册完成后 App 自动启动网关和隧道。从托盘菜单「复制 Tunnel URL」和
   「复制 Gateway 密码」，填进 Cursor → Settings → Models → OpenAI API Key & Base URL：
   - **API Key**: 托盘「复制 Gateway 密码」（即 `config.toml` 里的 `proxy_api_key`）
   - **Base URL**: 托盘「复制 Tunnel URL」（即 `https://kg-<你的用户名>.botsonny.top/v1`）
   - 本机调试也可用「复制本地 URL」（`http://127.0.0.1:<port>/v1`）

6. 以后每次开机启动 App 即可，无需再输激活码。

## 开发者怎么构建

```bash
cd app
python3.11 -m venv .venv && . .venv/bin/activate
pip install -r requirements-build.txt
python scripts/vendor_sync.py           # 拉上游 pin sha + 打补丁
python scripts/fetch_cloudflared.py     # 下载当前平台 cloudflared
pyinstaller packaging/kiro_tray.spec --noconfirm
python packaging/make_dist.py           # 产物在 app/release/
```

三平台发布包由 GitHub Actions matrix 自动构建，打 `app-v*` tag 即触发。

## 配置项（config.toml）

| 段 | 键 | 说明 |
|---|---|---|
| gateway | profile_arn | CodeWhisperer profile ARN（必填） |
| gateway | proxy_api_key | 客户端 Bearer key（必填，建议强随机） |
| gateway | port | 本机监听端口，默认 18000 |
| gateway | api_region | 默认 us-east-1 |
| gateway | kiro_creds_file | 留空用默认 SSO 缓存路径 |
| gateway | fake_reasoning | 是否注入思考标签，默认 false |
| cloudflare | provision_url | Worker URL（首次填写，此后不用改） |
| cloudflare | hostname | 自动写入，勿手改 |
| cloudflare | run_token | 自动写入，勿手改 |
