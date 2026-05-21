# autorefresh

自动刷新 OpenAI / Codex 账号 token 的工具集。通过 `codex login` 完成 OAuth 登录，自动填写邮箱、密码、邮箱验证码，并将新 token 写回本地多个凭证文件。

## 自动拉取额度

扫描 `~/.cli-proxy-api/*.json`，从 OpenAI `wham/usage` 接口拉取最新额度；`disabled: true` 的账号跳过；额度用尽的邮箱写入 `no_quota.txt`，并将对应账号 JSON 的 `disabled` 设为 `true`。**free** 账号周额度用尽后，新自然周（周一）再重检；**非 free** 账号仅 5 小时窗口用尽时每 5 小时重检，周额度用尽后等到新自然周再重检。若额度恢复则 `disabled` 改回 `false` 并从 `no_quota.txt` 移除。token 失效（401/403）的邮箱写入 `need_email.txt`，供 `./run_refresh.sh` 后续刷新 token。

```bash
./run_fetch_quota.sh
# 指定代理（默认 http://127.0.0.1:11080，或环境变量 QUOTA_PROXY_URL）
./run_fetch_quota.sh --proxy-url http://127.0.0.1:11080
# 直连
./run_fetch_quota.sh --proxy-url none
# 仅检查指定账号
./run_fetch_quota.sh --email xxx@outlook.com
# 强制刷新（忽略 disabled 及重检时间）
./run_fetch_quota.sh --force
./run_fetch_quota.sh --force --email xxx@outlook.com
```

## 自动刷新
```
# 默认同步 ~/.cli-proxy-api/
./run_refresh.sh
# 指定目录
./run_refresh.sh --cli-proxy-dir /path/to/dir
# 不同步原件
./run_refresh.sh --no-cli-proxy
```

## 功能

- 全自动刷新codex的token
- Codex 授权页（`auth.openai.com/.../codex/consent`）自动点击「继续」
- 刷新前备份、结束后还原 `~/.codex/auth.json` 与 `~/.codex/config.toml`
- 成功后更新：
  - `accounts.json`（JSONL）
  - `accounts/<邮箱>.json`（单账号）
  - `~/.cli-proxy-api/<邮箱>.json`（cli-proxy 原件）
- `need_email.txt` 标记 `# 已刷新 时间`，已标记账号自动跳过

## 环境要求

- Python 3.10+
- [Codex CLI](https://github.com/openai/codex)（`codex` 命令可用）
- Google Chrome（推荐，默认使用本机 Chrome + 项目内 `.chrome-profile`）

## 安装

```bash
cd autorefresh
pip3 install -r requirements.txt
python3 -m playwright install chromium
chmod +x run_refresh.sh refresh_tokens.py split_accounts.py
```

## 配置文件

### accounts.json

每行一个账号 JSON（JSONL），也支持 JSON 数组、或多个格式化 JSON 对象拼接。必填字段示例：

| 字段 | 说明 |
|------|------|
| `email` | 登录邮箱 |
| `password` | 登录密码 |
| `mailbox_url` 或 `mailbox.mailapi_url` | 收取验证码的 API |
| `access_token` / `refresh_token` / `id_token` | 刷新后会被覆盖 |

参考 `accounts.json.example`。

### need_email.txt

每行一个待刷新邮箱。刷新成功后追加标记，例如：

```
xx.xxx@outlook.com  # 已刷新 2026-05-18 14:23:05
```

带 `# 已刷新` 的行会被跳过。

### ~/.cli-proxy-api/<邮箱>.json

cli-proxy 原件，刷新后自动同步 token 相关字段（`access_token`、`refresh_token`、`id_token`、`account_id`、`last_refresh`、`expired` 等），保留 `disabled`、`type` 等原有字段。

## 使用

### 批量刷新（推荐）

编辑 `need_email.txt`，然后：

```bash
./run_refresh.sh
```

### 只刷新指定账号

```bash
./run_refresh.sh --email xxx.xxx@outlook.com
```

### 拆分 accounts.json

```bash
python3 split_accounts.py
# 输出到 accounts/，文件名如 xxx.xxx.com.json
```

## 命令行参数

| 参数 | 说明 |
|------|------|
| `--accounts` | 账号文件路径，默认 `accounts.json` |
| `--need-email` | 待刷新列表，默认 `need_email.txt` |
| `--email` | 仅刷新指定邮箱（可多次指定） |
| `--headless` | 无头模式（不推荐，易触发风控） |
| `--chrome-profile` | Chrome 用户数据目录，默认 `.chrome-profile` |
| `--use-main-chrome-profile` | 使用本机 Chrome 主配置（需先完全退出 Chrome） |
| `--cdp` | 连接已打开的 Chrome，如 `http://127.0.0.1:9222` |
| `--cli-proxy-dir` | cli-proxy 原件目录，默认 `~/.cli-proxy-api` |
| `--no-cli-proxy` | 不同步 cli-proxy 原件 |
| `--dry-run` | 不写入任何文件 |

### Chrome 调试模式示例

```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/Downloads/autorefresh/.chrome-profile"

./run_refresh.sh --cdp http://127.0.0.1:9222
```

## 注意事项

- 首次运行建议**不要**加 `--headless`，便于观察浏览器步骤
- 不要使用 `--no-system-chrome`，内置 Chromium 更容易被检测
- `accounts.json` 含敏感信息，勿提交到公开仓库（已在 `.gitignore` 中忽略部分本地目录）
- mailapi 返回的验证码从 `body` 字段用正则提取，无需额外调用 Claude

## 许可

仅供个人账号维护使用，请遵守 OpenAI 服务条款。
