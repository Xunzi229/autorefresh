#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

if ! python3 -c "import requests" 2>/dev/null; then
  pip3 install -r requirements.txt
fi

# 默认代理: QUOTA_PROXY_URL 或 http://127.0.0.1:11080
# 直连: ./run_fetch_quota.sh --proxy-url none
python3 fetch_quota.py "$@"
