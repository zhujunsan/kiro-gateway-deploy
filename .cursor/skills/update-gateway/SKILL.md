---
name: update-gateway-vendor
description: 修改 Kiro 网关本体逻辑（转换器/路由/流式/payload 处理等，即 app/kiro_gateway_tray/vendor/kiro 或 vendor/main.py 里的代码）时务必使用。核心铁律：禁止直接改 vendor/ 目录（它由 vendor_sync.py 从上游 fork 生成、且被 gitignore，手改会被覆盖）。正确流程：改上游 fork ../kiro-gateway 的源码并加/改测试 → 跑 pytest → 中文 commit 推 origin/main 发布（CI 产出 ghcr 镜像 main-<sha>）→ 回本仓同步更新 UPSTREAM_SHA 与 docker-compose 镜像 tag → 跑 vendor_sync.py 重新 vendor。当用户说"改网关"、"改转换逻辑"、"改 kiro 的 xxx"、"修一下工具调用/流式/payload"、"更新 vendor"、"升级网关依赖"等时触发。
---

# 更新网关本体（vendor）工作流

## 铁律：永远不要直接改 vendor/

`app/kiro_gateway_tray/vendor/`（含 `kiro/`、`main.py`、`requirements.txt`）是
**生成产物**：

- 由 `app/scripts/vendor_sync.py` 从上游 fork 在固定 SHA 处 clone 拷贝而来。
- 被 `.gitignore` 忽略（`git status` 看不到，本仓不跟踪它）。
- 任何手改都会在下次 `vendor_sync.py` 运行时被**整目录删除重建**而丢失。

所以网关本体的逻辑改动**必须改上游源码**，再走下面的同步流程落回本仓。

> 例外：只有当你确认改动**仅用于一次性本地抓包/调试**、且明确不打算保留时，
> 才可临时改 vendor；否则一律走上游流程。

---

## 涉及的两个仓库与两处 pin

- 上游 fork：`/Users/san/project/kiro-gateway`
  - 分支 `main`；remote `origin` = `github.com/zhujunsan/kiro-gateway`（我们的 fork），
    `upstream` = `jwadow/kiro-gateway`（原始上游）。
  - push 到 `main` 后，`.github/workflows/docker.yml` 会**先跑 pytest**，通过后构建并推
    `ghcr.io/zhujunsan/kiro-gateway:main-<短SHA>`（多架构）。pytest 不过则不出镜像。
- 本仓（kiro-gateway-deploy）里**两处必须同步指向同一个 SHA**：
  - `app/kiro_gateway_tray/__init__.py` 的 `UPSTREAM_SHA`（PyInstaller 打包的 App 用，
    `vendor_sync.py` 据此 clone）。
  - `docker/docker-compose.yml` 的 `image: ghcr.io/zhujunsan/kiro-gateway:main-<短SHA>`
    （docker 部署用）。

---

## 标准流程

### 1. 在上游 fork 改源码 + 测试

```bash
cd /Users/san/project/kiro-gateway
git switch main && git status   # 确认在 main、工作区干净
```

- 改 `kiro/...`（或 `main.py`）里的真实逻辑。
- **同步加/改 `tests/unit/` 下的测试**（CI 会跑 pytest，旧测试若断言被改的行为会失败）。
- 跑测试，必须全绿：

```bash
.venv/bin/python -m pytest -q
```

### 2. 提交并发布（push 到 origin/main）

- commit message 用**中文 + conventional 前缀**，与上游既有风格一致
  （如 `fix(tools): ...`、`fix(streaming): ...`、`fix(converters): ...`）。
- 用 HEREDOC 写多行说明（讲清"为什么"）。
- **禁止强推 main**。

```bash
git add <改动文件>
git commit -m "$(cat <<'EOF'
fix(<scope>): <一句话中文摘要>

<为什么这么改 / 根因 / 约束>
EOF
)"
git push origin main
git log -1 --format='%h'   # 记下新的短 SHA，下面要用
```

- 可选确认 CI 已开始/通过：`gh run list --branch main --limit 3`
  （镜像 `main-<SHA>` 要等这次 run 成功后才存在；约几分钟）。

### 3. 回本仓更新两处 pin

把上一步的短 SHA 填进去（示例 SHA `f3c8147`）：

- `app/kiro_gateway_tray/__init__.py`：`UPSTREAM_SHA = "f3c8147"`
- `docker/docker-compose.yml`：`image: ghcr.io/zhujunsan/kiro-gateway:main-f3c8147`

### 4. 重新 vendor（在 push 之后执行）

`vendor_sync.py` 会从 GitHub clone fork 到该 SHA 再拷贝，所以**必须先 push**。

```bash
cd /Users/san/project/kiro-gateway-deploy/app
.venv/bin/python scripts/vendor_sync.py   # 输出 [ok] vendored fork <SHA>
```

### 5. 校验

```bash
cd /Users/san/project/kiro-gateway-deploy
# vendored 与上游源码应完全一致
diff app/kiro_gateway_tray/vendor/kiro/converters_core.py ../kiro-gateway/kiro/converters_core.py && echo IDENTICAL
# vendor/ 不该出现在 git status（已 gitignore）；只应看到 __init__.py 与 docker-compose.yml 改动
git status --short
```

---

## 完成之后

- 以上只更新了"网关依赖 + docker tag"。若用户还要**发新版 App 安装包**
  （升 `__version__`、写 `ChangeLog.md`、打 `v*` tag），走 `make-release` skill。
- docker 部署侧用户重新 `docker compose pull && up -d` 即可（需 CI 已出镜像）。

## 常见坑

- 改了 vendor 没改上游 → 下次 sync 丢失。**改上游。**
- 只改了 `__init__.py` 没改 docker-compose（或反之）→ App 与 docker 跑的网关版本不一致。**两处一起改。**
- 没 push 就跑 vendor_sync → clone 不到新 SHA 而失败/拿到旧码。**先 push 再 sync。**
- 上游加了逻辑没加测试 → CI 的 pytest 可能因旧测试断言失败而**挡住镜像构建**。
