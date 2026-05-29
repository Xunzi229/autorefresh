"""~/.cli-proxy-api 账号 JSON：按文件内 email 字段匹配，而非文件名。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def normalize_email(email: str) -> str:
    return email.strip().lower()


def email_from_proxy_data(data: dict[str, Any]) -> str:
    return str(data.get("email") or "").strip()


def list_cli_proxy_files(proxy_dir: Path) -> list[Path]:
    if not proxy_dir.is_dir():
        return []
    return sorted(p for p in proxy_dir.glob("*.json") if p.is_file())


def load_cli_proxy_json(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    data = json.loads(text)
    return data if isinstance(data, dict) else {}


def find_cli_proxy_file(email: str, proxy_dir: Path) -> Path | None:
    """扫描 proxy_dir 下所有 JSON，按文件内 email 字段匹配。"""
    key = normalize_email(email)
    if not key:
        return None
    for path in list_cli_proxy_files(proxy_dir):
        try:
            data = load_cli_proxy_json(path)
        except json.JSONDecodeError:
            continue
        file_email = email_from_proxy_data(data)
        if file_email and normalize_email(file_email) == key:
            return path
    return None


def load_cli_proxy_by_email(email: str, proxy_dir: Path) -> tuple[dict[str, Any], Path | None]:
    path = find_cli_proxy_file(email, proxy_dir)
    if path is None:
        return {}, None
    try:
        return load_cli_proxy_json(path), path
    except json.JSONDecodeError:
        return {}, path


def resolve_cli_proxy_path(email: str, proxy_dir: Path) -> Path:
    """已有账号返回扫描到的路径；不存在则默认 proxy_dir/<email>.json（新建）。"""
    found = find_cli_proxy_file(email, proxy_dir)
    if found is not None:
        return found
    return proxy_dir / f"{email.strip()}.json"
