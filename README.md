# gpt2claude

用 ChatGPT 订阅额度在 Claude Code 里跑 GPT。

本地起一个只监听 `127.0.0.1:8787` 的协议适配器：把 Claude Code 发出的 **Anthropic Messages** 请求翻译成 Codex（ChatGPT 订阅）的 **Responses** 请求，再把流式结果翻译回 Anthropic 事件。凭证只从本机 Codex 登录文件读取，不复制、不输出，也**没有任何 API Key 计费兜底** —— 花的是订阅额度，不是按 token 付费。

纯 Python 标准库，不需要 `pip install`。Windows / macOS / Linux 通用。

| 环节 | 内容 |
| --- | --- |
| 客户端 → 本代理 | Anthropic Messages，普通 JSON 与 SSE 流式 |
| 本代理 → 上游 | `https://chatgpt.com/backend-api/codex/responses` |
| 上游鉴权 | 本机 Codex 的 ChatGPT 登录态，每次请求现读 |
| 客户端鉴权 | `config.json` 里的 `local_token`（Bearer 或 `x-api-key`） |

---

## 前置条件

| 项目 | Windows | macOS / Linux |
| --- | --- | --- |
| Python | 3.10+，命令名 `python` | 3.10+，命令名 `python3` |
| Codex CLI | 已用 ChatGPT 账号登录 | 同左 |
| Claude Code | 已安装（`claude` 在 PATH，或 `%USERPROFILE%\.local\bin\claude.exe`） | 已安装（`claude` 在 PATH，或 `~/.local/bin/claude`） |
| 账户权限 | 对 `gpt-6.1-sol` / `gpt-6.1-terra` 有权限 | 同左 |

**先确认 Codex 登录态。** 这是唯一容易卡住的前置条件：

```powershell
# Windows
codex login status
```

```bash
# macOS / Linux
codex login status
```

应看到 `Logged in using ChatGPT`。如果是 API Key 登录，代理会直接报错拒绝，不会改走按量计费。

若你的 Codex 用了自定义目录，代理会优先读 `CODEX_HOME`：

```powershell
$env:CODEX_HOME                                    # Windows
```
```bash
echo $CODEX_HOME                                   # macOS / Linux
```

---

## 快速开始

先克隆仓库：

```bash
git clone https://github.com/arvin507/gpt2claude.git
cd gpt2claude
```

下面的 Windows 示例假设克隆到 `C:\tools\gpt2claude`，macOS / Linux 示例假设克隆到 `~/gpt2claude`；请替换成自己的目录。首次启动会生成本地配置及随机访问口令，仓库不包含任何人的订阅凭证或本地密钥。

两个平台行为完全一致：**生成配置 → 已有代理则复用、否则后台启动 → 用独立的 `claude-settings.json` 启动 Claude Code**。不会修改你的全局 Claude 配置，也不会关闭 Claude Code 的工具权限确认。

### Windows

在**你希望 Claude Code 工作的项目目录**里打开 PowerShell：

```powershell
& C:\tools\gpt2claude\claude-gpt.ps1
```

如果被执行策略拦住：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\tools\gpt2claude\claude-gpt.ps1
```

### macOS / Linux

首次使用给一次执行权限（只需一次）：

```bash
cd ~/gpt2claude
chmod +x start.sh stop.sh claude-gpt.sh
```

之后在任意项目目录里（路径按你的实际位置替换）：

```bash
~/gpt2claude/claude-gpt.sh
```

需要指定解释器时：

```bash
PYTHON=/opt/homebrew/bin/python3 ~/gpt2claude/claude-gpt.sh
```

`claude-gpt.sh` 会先找 `PATH` 里的 `claude`，找不到再兜底 `~/.local/bin/claude`。传给脚本的额外参数会透传给 `claude`。

### 启动脚本到底做了什么

1. 执行 `proxy.py --init` —— 生成或补全 `config.json` 与 `claude-settings.json`（首次会随机生成 `local_token`）
2. 探测 `http://127.0.0.1:8787/health` —— 已是我方代理就直接复用，端口被别的服务占用则**明确报错而非静默接管**
3. 否则后台启动，写 `proxy.pid`，轮询 health 最多 6 秒确认起来了
4. `exec claude --settings claude-settings.json "$@"`

### 不用脚本，手动启动

Windows：

```powershell
cd C:\tools\gpt2claude
python proxy.py --init          # 只生成配置，不启动
python proxy.py                 # 前台运行，Ctrl+C 停止
```

macOS / Linux：

```bash
cd ~/gpt2claude
python3 proxy.py --init
python3 proxy.py
```

不要把 `proxy.py` 长期挂在会随终端关闭而退出的前台。日常建议用启动脚本，或自己交给 `launchd` / `systemd` / 计划任务托管。

---

## 日常管理

| 操作 | Windows | macOS / Linux |
| --- | --- | --- |
| 后台启动（幂等） | `& C:\tools\gpt2claude\start.ps1` | `./start.sh` |
| 停止 | `& C:\tools\gpt2claude\stop.ps1` | `./stop.sh` |
| 前台运行 | `python proxy.py` | `python3 proxy.py` |
| 健康检查 | `Invoke-RestMethod http://127.0.0.1:8787/health` | `curl -s http://127.0.0.1:8787/health` |
| 看诊断日志 | `Get-Content .\diagnostics.log -Tail 30 -Wait` | `tail -f diagnostics.log` |
| 看进程输出 | `Get-Content .\proxy.log -Tail 30` | `tail -30 proxy.log` |

正常返回：

```json
{"status":"ok","service":"claude-codex-proxy","upstream_model":"gpt-6.1-sol"}
```

`stop` 脚本会先校验 PID 对应进程的命令行里确实含 `proxy.py`，否则拒绝停止 —— 防止 PID 被系统复用后误杀别的进程。

**没有注册开机自启。** 机器重启后需要重新跑一次启动脚本；需要常驻的话 macOS 用 `launchd`（`RunAtLoad`），Linux 用 `systemd --user`，Windows 用任务计划程序。

---

## 接入其他客户端

除 `claude` 命令外，任何支持 Anthropic 协议的客户端都能连：

| 字段 | 值 |
| --- | --- |
| 协议 | Anthropic / Claude Messages |
| Base URL | `http://127.0.0.1:8787`（**不加** `/v1/messages`） |
| API Key / Auth Token | `config.json` 中的 `local_token`，不是 OpenAI 登录令牌 |
| 模型 | `gpt-6.1-sol` |

`claude-settings.json` 的 `env` 已含完整配置，可按客户端支持的方式导入。直接用 Claude Code CLI：

```powershell
claude --settings C:\tools\gpt2claude\claude-settings.json   # Windows
```
```bash
claude --settings ~/gpt2claude/claude-settings.json            # macOS / Linux
```

### 模型路由

模型按请求选择，不是全局写死：

| 客户端传入 | 实际调用 |
| --- | --- |
| `sonnet` / `haiku` / `claude-sonnet-4-5` | `models[0]`（默认 `gpt-6.1-sol`） |
| `opus` / `claude-opus-4-5` | `models[-1]`（默认 `gpt-6.1-terra`） |
| `claude-opus-5-5` / `claude-fable-5-1` 等目录 ID | `models[-1]` |
| `claude-sonnet-5-5` / `claude-haiku-5-5` 等目录 ID | `models[0]` |
| `gpt-6.1-sol` / `gpt-6.1-terra` | 同名模型 |

`config.json` 的 `models` 是**有序**的：第一个用于 sonnet/haiku，最后一个用于 opus。列表外的 ID 明确拒绝，不会静默回落到默认模型。

### 桌面 App 模型发现

Claude Desktop 的自动发现会过滤非 Claude 格式的模型 ID，所以代理把上游模型挂在 Desktop 认识的 ID 下，显示名/描述里标注真实上游。它**仍然调用** `config.json` 的 GPT 模型，不会调用 Claude。

| 面板里看到的 | 客户端传的 ID | 实际调用 |
| --- | --- | --- |
| `Opus · gpt-6.1-terra` | `claude-opus-5-5` | `models[-1]` |
| `Sonnet · gpt-6.1-sol` | `claude-sonnet-5-5` | `models[0]` |
| `Haiku · gpt-6.1-sol` | `claude-haiku-5-5` | `models[0]` |
| `gpt-6.1-sol` / `gpt-6.1-terra` | 同名 | 同名 |

任何 `claude-*` 形式的目录 ID 都会被接受（按家族名路由：`opus`/`fable` → `models[-1]`，其余 → `models[0]`），带 `[1m]` 后缀的 ID 会先剥掉后缀。

#### 为什么 App 里看不到思考强度 / 上下文不是 400K

**根因：Desktop 不是从 `/v1/models` 读窗口和强度，而是拿模型 ID 去查它自己的签名模型目录。**

目录在 `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Local\Claude-3p\model-catalog\published.json`（`documentBytes` 是 base64 的 JSON，带 RSASSA-PKCS1-v1_5-SHA512 签名，公钥固定在包里，**不能改**）。里面 `surfaces.ccd.model_selector_config[0].models[]` 的每个条目同时给出 `runtime.max_input_tokens` 与 `thinking.effort_options`。

App 的匹配函数（`app.asar` 内 `sCn`）先比对 `provider_ids`，再比对规范化后的 ID：

```js
let a = kT(e);                                   // 规范化传入的 id
return t.findIndex(t => oCn(t) && (kT(t.id) === a || aCn(t, n).some(p => kT(p) === a)));
```

**匹配不到 → 整条元数据都没有**：窗口落到内置默认 200000，`thinking` 为空所以强度选择器不渲染。`behaves_as` 也救不了，因为 `fCn` 要先把条目匹配上才会去读它。

所以此前对外暴露 `claude-opus-4-5` / `claude-sonnet-4-5` 必然是这个结果 —— 目录里只有 `claude-opus-4-6/4-7/4-8/5/5-5` 这些，没有 `4-5`。

顺带纠正一个此前的误判：`/v1/models` 里的 `max_input_tokens` **不会被解析**（`RCn` 只读 `id`、`display_name`、`description`、`max_effort_level`、`behaves_as`、`supports_1m`、`prefer_1m`、`anthropic_family_tier`），返回它只是给别的客户端用。真正起作用的开关是 `max_effort_level`（→ `maxEffort`，用来裁剪强度档位）和**模型 ID 本身**。

现在的做法与配套动作：

1. 代理对外只暴露目录里存在的 ID（上表），元数据因此能从目录命中，强度选择器恢复。
2. 目录把这类模型声明为 1M 窗口，而上游只有 400K，所以用环境变量把它压回 400K：

   ```powershell
   [Environment]::SetEnvironmentVariable('CLAUDE_CODE_MAX_CONTEXT_TOKENS','400000','User')
   ```
   ```bash
   # macOS / Linux：写进 ~/.zshrc 后重登
   export CLAUDE_CODE_MAX_CONTEXT_TOKENS=400000
   ```

   App 启动子 CLI 时会把该变量原样透传（`main.log` 里的 spawn 诊断可见 `CLAUDE_CODE_MAX_CONTEXT_TOKENS` 这一项）。
3. **改完必须完全退出 App 再开**（托盘图标 → Quit，确认没有残留 `claude.exe`），否则模型清单走缓存。
4. **还要在模型面板里重新选一次模型。** 旧选中项是 `claude-opus-4-5`，代理已不再暴露，继续用它不会带目录元数据。


---

## 配置

`config.json`（`--init` 自动生成，可手工改）：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `port` | `8787` | 监听端口，只绑 IPv4 回环 |
| `model` | `gpt-6.1-sol` | 默认模型，必须在 `models` 里 |
| `models` | `["gpt-6.1-sol","gpt-6.1-terra"]` | 允许的模型，**顺序决定 sonnet/opus 路由** |
| `reasoning_effort` | `low` | 默认思考强度，请求里的 `output_config.effort` 优先 |
| `context_window` | `400000` | 下发给客户端的窗口预算，不扩容上游 |
| `upstream_timeout_seconds` | `300` | 上游超时 |
| `local_token` | 随机生成 | 客户端鉴权口令 |

**思考强度**：支持 `low` / `medium` / `high` / `xhigh`，Claude 的 `max` 显式映射为 `xhigh`（只是映射，不代表同等推理语义）。需要 Claude Code 新版才会显示 `/effort` 或模型菜单里的强度选项，旧版可用 `reasoning_effort` 设默认值。

**上下文窗口**：`400000` 只是客户端的预算上限，不会让模型真的支持 400K；400K 实际请求尚未验证，token 预估也非精确分词。

**改完配置必须重启代理和 Claude Code** 才生效。`--init` 只更新设置文件，不会重启已在运行的代理。

> `config.json` 和 `claude-settings.json` 含明文 `local_token`，已在 `.gitignore` 中排除。分享或提交这个目录前请先换掉 token。

---

## 排障

| 现象 | 原因与处理 |
| --- | --- |
| `Port 8787 is occupied by another service` | 端口被别的程序占了。改 `config.json` 的 `port`，或先停掉占用者 |
| `Proxy did not start` | 看 `proxy.log`。常见是 Python 版本过低或缺依赖路径 |
| `Cannot read Codex ChatGPT credentials` | 没登录，或 `auth_mode` 不是 `chatgpt`，或 `CODEX_HOME` 指错 |
| `Codex access token expired` | 在 Codex 里重新登录即可，代理每次请求都重读 `auth.json`，**无需重启** |
| `Invalid local proxy token`（401） | 客户端填的 token 与 `config.json` 的 `local_token` 不一致；重新导入 `claude-settings.json` |
| `Unknown model` | 传入的模型不在 `models` 列表里 |
| `Unsupported effort` | effort 只接受 `low`/`medium`/`high`/`xhigh`/`max` |
| 日志里 `client disconnected` | 客户端主动断开，不等于上游报错 |

### 诊断日志

`diagnostics.log` 追加写入，按 2 MiB 自动轮转（最多 3 个备份），重启不清空。记录请求 ID、状态码、错误类型与消息；上游 HTTP 错误额外记录上游状态码和上游请求 ID，SSE 失败保留后端错误 code/message。

日志**不记录**完整请求/响应、问题正文或工具正文，常见 Bearer / JWT / API key 会脱敏。但上游返回的错误消息本身仍可能包含它引用的参数片段。

`proxy.log` 是进程标准错误输出，重启会重新创建。排查历史问题以 `diagnostics.log` 为准；启用详细日志之前的旧错误只有概括状态，无法事后恢复。

---

## 已实现

- `POST /v1/messages`：普通 JSON 与 Anthropic SSE 流式返回。
- 多轮文本、system blocks、用户图片（URL / base64）、工具结果中的图片。
- 自定义函数工具、强制工具选择、并行工具调用、工具结果回传，保留 call ID 与历史顺序。
- `GET /v1/models`、`GET /health`。
- `POST /v1/messages/count_tokens`：本地近似值，供客户端做上下文规划；响应头带 `x-token-count-approximate: true`。最终 usage 用后端实际返回值。
- 上游错误、429、连接中断转换为 Anthropic 错误；失败流不会伪装成成功。
- 只绑 IPv4 回环；除 `/health` 外必须带本地 token；只读现有 Codex 登录文件，绝不回退到 API Key 计费。

## 边界

- 这是自建的协议适配器，不是 Anthropic 官方支持的非 Claude 模型集成。实测版本为 Claude Code 2.1.19，其他版本可能需要继续适配。
- 订阅凭证每次请求从 `%CODEX_HOME%\auth.json`（非 Windows：`$CODEX_HOME/auth.json`）读取；未设置时读 `%USERPROFILE%\.codex\auth.json`（非 Windows：`~/.codex/auth.json`）。不会复制、输出或改写订阅令牌，也不会自行轮换共享的 refresh token。
- 不支持 PDF / document blocks、Anthropic 原生服务端工具、工具搜索引用、跨厂商 thinking 签名；遇到会返回明确错误。生成的设置关闭了工具搜索，普通客户端工具与 MCP 函数可用。
- GPT 推理过程不映射为 Claude thinking，只有最终文字与工具调用返回给客户端。工具参数增量在本地累积，**完整 JSON 校验通过且上游成功完成后才发布**，失败或截断不会发布半截调用。文字逐段流式发送。
- `temperature` / `top_p` / `top_k` 会校验并兼容接收但**不转发**，响应头标 `sampling=ignored`；`max_tokens` 同样校验后不转发，因此不保证输出长度上限。
- `cache_control`、`metadata`、`thinking` 及历史 thinking 兼容接收，但不提供 Anthropic 缓存、元数据存储或 thinking 签名语义。
- 新版 Claude Code 自动发送的 `context_management.edits` 中 `clear_thinking_20251015` 被接受（`keep: "all"` 或正整数 `thinking_turns`），无需转发；工具清理与服务端压缩仍明确拒绝。
- 非空 `stop_sequences`、`mcp_servers`、`container`、`service_tier`、`inference_geo`、`fallbacks` 明确拒绝，不冒充已实现的能力。MCP 应由客户端执行并作为普通函数工具传入。
- `output_config.format` 的 JSON Schema 转发上游；工具的 Anthropic strict 保证不成立，实际仍用上游 `strict: false`。
- usage 使用上游真实计数：有 `cached_tokens` 时从普通输入中扣除并计入 `cache_read_input_tokens`，缓存写入为零。`count_tokens` 仍是近似值，不代表 Anthropic 真实分词或计费。
- 响应提供 `request-id` 与 `x-proxy-compatibility`。限流保留安全的数字 `Retry-After`；未知路由返回 `not_found_error`；只有明确的上游输出 token 限额才映射 `max_tokens`，其他不完整响应报错。
- 换模型需要账户确实具备该模型权限；额度不足直接报错，没有任何兜底。

## 错误分层

- 请求 JSON / 结构错误 → `400`
- 上游 SSE JSON、UTF-8、事件结构或适配错误 → `502` / `api_error`
- 已经开始的流失败 → 发送 `error` 事件，**不会伪造 `message_stop`**

日志包含 phase、异常类型、函数与行号，但不保存异常里的输入正文。

---

## 验证

```powershell
# Windows
cd C:\tools\gpt2claude
python -m unittest -v test_proxy test_diagnostics
```

```bash
# macOS / Linux
cd ~/gpt2claude
python3 -m unittest -v test_proxy test_diagnostics
```

单元测试全部 mock 上游与凭证读取，不联网、不消耗额度、不改动用户配置。覆盖请求校验、文本多块、工具参数缓冲、失败/截断、usage、认证、错误映射与 Retry-After。

真实端到端验证（**会消耗订阅额度**）：

```powershell
python smoke_test.py            # 仅代理：普通问答 + 工具调用 + 流式回传
python smoke_test.py --claude   # 额外跑一次真实 Claude Code CLI + Read 工具
```

历史已验证：普通问答 `17 × 23 → 391`；强制工具调用；工具结果回传后的流式答案；真实 Claude Code 调用 Read 读取文件后由 GPT 返回文件校验码。新版真实客户端端到端验证尚未执行，历史结果不能视为新版已验证。

协议参考：[Claude Code 网关配置](https://code.claude.com/docs/en/llm-gateway-connect)、[Anthropic 流式消息](https://platform.claude.com/docs/en/build-with-claude/streaming)、[Hermes Codex 请求实现](https://github.com/NousResearch/hermes-agent/blob/main/agent/codex_runtime.py)。

---

## 文件清单

| 文件 | 作用 |
| --- | --- |
| `proxy.py` | 全部逻辑，仅标准库 |
| `config.json` | 端口、模型、token 等运行配置（gitignore） |
| `claude-settings.json` | 生成给 Claude Code 的独立 env 配置（gitignore） |
| `claude-gpt.ps1` / `start.ps1` / `stop.ps1` | Windows 启动、管理脚本 |
| `claude-gpt.sh` / `start.sh` / `stop.sh` | macOS / Linux 启动、管理脚本 |
| `diagnostics.log` | 归档诊断日志，2 MiB 轮转 |
| `proxy.log` / `stdout.log` / `proxy.pid` | 运行期产物 |
| `smoke_test.py` | 真实请求验证（消耗额度） |
| `test_proxy.py` / `test_diagnostics.py` | 单元测试（不联网） |
