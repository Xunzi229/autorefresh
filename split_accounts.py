#!/usr/bin/env python3
"""将 accounts.json 拆分为 accounts/<邮箱>.json（支持 JSONL / JSON 数组 / 多对象拼接）"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from accounts_io import parse_accounts_file

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_SRC = SCRIPT_DIR / "accounts.json"
DEFAULT_OUT = SCRIPT_DIR / "accounts"


def email_to_filename(email: str) -> str:
    """邮箱 -> 安全文件名，如 marge.bohon@outlook.com -> marge.bohon_at_outlook.com.json"""
    import re

    safe = email.strip().lower()
    safe = safe.replace("@", "_at_")
    safe = re.sub(r'[<>:"/\\|?*\s]', "_", safe)
    return f"{safe}.json"


def split_accounts(src: Path, out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    accounts = parse_accounts_file(src)
    for acc in accounts:
        email = acc.get("email")
        if not email:
            raise ValueError(f"账号缺少 email: db_id={acc.get('db_id')}")
        path = out_dir / email_to_filename(str(email))
        with path.open("w", encoding="utf-8") as out:
            json.dump(acc, out, ensure_ascii=False, indent=2)
            out.write("\n")
    return len(accounts)


def main() -> None:
    p = argparse.ArgumentParser(description="拆分 accounts.json 为单账号 JSON")
    p.add_argument("--src", type=Path, default=DEFAULT_SRC)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = p.parse_args()
    n = split_accounts(args.src, args.out)
    print(f"已拆分 {n} 个账号 -> {args.out}/")


if __name__ == "__main__":
    main()
