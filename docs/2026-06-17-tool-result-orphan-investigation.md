# 调查记录：Cursor 经由 kiro-gateway 时「工具调用一调就断」

日期：2026-06-17
状态：**根因已定位并修复，已用真实请求体验证生效**
镜像：`ghcr.io/jwadow/kiro-gateway:main-a5292ca`

---

## 一、现象（用户报告）

1. 在 Cursor 里通过本项目的 kiro-gateway 使用 Kiro 订阅的 Claude 模型，**对话经常跑着跑着就断了**。
2. 主观感受：**带工具调用的轮次特别容易坏**，纯聊天相对正常。
3. 自定义模型（`kiro-*` 别名）**经常被 Cursor 自动关掉**，需要去设置里重新打开。
4. 关键澄清：断的时候**响应很快**，「半分钟就回复完然后断了」，并不是长时间无响应。

---

## 二、最终根因（已用日志证实）

网关在 **OpenAI → 统一格式转换阶段**，把本属于配对的 `tool_result` 大量降级成纯文本。

抓到的决定性日志（06:54:44，即用户复现「断」的那次请求）：

```
Converted 43 OpenAI messages: 0 tool_calls, 18 tool_results, 1 images
Converting N orphaned tool_results to text (no preceding assistant message with tool_calls). Tool IDs: ['tooluse_ftWl6Hp...', 'tooluse_l0apLCPqC...', ...]
```

要点：
- **18 个 tool_result，却数出 0 个 tool_calls**。配对的 assistant `tool_calls` 在转换中全部丢失。
- 因此每个 tool_result 都找不到「前面那条带 tool_calls 的 assistant 消息」，被 `ensure_assistant_before_tool_results`（`kiro/converters_core.py:1036`）判为孤儿，降级成 `[tool result] ...` 文本塞回上下文。
- 那些 `tooluse_*` ID 正是本对话里一条条真实工具调用的结果，能一一对上。

### 为什么能解释全部现象
- **偏工具调用多的对话坏**：工具调用越多，被降级的 tool_result 越多。模型收到的历史里「我调用过工具」这一事实消失，只剩一堆无头无尾的文本结果，上下文被严重扭曲 → 行为错乱 / 提前结束 → 表现为「工具一调就断」。
- **响应快但断**：转换是发请求前在本地做的纯 CPU 操作，不慢；转换完照样很快返回 200。所以是「内容被破坏导致这轮无效」，不是超时。
- **模型被自动关掉**：Cursor 周期性发校验/探测请求，遇到异常响应就把自定义模型开关关掉，与上面同源。

---

## 三、被排除的错误方向（重要，避免重复走弯路）

调查过程中提出并**被证据推翻**的假设：

1. **Cloudflare 免费版 100s 超时（524）** —— 否。用户明确响应很快、半分钟就回完，根本没到 100s。
2. **`http2` 隧道传输不稳 / quic 替代** —— 否。用户试过 quic 走不通（国内 UDP 7844 常被墙），且断点与隧道无关。
3. **trim 裁得太狠 / `KIRO_MAX_PAYLOAD_BYTES=600000` 太小** —— 否。
   - 该值贴着 Kiro 上游 ~615KB 硬上限，几乎没有上调空间。
   - 上游 `trim_payload_to_limit` 已做对：成对裁剪（一次 pop 两条）、`_align_to_user_message` 对齐边界、`_repair_orphaned_tool_results` 修复落单工具结果。
   - 出问题那次请求仅 ~25K token，**根本没触发 trim**。
4. **调用了不存在的工具** —— 否。工具不存在只会返回一个失败结果，模型会自行纠正，不会整轮中断。
5. **流式 JSON 中途被网络截断** —— 否。日志显示 `Streaming completed successfully`、HTTP 200，收尾正常。

> 经验教训：前几轮在「超时 / 隧道 / trim」上反复猜测并差点基于看错的函数（`_trim_history_to_budget`，实际未被调用；真正生效的是 `payload_guards` 里的 `trim_payload_to_limit`）写补丁。最终靠打开 `DEBUG_MODE=errors` + `LOG_LEVEL=DEBUG` 抓真实请求才定位。**以日志为准，不要基于单段代码下大结论。**

---

## 四、根因定论与修复（已落地）

**结论：属于 A（网关解析 bug）。** 用 `DEBUG_MODE=all` 抓到一份真实 Cursor 请求体（53 条消息，25 条 assistant），逐条分析确认：

- **Cursor 走 OpenAI 端点（`/v1/chat/completions`），但用的是 Anthropic 风格的消息形状**：assistant 的工具调用**不在** `tool_calls` 字段里，而是放在 `content` 数组的 `{"type":"tool_use","id":"tooluse_...","name":...,"input":{...}}` 块中。
- 抓包统计：25/25 条 assistant 消息的 `tool_calls` 字段都缺失，全部携带 `tool_use` 块；35 个 `tool_use` id 与 35 个 `tool_result` 的 `tool_use_id` 一一配对（前缀均 `tooluse_`）。
- 而 `converters_openai.py: _extract_tool_calls_from_openai` **只读 `msg.tool_calls` 字段**，对 content 里的 `tool_use` 块视而不见 → 数出 0 tool_calls。tool_result 一侧（`_extract_tool_results_from_openai`）本就识别 content-block，所以才出现 `0 tool_calls, N tool_results` 的不对称，导致全部降级。

> 注：core 层的 `extract_tool_uses_from_message` 本来就能读 content-block 形式的 tool_use，但 OpenAI 适配器在更早的 `convert_openai_messages_to_unified` 里已用 `extract_text_content()` 把 assistant content 拍平成纯字符串（tool_use 块被丢弃）、且 `tool_calls=None`，等传到 core 时两条路径都拿不到数据。

### 修复

新增幂等补丁 `patches/fix_openai_tooluse_blocks.py`（sentinel 保护 + 精确锚点，失配则 `sys.exit` 报错）：

- 让 `_extract_tool_calls_from_openai` 在 `tool_calls` 字段为空时，**回退解析 `content` 里的 `tool_use` 块**，还原为统一格式的 tool_calls（`input` 作为 dict 直接放进 `arguments`，core 已兼容 dict/str 两种）。
- 只改 assistant 一侧；tool_result 提取无需动。

已挂载到 `docker-compose.yml`（volumes + 启动命令第 4 条 patch）。

### 验证（同一份真实请求体）

- 修复前：`0 tool_calls, 47 tool_results` → 全部孤儿降级。
- 修复后：`47 tool_calls, 47 tool_results`，`orphan tool_result messages remaining: 0`，`ensure_assistant_before_tool_results` 不再降级（`converted=False`）。
- 容器活体冒烟（Anthropic 风格 tool_use 块）：`tool_calls=1 tool_results=1 orphan_downgrade=False`，PASS。
- `down && up` 重建后五条 patch 全部 `[ok]`：
  - `[ok] patched MODEL_ALIASES`
  - `[ok] patched get_model_id_for_kiro`
  - `[ok] patched /usage endpoint`
  - `[ok] patched trim_payload_to_limit (keep system prompt)`
  - `[ok] patched _extract_tool_calls_from_openai (tool_use content blocks)`

---

## 五、环境与调试现状

- `docker-compose.yml` 的 `DEBUG_MODE` / `LOG_LEVEL` **已调回默认 `off` / `INFO`**（排查时曾临时设 `errors`/`all` + `DEBUG`）。需要再次抓包时：`DEBUG_MODE=errors docker compose up -d`（只落盘失败请求）或 `DEBUG_MODE=all`（每次请求覆盖落盘到容器 `/app/debug_logs/`）。
- 抓日志常用命令：
  - 实时：`docker compose logs --since <time> kiro-gateway`
  - 转换计数核对：`docker compose logs kiro-gateway 2>&1 | grep -iE 'tool_calls|orphan'`，看 `Converted N OpenAI messages: X tool_calls, Y tool_results` 的 X 是否不再为 0。

## 六、相关代码位置速查

- `kiro/converters_openai.py`
  - `convert_openai_messages_to_unified`（约 244 行）：统计/转换 OpenAI 消息，**0 tool_calls 就在这里产生**。
  - `build_kiro_payload`（约 428 行）：组装 Kiro payload。
- `kiro/converters_core.py`
  - `ensure_assistant_before_tool_results`（约 1036 行）：**把孤儿 tool_result 降级成文本**（现象的直接执行者）。
  - `process_tools_with_long_descriptions`（约 529 行）：超长工具描述移入 system prompt。
- `kiro/payload_guards.py`
  - `trim_payload_to_limit` / `check_payload_size` / `_repair_orphaned_tool_results` / `_align_to_user_message` / `_strip_empty_tool_uses`：trim 实际实现（已确认无需修改）。

---

## 七、附：另一个值得关注的次要现象

trim 触发时，裁完结果（~400KB）明显低于上限（600KB），原因是 `_repair_orphaned_tool_results` 在成对裁剪后又删掉一批 toolResults。这不致命，但说明长对话（本例单会话曾涨到 1.4MB / 580+ 条消息）会被裁掉一半以上历史。**长 agent 任务建议适时开新会话**，这是独立于主根因的最大体验杠杆。
