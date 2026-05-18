#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

if ! python3 -c "import playwright" 2>/dev/null; then
  pip3 install -r requirements.txt
  python3 -m playwright install chromium
fi

# 默认：本机 Google Chrome + 项目内 .chrome-profile（非测试用空白浏览器）
# 使用主 Chrome 配置: ./run_refresh.sh --use-main-chrome-profile  （需先退出 Chrome）
# 连接已开 Chrome:   CHROME_CDP_URL=http://127.0.0.1:9222 ./run_refresh.sh --cdp http://127.0.0.1:9222
python3 refresh_tokens.py "$@"
