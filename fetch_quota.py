#!/usr/bin/env python3
"""拉取 ~/.cli-proxy-api/ 下各账号最新 GPT 额度，额度用尽写入 no_quota.txt"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

import cli_proxy_io

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CLI_PROXY_DIR = Path.home() / ".cli-proxy-api"
DEFAULT_NO_QUOTA = SCRIPT_DIR / "no_quota.txt"
DEFAULT_NEED_EMAIL = SCRIPT_DIR / "need_email.txt"
DEFAULT_PROXY = os.environ.get("QUOTA_PROXY_URL", "http://127.0.0.1:11080")
OPENAI_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_USER_AGENT = "codex_cli_rs/0.76.0 (Debian 13.0.0; x86_64) WindowsTerminal"
TZ_CN = timezone(timedelta(hours=8))
EXHAUSTED_MARK = "额度用尽"
REFRESHED_MARK = "已刷新"
TOKEN_INVALID_STATUSES = {401, 403}
SESSION_WINDOW_SECONDS = 18_000
WEEKLY_WINDOW_SECONDS = 604_800
RECHECK_SESSION_HOURS = 5
ACCOUNT_GAP_SEC = 1


@dataclass
class NoQuotaRecord:
    email: str
    recorded_at: datetime
    scope: str  # "session" | "weekly"


@dataclass
class QuotaStatus:
    exhausted: bool
    scope: str | None = None  # "session" | "weekly"
    plan_type: str = "free"


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _timestamp() -> str:
    return datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")


def _parse_email_line(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    return stripped.split("#", 1)[0].strip() or None


def _load_json(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    return json.loads(text)


def list_account_files(cli_dir: Path) -> list[Path]:
    return cli_proxy_io.list_cli_proxy_files(cli_dir)


def build_proxies(proxy_url: str | None) -> dict[str, str] | None:
    if not proxy_url:
        return None
    return {"http": proxy_url, "https": proxy_url}


def fetch_openai_usage(
    access_token: str,
    account_id: str,
    *,
    proxy_url: str | None,
    timeout: int = 30,
) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": CODEX_USER_AGENT,
        "Chatgpt-Account-Id": account_id,
    }
    resp = requests.get(
        OPENAI_USAGE_URL,
        headers=headers,
        proxies=build_proxies(proxy_url),
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def is_token_invalid_error(exc: requests.HTTPError) -> bool:
    resp = exc.response
    if resp is None:
        return False
    if resp.status_code in TOKEN_INVALID_STATUSES:
        return True
    try:
        body = resp.text.lower()
    except Exception:  # noqa: BLE001
        return False
    return any(k in body for k in ("invalid_token", "token expired", "token_expired", "unauthorized"))


def _window_exhausted(window: dict[str, Any] | None) -> bool:
    if not window:
        return False
    return window.get("used_percent", 0) >= 100


def _classify_windows(usage: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    rate_limit = usage.get("rate_limit") or {}
    candidates = [
        w for w in (rate_limit.get("primary_window"), rate_limit.get("secondary_window")) if w
    ]
    session: dict[str, Any] | None = None
    weekly: dict[str, Any] | None = None
    for window in candidates:
        seconds = int(window.get("limit_window_seconds") or 0)
        if seconds <= SESSION_WINDOW_SECONDS + 3_600:
            session = window
        elif seconds >= WEEKLY_WINDOW_SECONDS - 86_400:
            weekly = window
    if not session and not weekly and candidates:
        weekly = candidates[0]
    return session, weekly


def analyze_quota(usage: dict[str, Any]) -> QuotaStatus:
    plan_type = str(usage.get("plan_type") or "free").lower()
    rate_limit = usage.get("rate_limit") or {}
    session, weekly = _classify_windows(usage)
    session_exhausted = _window_exhausted(session)
    weekly_exhausted = _window_exhausted(weekly)

    if plan_type == "free":
        if (
            weekly_exhausted
            or rate_limit.get("limit_reached")
            or rate_limit.get("allowed") is False
        ):
            return QuotaStatus(exhausted=True, scope="weekly", plan_type=plan_type)
        return QuotaStatus(exhausted=False, plan_type=plan_type)

    if weekly_exhausted:
        return QuotaStatus(exhausted=True, scope="weekly", plan_type=plan_type)
    if session_exhausted or rate_limit.get("limit_reached") or rate_limit.get("allowed") is False:
        return QuotaStatus(exhausted=True, scope="session", plan_type=plan_type)
    return QuotaStatus(exhausted=False, plan_type=plan_type)


def is_quota_exhausted(usage: dict[str, Any]) -> bool:
    return analyze_quota(usage).exhausted


def _format_window(label: str, window: dict[str, Any] | None) -> str | None:
    if not window:
        return None
    used = window.get("used_percent")
    reset_after = window.get("reset_after_seconds")
    hours = int(window.get("limit_window_seconds") or 0) // 3600
    parts = [f"{label}={used}%"]
    if hours:
        parts.append(f"{hours}h")
    if reset_after is not None:
        parts.append(f"reset_in={reset_after}s")
    return " ".join(parts)


def format_usage_summary(usage: dict[str, Any]) -> str:
    rate_limit = usage.get("rate_limit") or {}
    session, weekly = _classify_windows(usage)
    parts = [
        f"plan={usage.get('plan_type', '?')}",
        f"allowed={rate_limit.get('allowed')}",
        f"limit_reached={rate_limit.get('limit_reached')}",
    ]
    session_text = _format_window("session", session)
    weekly_text = _format_window("weekly", weekly)
    if session_text:
        parts.append(session_text)
    if weekly_text:
        parts.append(weekly_text)
    if not session_text and not weekly_text:
        primary = rate_limit.get("primary_window") or {}
        used = primary.get("used_percent")
        reset_after = primary.get("reset_after_seconds")
        if used is not None:
            parts.append(f"used={used}%")
        if reset_after is not None:
            parts.append(f"reset_in={reset_after}s")
    return ", ".join(parts)


def upsert_no_quota(path: Path, email: str, ts: str, scope: str) -> None:
    key = email.strip().lower()
    line = f"{email}  # {EXHAUSTED_MARK} {scope} {ts}\n"
    lines: list[str] = []
    found = False
    if path.exists():
        with path.open(encoding="utf-8") as f:
            for raw in f:
                addr = _parse_email_line(raw)
                if addr and addr.lower() == key:
                    lines.append(line)
                    found = True
                else:
                    lines.append(raw if raw.endswith("\n") else raw + "\n")
    if not found:
        lines.append(line)
    with path.open("w", encoding="utf-8") as f:
        f.writelines(lines)


def remove_from_no_quota(path: Path, email: str) -> None:
    if not path.exists():
        return
    key = email.strip().lower()
    kept: list[str] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            addr = _parse_email_line(line)
            if addr and addr.lower() == key:
                continue
            kept.append(line if line.endswith("\n") else line + "\n")
    with path.open("w", encoding="utf-8") as f:
        f.writelines(kept)


def _parse_no_quota_line(line: str) -> NoQuotaRecord | None:
    if EXHAUSTED_MARK not in line:
        return None
    email = _parse_email_line(line)
    if not email:
        return None
    tail = line.split(EXHAUSTED_MARK, 1)[1].strip()
    scope = "weekly"
    if tail.startswith("session "):
        scope = "session"
        tail = tail[len("session ") :]
    elif tail.startswith("weekly "):
        scope = "weekly"
        tail = tail[len("weekly ") :]
    try:
        recorded_at = datetime.strptime(tail, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ_CN)
    except ValueError:
        return None
    return NoQuotaRecord(email=email, recorded_at=recorded_at, scope=scope)


def load_no_quota_records(path: Path) -> dict[str, NoQuotaRecord]:
    records: dict[str, NoQuotaRecord] = {}
    if not path.exists():
        return records
    with path.open(encoding="utf-8") as f:
        for line in f:
            record = _parse_no_quota_line(line)
            if record:
                records[record.email.lower()] = record
    return records


def is_new_week_since_record(recorded_at: datetime, now: datetime | None = None) -> bool:
    """记录时间与当前不在同一自然周（周一为一周起点）则需重新拉取。"""
    now = now or datetime.now(TZ_CN)
    return recorded_at.isocalendar()[:2] != now.isocalendar()[:2]


def should_recheck_no_quota(record: NoQuotaRecord, now: datetime | None = None) -> bool:
    now = now or datetime.now(TZ_CN)
    if record.scope == "session":
        return (now - record.recorded_at) >= timedelta(hours=RECHECK_SESSION_HOURS)
    return is_new_week_since_record(record.recorded_at, now)


def _save_account_json(path: Path, account: dict[str, Any]) -> None:
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(account, f, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(path)


def mark_account_disabled(path: Path, account: dict[str, Any]) -> None:
    account["disabled"] = True
    _save_account_json(path, account)
    log(f"已写入 disabled=true -> {path.name}")


def mark_account_enabled(path: Path, account: dict[str, Any]) -> None:
    account["disabled"] = False
    _save_account_json(path, account)
    log(f"已写入 disabled=false -> {path.name}")


def append_need_email(path: Path, email: str) -> None:
    """写入 need_email.txt，供后续 refresh_tokens 刷新 token。"""
    key = email.strip().lower()
    lines: list[str] = []
    found = False
    if path.exists():
        with path.open(encoding="utf-8") as f:
            for line in f:
                addr = _parse_email_line(line)
                if addr and addr.lower() == key:
                    if not found:
                        lines.append(f"{email}\n")
                        found = True
                else:
                    lines.append(line if line.endswith("\n") else line + "\n")
    if not found:
        lines.append(f"{email}\n")
    with path.open("w", encoding="utf-8") as f:
        f.writelines(lines)
    log(f"已写入 need_email.txt: {email}")


def process_account(
    path: Path,
    *,
    proxy_url: str | None,
    no_quota_path: Path,
    need_email_path: Path,
    no_quota_records: dict[str, NoQuotaRecord],
    dry_run: bool,
    force: bool = False,
) -> str:
    try:
        account = _load_json(path)
    except json.JSONDecodeError as exc:
        log(f"跳过 {path.name}: JSON 解析失败 ({exc})")
        return "skip"

    email = str(account.get("email") or "").strip()
    if not email:
        log(f"跳过 {path.name}: JSON 缺少 email 字段")
        return "skip"
    email_key = email.lower()
    no_quota_record = no_quota_records.get(email_key)
    recheck = no_quota_record is not None and should_recheck_no_quota(no_quota_record)

    if account.get("disabled"):
        if force:
            log(f"强制刷新 {email}")
        elif recheck:
            if no_quota_record and no_quota_record.scope == "session":
                log(f"重新拉取 {email}: 5小时窗口用尽，距上次记录已超过 {RECHECK_SESSION_HOURS} 小时")
            else:
                log(f"重新拉取 {email}: 周额度用尽，no_quota 记录为上周，已进入新一周")
        else:
            if no_quota_record and no_quota_record.scope == "session":
                log(f"跳过 {email}: disabled=true (5小时窗口用尽，未到重检时间)")
            else:
                log(f"跳过 {email}: disabled=true")
            return "disabled"

    account_type = str(account.get("type") or "codex").lower()
    if account_type not in ("codex", "openai", "chatgpt"):
        log(f"跳过 {email}: 不支持的 type={account_type}")
        return "skip"

    access_token = account.get("access_token") or ""
    account_id = account.get("account_id") or ""
    if not access_token or not account_id:
        log(f"跳过 {email}: 缺少 access_token 或 account_id")
        return "skip"

    try:
        usage = fetch_openai_usage(access_token, account_id, proxy_url=proxy_url)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        if is_token_invalid_error(exc):
            log(f"token 失效 {email}: HTTP {status}")
            if not dry_run:
                append_need_email(need_email_path, email)
            return "token_invalid"
        log(f"失败 {email}: HTTP {status} ({exc})")
        return "fail"
    except requests.RequestException as exc:
        log(f"失败 {email}: 请求异常 ({exc})")
        return "fail"

    summary = format_usage_summary(usage)
    quota = analyze_quota(usage)
    ts = _timestamp()

    if quota.exhausted:
        scope = quota.scope or "weekly"
        log(f"额度用尽 {email}: {summary} [{scope}]")
        if not dry_run:
            upsert_no_quota(no_quota_path, email, ts, scope)
            mark_account_disabled(path, account)
        return "exhausted"

    log(f"可用 {email}: {summary}")
    was_in_no_quota = email_key in no_quota_records
    was_disabled = account.get("disabled")
    if not dry_run:
        remove_from_no_quota(no_quota_path, email)
        if was_disabled and (recheck or was_in_no_quota):
            mark_account_enabled(path, account)
    return "recovered" if was_disabled and was_in_no_quota else "ok"


def main() -> int:
    parser = argparse.ArgumentParser(description="拉取 cli-proxy-api 账号最新 GPT 额度")
    parser.add_argument(
        "--cli-proxy-dir",
        type=Path,
        default=DEFAULT_CLI_PROXY_DIR,
        help="账号目录，默认 ~/.cli-proxy-api",
    )
    parser.add_argument(
        "--no-quota-file",
        type=Path,
        default=DEFAULT_NO_QUOTA,
        help="额度用尽输出文件，默认 no_quota.txt",
    )
    parser.add_argument(
        "--proxy-url",
        default=os.environ.get("QUOTA_PROXY_URL", DEFAULT_PROXY),
        help="访问 OpenAI 的代理，默认 QUOTA_PROXY_URL 或 http://127.0.0.1:11080；传 none 表示直连",
    )
    parser.add_argument(
        "--need-email-file",
        type=Path,
        default=DEFAULT_NEED_EMAIL,
        help="token 失效时写入的邮箱列表，默认 need_email.txt",
    )
    parser.add_argument("--email", action="append", help="仅处理指定邮箱")
    parser.add_argument(
        "--force",
        action="store_true",
        help="强制刷新，忽略 disabled 及 no_quota 重检时间限制",
    )
    parser.add_argument("--dry-run", action="store_true", help="不写入 no_quota.txt、need_email.txt 及账号 JSON")
    args = parser.parse_args()

    cli_dir = args.cli_proxy_dir.expanduser()
    if not cli_dir.is_dir():
        log(f"目录不存在: {cli_dir}")
        return 1

    proxy_url = (args.proxy_url or "").strip()
    if proxy_url.lower() in ("none", "direct", ""):
        proxy_url = None

    files = list_account_files(cli_dir)
    if args.email:
        wanted = {e.strip().lower() for e in args.email}
        filtered: list[Path] = []
        for p in files:
            try:
                data = _load_json(p)
            except json.JSONDecodeError:
                continue
            file_email = cli_proxy_io.email_from_proxy_data(data)
            if file_email and cli_proxy_io.normalize_email(file_email) in wanted:
                filtered.append(p)
        files = filtered

    if not files:
        log("未找到账号 JSON 文件")
        return 1

    stats = {
        "ok": 0, "recovered": 0, "exhausted": 0, "disabled": 0,
        "skip": 0, "fail": 0, "token_invalid": 0,
    }
    no_quota_records = load_no_quota_records(args.no_quota_file)
    for idx, path in enumerate(files):
        result = process_account(
            path,
            proxy_url=proxy_url,
            no_quota_path=args.no_quota_file,
            need_email_path=args.need_email_file,
            no_quota_records=no_quota_records,
            dry_run=args.dry_run,
            force=args.force,
        )
        stats[result] = stats.get(result, 0) + 1
        if idx < len(files) - 1:
            time.sleep(ACCOUNT_GAP_SEC)

    log(
        "完成: "
        f"可用 {stats['ok']} 恢复 {stats['recovered']} 用尽 {stats['exhausted']} "
        f"token失效 {stats['token_invalid']} 禁用 {stats['disabled']} "
        f"跳过 {stats['skip']} 失败 {stats['fail']}"
    )
    if stats["fail"] or stats["token_invalid"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
