---
name: kiro-gateway-release
description: Kiro Gateway Tray App 提交代码与版本发布工作流。当用户说"提交代码"、"提交下"、"发版"、"发个版"、"打个版本"、"升个版本"、"出个版本"等时务必使用。涵盖：审查全部待提交变动、识别应排除的本地/构建文件、升级 `app/kiro_gateway_tray/__init__.py` 的 `__version__`、在根目录 `ChangeLog.md` 追加版本段落、运行测试、生成中文 commit、打 `v*` tag 并安全推送（禁止强推主分支）。推 tag 后 GitHub Actions 自动构建三平台安装包并发版，release 说明取自本次 ChangeLog.md 段落。
---

# Kiro Gateway Tray 发版工作流

## 触发场景

- **只提交**：用户说"提交下"、"提交代码"等
- **提交 + 发版**：用户说"发版"、"发个版"、"打个版本"等

---

## 先做检查

无论只提交还是发版，都先执行：

1. 查看 `git status`、`git diff`、`git diff --cached`、最近几条 `git log`
   - 必须覆盖工作区与暂存区的**全部待提交变动**（staged / unstaged / untracked），不能只总结本次对话里新增的 diff
   - 若仓库在本次对话开始前就有未提交改动，也要一并纳入功能总结与提交范围
2. 区分哪些是应用代码 / 文档 / 工作流改动，哪些只是本地环境或临时文件
3. 默认**不要**纳入提交，除非用户明确要求：
   - `.vscode/`、`.idea/` 等本地 IDE 配置；`.DS_Store`、临时调试文件
   - `app/.venv/`、`app/build/`、`app/release/`、`app/dist/`、`app/kiro_gateway_tray/vendor/`（vendor 由 `vendor_sync.py` 在 CI 生成）、`resources/cloudflared/`（由 `fetch_cloudflared.py` 下载）
   - 任何可能含密钥/凭据的文件（如用户本机的 `config.toml`，它不在仓库里，勿提交）
4. 如果版本说明、提交范围或版本号升级位数有歧义，先和用户确认
5. 如果判断本次可能需要升 `MINOR`（`0.x.y` 中 `x` 变化），必须先询问用户并获得同意；未确认时默认只升 `PATCH`

---

## 仅提交代码

1. 暂存需要纳入的文件；`.cursor/`、`.github/` 内与工作流相关的改动可以提交
2. 默认排除本地/构建/vendor 文件，除非用户明确要求
3. 根据变更内容生成**中文** commit message
4. `git commit`

---

## 提交 + 发版完整流程

### 1. 升版本号

唯一版本号来源是 `app/kiro_gateway_tray/__init__.py` 的 `__version__`（格式 `MAJOR.MINOR.PATCH`）。

```python
__version__ = "X.Y.Z"
```

升级规则：

- `PATCH`：小修复、参数调整、文档补充、非破坏性优化
- `MINOR`：新增功能、能力增强（升 `x` 前必须先征得用户同意）
- `MAJOR`：不兼容变更

无法明确判断时默认升 `PATCH`，并结合改动说明原因。

> CI 打 tag 时 `inject_metadata.py` 会用 tag 名再覆盖一次 `__version__`，所以**源码里的版本号必须和将要打的 tag 一致**，否则非 tag 构建（本地/PR）显示的版本会对不上。

### 2. 更新 ChangeLog.md

在根目录 `ChangeLog.md` 顶部说明之后、紧贴现有最新版本之上，追加新版本段落：

```markdown
## vX.Y.Z (YYYY-MM-DD)

**New**
- 新增内容

**Changed**
- 变更内容

**Fixed**
- 修复内容
```

- 日期用当天实际日期（`YYYY-MM-DD`）
- 只列有变动的分类，无内容的分类省略
- 内容必须基于本次发版的**全部代码变动**做功能级总结（新增/变更/修复），不能只写本次对话新增的部分
- 标题格式必须是 `## vX.Y.Z`，发版时 CI 用 `app/scripts/extract_changelog.py` 按此格式提取本段作为 GitHub Release 说明

可本地自检提取是否正常：

```bash
cd app && python scripts/extract_changelog.py vX.Y.Z
```

### 3. 验证

发版前在 `app/` 跑测试，全绿才继续：

```bash
cd app && .venv/bin/python -m pytest -q
```

- 若无 `.venv`，用项目约定的解释器；不要用系统 `python`（可能缺 pytest 等依赖）
- 构建/打包（PyInstaller、make_dist）由 CI 完成，本地无需执行
- 测试失败时向用户说明实际错误，不要声称通过

### 4. 提交

```bash
git add <确认要纳入的文件>
git commit -m "release: vX.Y.Z <本次主要变更简述>"
```

- commit message 使用**中文**
- 以修复/文档/杂项为主时也可用 `fix:` / `docs:` / `chore:`，但发版提交优先 `release: vX.Y.Z ...`
- 至少包含 `app/kiro_gateway_tray/__init__.py` 与 `ChangeLog.md` 两处改动
- 提交后再次 `git status`，确认只剩用户明确不想交的本地文件

### 5. 打 tag 并推送

```bash
git branch --show-current        # 先确认当前分支
git push origin <当前分支>
git tag vX.Y.Z
git push origin vX.Y.Z
```

- tag 格式：`v` + 版本号，如 `v0.1.11`，且必须与 `__version__` 一致
- **禁止强推主分支**；不要默认使用 `--force`
- 若本地或远程已存在同名 tag，先告知用户并确认是否覆盖，仅在用户明确要求时处理
- 推送失败优先排查快进冲突、权限或网络问题

### 6. 发版后

推送 `v*` tag 会触发 `.github/workflows/build-app.yml`：

- `build` job 在 macOS(arm64/x64)、Windows、Linux 上跑测试并用 PyInstaller 打包安装包
- `release` job 提取本次 `ChangeLog.md` 段落作为 Release 说明（其后附 CI 自动生成的提交列表），创建 GitHub Release 并上传四平台产物

可提示用户去仓库 Actions 页查看构建进度，或 `gh run watch` 跟踪。

---

## 输出要求

完成后向用户简要说明：

1. 本次纳入发版的主要变更
2. 实际提交号、新版本号、`ChangeLog.md` 条目
3. 测试是否通过、tag / push 是否成功、Actions 是否已触发
4. 哪些文件被有意排除（如 `.venv/`、`build/`、vendor、本地配置）
