#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

if ! python3 -c "import playwright" 2>/dev/null; then
  pip3 install -r requirements.txt
  python3 -m playwright install chromium
fi

python3 refresh_web_then_codex.py "$@"
