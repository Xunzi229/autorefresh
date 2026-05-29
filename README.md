# autorefresh

OpenAI / Codex 账号 token 自动维护工具集。配合 [cli-proxy-api](https://github.com/) 使用的 `~/.cli-proxy-api/` 原件目录，实现额度巡检、token 刷新、多文件回写。

## 适用条件（什么时候用这个仓库）

在**同时满足**以下条件时，适合使用本工具：

| 条件 | 说明 |
|------|------|
| 个人账号维护 | 为自己或已授权的少量 Outlook 等邮箱账号维护 token，非批量注册/滥用 |
| cli-proxy 生态 | 使用 `~/.cli-proxy-api/*.json` 作为代理侧账号原件（文件名可与邮箱不一致，按 JSON 内 `email` 字段匹配） |
| 邮箱 + 密码登录 | `accounts.json` 中有 `email`、`password`，且账号走邮箱验证码（非仅手机验证） |
| mailapi 收码 | 配置了 `mailbox_url` 或 `mailbox.mailapi_url`，能自动拉取 6 位邮箱验证码 |
| 本机 Chrome | macOS 上安装 **Google Chrome**（推荐）；默认用项目内 `.chrome-profile` 持久化 Cookie |
| token 需刷新 | `need_email.txt` 中有待处理邮箱，或由 `fetch_quota.py` 检测到 token 失效后写入 |

## 不适用 / 能力边界

以下情况**不要指望**本工具全自动完成，或需换方案：

| 情况 | 行为 |
|------|------|
| OAuth 强制手机验证 | `phone-verification`、`phone-otp/*`（含 `select-channel`）时，**`run_web_then_codex.sh` 会改走** `https://chatgpt.com/api/auth/session` 取 `access_token`，**通常拿不到 OAuth `refresh_token`** |
| MFA 无法走邮箱 | 只能手机等方式验证 → 标记 `need_email.txt` 为 `# 已废弃` |
| 邮箱验证码反复错误 | 标记 `# 异常` |
| 无 mailapi | 无法自动填验证码，脚本会报错退出 |
| 无密码 / 无 refresh_token 且 session 也失效 | 无法完成刷新 |
| `--headless` | 易触发 OpenAI 风控，**不推荐** |
| 纯 API、无浏览器场景 | 仅 `refresh_token` 有效时可用 API 刷新；否则必须浏览器 |
| 违反 OpenAI 服务条款的用途 | 本仓库仅供合规的个人账号维护 |

## 工具一览

```
need_email.txt ──► 刷新脚本 ──► accounts.json / accounts/*.json / ~/.cli-proxy-api/*.json
                        ▲
fetch_quota.sh ── token 失效 / 额度用尽 ── need_email.txt / no_quota.txt
```

| 入口 | 脚本 | 依赖 Codex CLI | 说明 |
|------|------|----------------|------|
| **`./run_web_then_codex.sh`** | `refresh_web_then_codex.py` | **否** | **推荐（cli-proxy 场景）**：三级刷新 + 本地 OAuth 回调；遇手机验证回退 session API |
| `./run_refresh.sh` | `refresh_tokens.py` | **是** | 经典流程：`refresh_token` API → 失败则 `codex login` + 浏览器 OAuth |
| `./run_web_session.sh` | `refresh_web_session.py` | 否 | 仅 ChatGPT 网页登录，用 session API 取 `access_token`（无 OAuth refresh_token） |
| `./run_fetch_quota.sh` | `fetch_quota.py` | 否 | 扫描 cli-proxy 原件拉额度；用尽写 `no_quota.txt`；token 失效写 `need_email.txt` |

辅助：`split_accounts.py` 拆分 `accounts.json` → `accounts/<邮箱>.json`；`cli_proxy_io.py` 按 JSON 内 `email` 扫描匹配 cli-proxy 文件。

## 推荐刷新策略（run_web_then_codex）

`refresh_web_then_codex.py` 对每个账号按顺序尝试，**成功即停**：

1. **有效跳过** — 扫描 `~/.cli-proxy-api/`，按 JSON 内 `email` 找到文件，探测 `access_token` 仍有效 → 只更新 `need_email.txt` 为「已刷新」
2. **API 刷新** — `access_token` 失效 → 用 `refresh_token` 调 `https://auth.openai.com/oauth/token`
3. **浏览器 + OAuth** — `refresh_token` 也失效 → ChatGPT 网页登录 → 本地 `http://localhost:1455/auth/callback` 完成 Codex OAuth
4. **Session 回退** — OAuth 中出现手机验证 / 手机 OTP 页 → 跳过 OAuth，改用 `https://chatgpt.com/api/auth/session` 写回 `access_token`

```bash
./run_web_then_codex.sh
./run_web_then_codex.sh --email xxx@outlook.com
./run_web_then_codex.sh --force-browser    # 跳过 1、2，强制浏览器
./run_web_then_codex.sh --no-cli-proxy       # 不同步 cli-proxy 原件
./run_web_then_codex.sh --proxy-url none     # 探测 token 时直连
```

## 经典刷新（run_refresh）

依赖本机已安装 [Codex CLI](https://github.com/openai/codex)（`codex` 在 PATH 中）：

1. `refresh_token` API 刷新
2. 失败则 `codex login` 启动 OAuth，Playwright 自动填邮箱/密码/验证码

```bash
./run_refresh.sh
./run_refresh.sh --email xxx@outlook.com
./run_refresh.sh --no-cli-proxy
```

## 仅网页 Session（run_web_session）

不跑 Codex OAuth，适合只需要 ChatGPT session `access_token`、不需要 OAuth `refresh_token` 的场景：

```bash
./run_web_session.sh
```

## 额度巡检（run_fetch_quota）

扫描 `~/.cli-proxy-api/*.json`（**按 JSON 内 `email` 识别账号**，不是文件名），请求 OpenAI `wham/usage`：

- `disabled: true` 的账号默认跳过（`--force` 可强制）
- 额度用尽 → 写入 `no_quota.txt`，原件 `disabled=true`
- **free** 账号：周额度用尽后，新自然周再重检
- **非 free**：5 小时窗口用尽每 5 小时重检；周额度用尽等到新自然周
- 额度恢复 → `disabled=false`，从 `no_quota.txt` 移除
- token 失效（401/403）→ 写入 `need_email.txt`，供刷新脚本处理

```bash
./run_fetch_quota.sh
./run_fetch_quota.sh --proxy-url http://127.0.0.1:11080   # 默认或 QUOTA_PROXY_URL
./run_fetch_quota.sh --proxy-url none                   # 直连
./run_fetch_quota.sh --email xxx@outlook.com
./run_fetch_quota.sh --force
```

## 环境要求

- Python 3.10+
- `pip install -r requirements.txt` + `python3 -m playwright install chromium`
- **Google Chrome**（`run_web_then_codex` / `run_web_session` / `run_refresh` 浏览器步骤）
- **Codex CLI**（仅 `run_refresh.sh` 需要）
- macOS 为主（Chrome 主配置路径按 macOS 编写；Linux 可用 `--chrome-profile` / CDP 变通）

## 安装

```bash
cd autorefresh
pip3 install -r requirements.txt
python3 -m playwright install chromium
chmod +x run_*.sh
```

## 配置文件

### accounts.json

JSONL（每行一个 JSON），也支持 JSON 数组或多对象拼接。常用字段：

| 字段 | 必填 | 说明 |
|------|------|------|
| `email` | 是 | 登录邮箱 |
| `password` | 浏览器刷新时必填 | 登录密码 |
| `mailbox_url` / `mailbox.mailapi_url` | 浏览器刷新时必填 | 邮箱验证码 API |
| `access_token` / `refresh_token` / `id_token` | 否 | 刷新成功后覆盖 |

参考 `accounts.json.example`。

### need_email.txt

每行一个待处理邮箱。脚本会追加状态标记：

| 标记 | 含义 |
|------|------|
| `# 已刷新 时间` | 成功，下次跳过 |
| `# 已废弃 时间` | MFA 等不可恢复，跳过 |
| `# 异常 时间` | 如验证码错误，跳过 |

参考 `need_email.txt.example`。

### ~/.cli-proxy-api/*.json

cli-proxy 原件目录：

- **匹配规则**：扫描目录下所有 `.json`，用文件内的 **`email` 字段** 与账号对应（文件名可以不一致）
- **写入规则**：找到则更新原文件；找不到则新建 `<email>.json`
- 同步字段：`access_token`、`refresh_token`、`id_token`、`account_id`、`last_refresh`、`expired` 等；保留 `disabled`、`type` 等原字段

## 刷新后写回位置

成功后会更新（除非 `--dry-run` / `--no-cli-proxy`）：

- `accounts.json`
- `accounts/<邮箱>.json`
- `~/.cli-proxy-api/` 中 email 匹配的 JSON
- `need_email.txt` 标记
- 浏览器流程会临时备份并还原 `~/.codex/auth.json`、`~/.codex/config.toml`

## 常用参数

| 参数 | 说明 |
|------|------|
| `--accounts` | 账号文件，默认 `accounts.json` |
| `--need-email` | 待处理列表，默认 `need_email.txt` |
| `--email` | 仅处理指定邮箱（可重复） |
| `--chrome-profile` | Chrome 用户数据目录，默认 `.chrome-profile` |
| `--use-main-chrome-profile` | 使用本机 Chrome 主配置（须先完全退出 Chrome） |
| `--cdp` | 连接已打开 Chrome，如 `http://127.0.0.1:9222` |
| `--cli-proxy-dir` | cli-proxy 目录，默认 `~/.cli-proxy-api` |
| `--no-cli-proxy` | 不同步 cli-proxy 原件 |
| `--headless` | 无头模式（不推荐） |
| `--dry-run` | 不写入文件 |

### Chrome CDP 调试示例

```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222 \
  --user-data-dir="$PWD/.chrome-profile"

./run_web_then_codex.sh --cdp http://127.0.0.1:9222
```

## 典型工作流

```bash
# 1. 巡检额度，把 token 失效账号写入 need_email.txt
./run_fetch_quota.sh

# 2. 编辑 need_email.txt，去掉不需要刷新的行

# 3. 批量刷新（推荐）
./run_web_then_codex.sh

# 4. 可选：拆分单账号文件
python3 split_accounts.py
```

## 注意事项

- 首次运行建议**不加** `--headless`，便于观察浏览器步骤
- 默认使用**本机 Google Chrome** + 项目内 `.chrome-profile`；避免 `--no-system-chrome`（内置 Chromium 更易被检测）
- mailapi 从响应 `body` 等字段正则提取 6 位验证码；首次进入验证码页**立即使用**邮箱中已有验证码；**仅验证码错误后**才等待 5 秒重新拉取
- OAuth 提交验证码后若页面「出错」有「重试」，会自动重填原验证码；若验证码错误会点「重新获取验证码」再拉新码
- `accounts.json`、cli-proxy 原件含敏感信息，勿提交公开仓库

## 许可

仅供个人账号维护使用，请遵守 OpenAI 服务条款。
