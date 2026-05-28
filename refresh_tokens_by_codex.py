#!/usr/bin/env python3
"""全自动刷新 Codex token：优先 refresh_token API，失败则 codex login + 浏览器登录 + 回写 accounts.json"""

from __future__ import annotations

import argparse
import atexit
import base64
from collections import Counter
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import Page, sync_playwright
except ImportError:
    print("请先: pip install -r requirements.txt && playwright install chromium", file=sys.stderr)
    raise

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_ACCOUNTS = SCRIPT_DIR / "accounts.json"
DEFAULT_ACCOUNTS_DIR = SCRIPT_DIR / "accounts"
DEFAULT_NEED_EMAIL = SCRIPT_DIR / "need_email.txt"
CODEX_DIR = Path.home() / ".codex"
AUTH_PATH = CODEX_DIR / "auth.json"
CONFIG_PATH = CODEX_DIR / "config.toml"
CODEX_BACKUP_DIR = SCRIPT_DIR / ".codex-backup"
CLI_PROXY_DIR = Path.home() / ".cli-proxy-api"
TZ_CN = timezone(timedelta(hours=8))
DEFAULT_CHROME_PROFILE = SCRIPT_DIR / ".chrome-profile"
MAC_CHROME_USER_DATA = Path.home() / "Library/Application Support/Google/Chrome"
OAUTH_URL_RE = re.compile(r"https://auth\.openai\.com/oauth/authorize\?[^\s\]]+")
OTP_RE = re.compile(r"\b(\d{6})\b")
OTP_CONTEXT_PATTERNS = [
    re.compile(
        r"(?:verification code|login code|temporary verification code)"
        r"(?:\s*to continue)?\s*[:：]?\s*(\d{6})",
        re.I,
    ),
    re.compile(r"enter this (?:temporary )?verification code[^\d]{0,60}(\d{6})", re.I),
    re.compile(r"enter this code[:\s]+(\d{6})", re.I),
]
REFRESHED_MARK = "已刷新"
ABANDONED_MARK = "已废弃"
ABNORMAL_MARK = "异常"
ACTION_DELAY_SEC = 0.5  # 浏览器操作之间的间隔（秒）
ACCOUNT_GAP_SEC = 10  # 每个账号刷新之间的间隔（秒）
TYPE_CHAR_DELAY_MS = 200  # 逐字符输入间隔（毫秒）

# Codex OAuth refresh（与 codex-rs/login 一致）
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OAUTH_TOKEN_URL = os.environ.get(
    "CODEX_REFRESH_TOKEN_URL_OVERRIDE", "https://auth.openai.com/oauth/token"
)

# 多语言按钮文案
BTN_CONTINUE = [
    "Continue", "继续", "Allow", "允许", "Authorize", "授权", "Confirm", "确认",
    "Accept", "接受", "Yes", "是", "Got it", "知道了",
]
CONSENT_BTN_RE = re.compile(
    r"continue|继续|allow|允许|authorize|授权|confirm|确认|accept|接受",
    re.I,
)
CODEX_CONSENT_URL_RE = re.compile(
    r"auth\.openai\.com/sign-in-with-chatgpt/codex/consent",
    re.I,
)
EMAIL_VERIFICATION_URL_RE = re.compile(
    r"auth\.openai\.com/email-verification",
    re.I,
)
LOGIN_PASSWORD_URL_RE = re.compile(
    r"auth\.openai\.com/log-in/password",
    re.I,
)
LOGIN_OR_CREATE_URL_RE = re.compile(
    r"auth\.openai\.com/log-in-or-create-account",
    re.I,
)
CHOOSE_ACCOUNT_URL_RE = re.compile(
    r"auth\.openai\.com/choose-an-account",
    re.I,
)
MFA_CHALLENGE_URL_RE = re.compile(
    r"auth\.openai\.com/mfa-challenge",
    re.I,
)
MFA_EMAIL_OTP_URL_RE = re.compile(
    r"auth\.openai\.com/mfa-challenge/email-otp",
    re.I,
)
OTP_ERROR_RE = re.compile(
    r"incorrect.{0,24}code|wrong.{0,24}code|invalid.{0,24}code|"
    r"code.{0,24}(incorrect|invalid|wrong|expired)|"
    r"verification code.{0,40}(incorrect|invalid|wrong|expired)|"
    r"验证码.{0,12}(错误|不正确|无效|失效|过期)|"
    r"(错误|不正确|无效|过期)的验证码|"
    r"enter a valid code|try again",
    re.I,
)
KNOWN_AUTH_PAGE_URL_RE = re.compile(
    r"auth\.openai\.com/(?:log-in(?:/password|-or-create-account)?|email-verification|mfa-challenge)|"
    r"auth\.openai\.com/sign-in-with-chatgpt/codex/consent|"
    r"localhost:\d+",
    re.I,
)
POST_LOGIN_NAV_URL_RE = re.compile(
    r"auth\.openai\.com/(?:mfa-challenge|email-verification|sign-in-with-chatgpt/codex/consent)|"
    r"localhost:\d+",
    re.I,
)
BTN_ANOTHER_ACCOUNT = [
    "登录至另一个帐户", "登录至另一个账户",
    "Sign in to another account",
    "Use another account", "Sign in with a different account", "Log in with another account",
    "使用其他账号", "使用另一个帐户", "使用另一个账户",
    "登录其他帐户", "登录其他账户", "登录另一个帐户", "登录另一个账户", "使用其他帐户",
]
BTN_MFA_TRY_OTHER = [
    "尝试其他方法", "Try another method", "Use another method", "Try a different method",
]
BTN_MFA_EMAIL = [
    "电子邮件", "Email", "E-mail", "email",
]
BTN_SUBMIT = ["Continue", "继续", "Log in", "登录", "Verify", "验证", "Submit", "提交", "Next", "下一步"]
CHOOSE_ANOTHER_ACCOUNT_RE = re.compile(
    r"登录(?:至|到)?另一个[账帐]|登录另一个[账帐]|使用另一个[账帐]|登录其他[账帐]|"
    r"使用其他账号|使用另一个[账帐]|使用其他[账帐]|"
    r"Sign in to another account|Use another account|Log in with another account|"
    r"Sign in with a different account",
    re.I,
)
CHOOSE_ACCOUNT_EXCLUDE_RE = re.compile(
    r"remove|delete|移除|删除|注销|登出|sign out|log out|"
    r"forget|清除|clear|unlink|revoke|忘记|remove account|删除[账帐]户|移除[账帐]户",
    re.I,
)

_shutdown_requested = threading.Event()
_active_login: Any = None
_sigint_count = 0


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _check_shutdown() -> None:
    if _shutdown_requested.is_set():
        raise KeyboardInterrupt("用户中断")


def _interruptible_sleep(sec: float) -> None:
    deadline = time.time() + sec
    while time.time() < deadline:
        _check_shutdown()
        time.sleep(min(0.25, deadline - time.time()))


def _handle_interrupt(signum: int | None = None) -> None:
    """还原 ~/.codex、终止 codex login，立即退出（不等待 Playwright 清理）。"""
    if not _shutdown_requested.is_set():
        sig_label = signum if signum is not None else signal.SIGINT
        log(f"收到信号 {sig_label}，正在还原 ~/.codex …")
        backup = _active_codex_backup
        if backup is not None:
            backup.restore()
        _shutdown_requested.set()
        login = _active_login
        if login is not None:
            login.terminate()
    code = 128 + signum if signum is not None else 130
    log("已中断，退出")
    os._exit(code)


def _poll_until(
    predicate: Callable[[], bool],
    *,
    timeout_sec: float = 20,
    interval: float = 0.4,
) -> bool:
    """短间隔轮询，避免 Playwright 长 wait 阻塞 Ctrl+C。"""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        _check_shutdown()
        try:
            if predicate():
                return True
        except PlaywrightError:
            if _shutdown_requested.is_set():
                _check_shutdown()
            raise
        _interruptible_sleep(interval)
    try:
        return predicate()
    except PlaywrightError:
        if _shutdown_requested.is_set():
            _check_shutdown()
        return False


def load_accounts(path: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    from accounts_io import parse_accounts_file

    rows = parse_accounts_file(path)
    index: dict[str, int] = {}
    for i, row in enumerate(rows):
        email = str(row.get("email", "")).lower()
        if email:
            index[email] = i
    return rows, index


def _parse_need_email_line(line: str) -> str | None:
    """解析行内邮箱；整行注释或空行返回 None。"""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    return stripped.split("#", 1)[0].strip() or None


def _is_marked_refreshed(line: str) -> bool:
    return REFRESHED_MARK in line


def _is_marked_abandoned(line: str) -> bool:
    return ABANDONED_MARK in line


def _is_marked_abnormal(line: str) -> bool:
    return ABNORMAL_MARK in line


def _refresh_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def is_skipped_abnormal(path: Path, email: str) -> bool:
    """need_email.txt 中已标记「异常」则跳过。"""
    if not path.exists():
        return False
    key = email.strip().lower()
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not _is_marked_abnormal(line):
                continue
            addr = _parse_need_email_line(line)
            if addr and addr.lower() == key:
                return True
    return False


def is_skipped_abandoned(path: Path, email: str) -> bool:
    """need_email.txt 中已标记「已废弃」则跳过。"""
    if not path.exists():
        return False
    key = email.strip().lower()
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not _is_marked_abandoned(line):
                continue
            addr = _parse_need_email_line(line)
            if addr and addr.lower() == key:
                return True
    return False


def is_skipped_refreshed(path: Path, email: str) -> bool:
    """need_email.txt 中已标记「已刷新」则跳过。"""
    if not path.exists():
        return False
    key = email.strip().lower()
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not _is_marked_refreshed(line):
                continue
            addr = _parse_need_email_line(line)
            if addr and addr.lower() == key:
                return True
    return False


def load_need_emails(path: Path) -> list[str]:
    emails: list[str] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            if _is_marked_refreshed(line) or _is_marked_abandoned(line) or _is_marked_abnormal(line):
                continue
            email = _parse_need_email_line(line)
            if email:
                emails.append(email)
    return emails


def mark_abnormal_in_need_emails(path: Path, email: str, *, reason: str = "验证码错误") -> bool:
    """在 need_email.txt 标记「异常」，后续自动跳过。"""
    if not path.exists():
        return False
    key = email.strip().lower()
    ts = _refresh_timestamp()
    lines: list[str] = []
    marked = False
    with path.open(encoding="utf-8") as f:
        for line in f:
            addr = _parse_need_email_line(line)
            if addr and addr.lower() == key:
                lines.append(f"{addr}  # {ABNORMAL_MARK} {reason} {ts}\n")
                marked = True
            else:
                lines.append(line if line.endswith("\n") else line + "\n")
    if marked:
        with path.open("w", encoding="utf-8") as f:
            f.writelines(lines)
        log(f"已在 {path.name} 标记: {email}  # {ABNORMAL_MARK} {reason} {ts}")
    return marked


def mark_abandoned_in_need_emails(path: Path, email: str, *, reason: str = "MFA") -> bool:
    """在 need_email.txt 标记「已废弃」，后续自动跳过。"""
    if not path.exists():
        return False
    key = email.strip().lower()
    ts = _refresh_timestamp()
    lines: list[str] = []
    marked = False
    with path.open(encoding="utf-8") as f:
        for line in f:
            addr = _parse_need_email_line(line)
            if addr and addr.lower() == key:
                lines.append(f"{addr}  # {ABANDONED_MARK} {reason} {ts}\n")
                marked = True
            else:
                lines.append(line if line.endswith("\n") else line + "\n")
    if marked:
        with path.open("w", encoding="utf-8") as f:
            f.writelines(lines)
        log(f"已在 {path.name} 标记: {email}  # {ABANDONED_MARK} {reason} {ts}")
    return marked


def mark_refreshed_in_need_emails(path: Path, email: str) -> bool:
    """在 need_email.txt 标记「已刷新」并记录时间，不删除该行。"""
    if not path.exists():
        return False
    key = email.strip().lower()
    ts = _refresh_timestamp()
    lines: list[str] = []
    marked = False
    with path.open(encoding="utf-8") as f:
        for line in f:
            addr = _parse_need_email_line(line)
            if addr and addr.lower() == key:
                lines.append(f"{addr}  # {REFRESHED_MARK} {ts}\n")
                marked = True
            else:
                lines.append(line if line.endswith("\n") else line + "\n")
    if marked:
        with path.open("w", encoding="utf-8") as f:
            f.writelines(lines)
        log(f"已在 {path.name} 标记: {email}  # {REFRESHED_MARK} {ts}")
    return marked


def get_mailapi_url(account: dict[str, Any]) -> str | None:
    mailbox = account.get("mailbox") or {}
    if isinstance(mailbox, dict):
        for key in ("mailapi_url", "mailbox_url"):
            if mailbox.get(key):
                return str(mailbox[key])
    for key in ("mailapi_url", "mailbox_url"):
        if account.get(key):
            return str(account[key])
    return None


def extract_code_from_mailapi(mail_data: dict[str, Any]) -> str | None:
    """从 mailapi JSON 用正则提取 6 位验证码（优先 code 字段与正文关键词上下文）。"""
    code_field = mail_data.get("code")
    if code_field is not None:
        s = str(code_field).strip()
        if OTP_RE.fullmatch(s):
            return s
        m = OTP_RE.search(s)
        if m:
            return m.group(1)

    parts: list[str] = []
    for key in ("body", "subject", "text", "content"):
        val = mail_data.get(key)
        if val:
            parts.append(str(val))
    if not parts:
        return None
    combined = "\n".join(parts)

    for pat in OTP_CONTEXT_PATTERNS:
        m = pat.search(combined)
        if m:
            return m.group(1)

    # 正文中所有独立 6 位数字，取出现次数最多的（验证码常在邮件中重复出现）
    hits = OTP_RE.findall(combined)
    if not hits:
        return None
    return Counter(hits).most_common(1)[0][0]


def fetch_email_code(mailapi_url: str, timeout: int = 180, interval: int = 5) -> str:
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        _check_shutdown()
        try:
            resp = requests.get(mailapi_url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            _interruptible_sleep(interval)
            continue

        status = str(data.get("status", "")).lower()
        if status and status not in ("success", "ok", ""):
            last_error = f"status={status}"
            _interruptible_sleep(interval)
            continue

        code = extract_code_from_mailapi(data)
        if code:
            log(f"验证码: {code} (from={data.get('from', '?')})")
            return code

        _interruptible_sleep(interval)

    raise TimeoutError(f"等待邮箱验证码超时 ({timeout}s): {last_error}")


class CodexLoginProcess:
    """后台保持 codex login 进程，直到 OAuth 回调结束。"""

    def __init__(self) -> None:
        self.proc: subprocess.Popen[str] | None = None
        self.oauth_url: str | None = None
        self._output: list[str] = []
        self._lock = threading.Lock()

    def _reader(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            with self._lock:
                self._output.append(line)
            sys.stdout.write(line)
            sys.stdout.flush()
            if not self.oauth_url:
                m = OAUTH_URL_RE.search(line)
                if m:
                    self.oauth_url = m.group(0).rstrip(").,]'\"")
                    log("已捕获 OAuth URL")

    def start(self) -> str:
        self.proc = subprocess.Popen(
            ["codex", "login"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        t = threading.Thread(target=self._reader, daemon=True)
        t.start()

        deadline = time.time() + 90
        while time.time() < deadline:
            _check_shutdown()
            if self.oauth_url:
                return self.oauth_url
            if self.proc.poll() is not None:
                break
            _interruptible_sleep(0.3)

        with self._lock:
            buf = "".join(self._output)
        raise RuntimeError(f"未能从 codex login 输出解析 OAuth URL:\n{buf}")

    def terminate(self) -> None:
        if not self.proc or self.proc.poll() is not None:
            return
        log("终止 codex login 进程…")
        try:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        except Exception as exc:  # noqa: BLE001
            log(f"终止 codex login 失败: {exc}")

    def wait_done(self, timeout: int = 300) -> None:
        """等待 codex login 自然结束（回调完成），不主动 kill。"""
        if not self.proc:
            return
        if _shutdown_requested.is_set():
            self.terminate()
            return
        try:
            self.proc.wait(timeout=timeout)
            log(f"codex login 进程已结束 (code={self.proc.returncode})")
        except subprocess.TimeoutExpired:
            log("codex login 仍在运行（回调可能已完成，继续检查 auth.json）")

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


def _auth_file_ready(auth_path: Path) -> bool:
    if not auth_path.exists():
        return False
    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
        tokens = data.get("tokens") or {}
        if tokens.get("access_token") or data.get("OPENAI_API_KEY"):
            return True
        if data.get("auth_mode") == "chatgpt" and tokens:
            return True
    except json.JSONDecodeError:
        pass
    return False


def wait_for_auth(auth_path: Path, login: CodexLoginProcess, timeout: int = 300) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        _check_shutdown()
        if _auth_file_ready(auth_path):
            return json.loads(auth_path.read_text(encoding="utf-8"))
        _interruptible_sleep(2)

    raise TimeoutError(f"等待 {auth_path} 写入超时")


def _jwt_payload(token: str) -> dict[str, Any]:
    payload = token.split(".")[1]
    padding = "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload + padding))


def _pause(sec: float = ACTION_DELAY_SEC) -> None:
    _interruptible_sleep(sec)


def _auth_path(url: str) -> str:
    return urlparse(url).path.rstrip("/") or "/"


def _is_codex_consent_page(page: Page) -> bool:
    return bool(CODEX_CONSENT_URL_RE.search(page.url)) or "/codex/consent" in _auth_path(page.url)


def _is_email_verification_page(page: Page) -> bool:
    return bool(EMAIL_VERIFICATION_URL_RE.search(page.url)) or _auth_path(page.url).endswith(
        "/email-verification"
    )


def _is_login_email_page(page: Page) -> bool:
    """https://auth.openai.com/log-in"""
    return _auth_path(page.url) == "/log-in"


def _is_login_or_create_page(page: Page) -> bool:
    """https://auth.openai.com/log-in-or-create-account"""
    return (
        bool(LOGIN_OR_CREATE_URL_RE.search(page.url))
        or _auth_path(page.url) == "/log-in-or-create-account"
    )


def _is_choose_account_page(page: Page) -> bool:
    """https://auth.openai.com/choose-an-account"""
    return (
        bool(CHOOSE_ACCOUNT_URL_RE.search(page.url))
        or _auth_path(page.url) == "/choose-an-account"
    )


def _is_login_password_page(page: Page) -> bool:
    """https://auth.openai.com/log-in/password"""
    return bool(LOGIN_PASSWORD_URL_RE.search(page.url)) or _auth_path(page.url) == "/log-in/password"


def _is_mfa_email_otp_page(page: Page) -> bool:
    """https://auth.openai.com/mfa-challenge/email-otp"""
    return "mfa-challenge/email-otp" in page.url.lower().split("?", 1)[0]


def _is_mfa_challenge_root(page: Page) -> bool:
    """mfa-challenge 主流程页：/mfa-challenge 或 /mfa-challenge/<session_id>，不含 email-otp。"""
    if _is_mfa_email_otp_page(page):
        return False
    path = _auth_path(page.url)
    if path == "/mfa-challenge":
        return True
    # 例: /mfa-challenge/6a0fab9a0ad88191b6956bdd56de25fc（排除 email-otp）
    return bool(re.match(r"^/mfa-challenge/(?!email-otp)[^/]+$", path))


def _is_mfa_challenge_page(page: Page) -> bool:
    return _is_mfa_challenge_root(page) or _is_mfa_email_otp_page(page)


def _is_otp_code_page(page: Page) -> bool:
    return _is_email_verification_page(page) or _is_mfa_email_otp_page(page)


class MfaChallengeError(Exception):
    """MFA 无法走邮箱验证或其他方式，账号视为废弃。"""


class AccountAbnormalError(Exception):
    """账号异常（如邮箱验证码错误），无法继续自动刷新。"""


def _detect_otp_error(page: Page) -> str | None:
    """检测验证码页（email-verification / mfa email-otp）上的错误提示。"""
    if not _is_otp_code_page(page):
        return None

    snippets: list[str] = []
    for sel in ('[role="alert"]', '[data-testid*="error"]', '[class*="error"]'):
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 8)):
                text = (loc.nth(i).inner_text(timeout=800) or "").strip()
                if text:
                    snippets.append(text)
        except Exception:  # noqa: BLE001
            pass

    try:
        body_text = page.locator("body").inner_text(timeout=3000) or ""
        if body_text:
            snippets.append(body_text)
    except Exception:  # noqa: BLE001
        pass

    for text in snippets:
        for line in text.splitlines():
            line = line.strip()
            if line and OTP_ERROR_RE.search(line):
                return line[:160]
        if OTP_ERROR_RE.search(text):
            m = OTP_ERROR_RE.search(text)
            if m:
                start = max(0, m.start() - 20)
                return text[start : m.end() + 40].strip()[:160]
    return None


def _check_otp_error_or_raise(page: Page) -> None:
    err = _detect_otp_error(page)
    if err:
        raise AccountAbnormalError(f"邮箱验证码错误: {err}")


def _login_page_kind(page: Page) -> str:
    """按 URL + DOM 判定当前登录阶段。"""
    if _is_callback_url(page.url):
        return "callback"
    # 授权页优先（OTP 提交后 URL 可能仍停在 email-otp）
    if _is_codex_consent_page(page):
        return "consent"
    if _is_mfa_email_otp_page(page):
        return "mfa_email_otp"
    if _is_mfa_challenge_root(page):
        return "mfa_challenge"
    if _is_email_verification_page(page):
        return "email_verification"
    if _is_login_password_page(page):
        return "password"
    if _is_choose_account_page(page):
        return "choose_account"
    if _is_login_or_create_page(page):
        return "login_or_create"
    if _is_login_email_page(page):
        # URL 仍为 /log-in 但已出现密码框
        if _password_visible(page):
            return "password"
        return "login"
    return "unknown"


def _log_current_page(page: Page, kind: str | None = None) -> None:
    kind = kind or _login_page_kind(page)
    short = page.url.split("?", 1)[0]
    log(f"当前页面[{kind}]: {short}")


def _wait_known_auth_page(page: Page, timeout_sec: int = 20) -> None:
    _poll_until(
        lambda: bool(KNOWN_AUTH_PAGE_URL_RE.search(page.url)),
        timeout_sec=timeout_sec,
    )


def _wait_email_verification_page(page: Page, timeout_sec: int = 30) -> bool:
    if _poll_until(lambda: _is_email_verification_page(page), timeout_sec=timeout_sec):
        log("已进入邮箱验证码页 email-verification")
        return True
    return _is_email_verification_page(page)


def _wait_codex_consent_page(page: Page, timeout_sec: int = 30) -> bool:
    if _poll_until(lambda: _is_codex_consent_page(page), timeout_sec=timeout_sec):
        log("已进入 Codex 授权页")
        return True
    return _is_codex_consent_page(page)


def _click_codex_consent_continue(page: Page, timeout: int = 8000) -> bool:
    """Codex 授权页 https://auth.openai.com/.../codex/consent 点击「继续」。"""
    if not _is_codex_consent_page(page):
        return False

    log("Codex 授权页，点击「继续」")
    _pause(1)

    # 优先精确匹配「继续」
    for loc in (
        page.get_by_role("button", name="继续"),
        page.get_by_role("button", name=re.compile(r"^\s*继续\s*$")),
        page.locator('button:has-text("继续")'),
        page.get_by_text("继续", exact=True),
    ):
        try:
            target = loc.first
            target.wait_for(state="visible", timeout=3000)
            for _ in range(24):
                if target.is_enabled():
                    break
                _pause(0.25)
            if not target.is_enabled():
                log("授权页「继续」按钮不可用，可能已完成授权")
                return False
            target.click(timeout=timeout)
            _pause()
            log("已点击「继续」")
            return True
        except Exception:  # noqa: BLE001
            pass

    return False


def _wait_oauth_complete_or_leave_consent(
    page: Page,
    auth_path: Path | None,
    *,
    timeout_sec: float = 45,
) -> bool:
    """点击授权「继续」后：auth.json 已写入，或浏览器离开 consent/进入 localhost。"""
    def ready() -> bool:
        if auth_path is not None and _auth_file_ready(auth_path):
            return True
        if _is_callback_url(page.url):
            return True
        return not _is_codex_consent_page(page)

    return _poll_until(ready, timeout_sec=timeout_sec)


def _click_otp_submit(page: Page, timeout: int = 10000) -> bool:
    """验证码页提交：点击「继续」（email-verification / mfa email-otp）。"""
    if not _is_otp_code_page(page):
        return False
    log("验证码页，点击「继续」")
    _pause(1.5)

    for loc in (
        page.get_by_role("button", name="继续"),
        page.get_by_role("button", name=re.compile(r"^\s*继续\s*$")),
        page.locator('button:has-text("继续")'),
        page.get_by_role("button", name=re.compile(r"^\s*Continue\s*$", re.I)),
        page.locator('button:has-text("Continue")'),
        page.locator('button[type="submit"]'),
    ):
        try:
            btn = loc.first
            btn.wait_for(state="visible", timeout=5000)
            for _ in range(24):
                if btn.is_enabled():
                    break
                _pause(0.25)
            btn.click(timeout=timeout)
            _pause()
            log("已点击验证码页「继续」")
            return True
        except Exception:  # noqa: BLE001
            pass

    try:
        buttons = page.locator("button")
        for i in range(min(buttons.count(), 25)):
            btn = buttons.nth(i)
            if not btn.is_visible():
                continue
            text = (btn.inner_text(timeout=1000) or "").strip()
            if not text:
                continue
            if text in ("继续", "Continue", "Verify", "验证", "Submit", "提交") or CONSENT_BTN_RE.search(text):
                if not btn.is_enabled():
                    continue
                btn.click(timeout=timeout)
                _pause()
                log(f"已点击验证码页按钮: {text[:24]}")
                return True
    except Exception:  # noqa: BLE001
        pass

    return _click_text(page, ["继续", "Continue", "Verify", "验证", "Submit", "提交"], timeout=timeout)


def _wait_leave_otp_page(page: Page, timeout_sec: int = 20) -> bool:
    if not _is_otp_code_page(page):
        return True
    if _poll_until(lambda: not _is_otp_code_page(page), timeout_sec=timeout_sec):
        log("已离开验证码页")
        return True
    return not _is_otp_code_page(page)


def _wait_leave_email_verification(page: Page, timeout_sec: int = 20) -> bool:
    return _wait_leave_otp_page(page, timeout_sec=timeout_sec)


def _try_another_account(page: Page, *, timeout: int = 8000) -> bool:
    """OAuth/账户选择页：点击「登录至另一个账户」等进入 log-in（非 MFA 页）。"""
    if (
        _is_login_email_page(page)
        or _is_login_or_create_page(page)
        or _is_login_password_page(page)
        or _is_email_verification_page(page)
        or _is_mfa_challenge_page(page)
        or _is_codex_consent_page(page)
        or _is_callback_url(page.url)
        or "auth.openai.com/mfa-challenge" in page.url.lower()
    ):
        return False

    if _is_choose_account_page(page):
        return _click_choose_another_account(page, timeout=timeout)

    log("选择「登录另一个账户」")
    for text in BTN_ANOTHER_ACCOUNT:
        for role in ("button", "link"):
            loc = page.get_by_role(role, name=re.compile(re.escape(text), re.I))
            try:
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click(timeout=timeout)
                    _pause(2)
                    log(f"已点击「{text}」")
                    return True
            except Exception:  # noqa: BLE001
                pass
        loc = page.locator(f"text={text}")
        try:
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click(timeout=timeout)
                _pause(2)
                log(f"已点击「{text}」")
                return True
        except Exception:  # noqa: BLE001
            pass

    if _click_text(page, BTN_ANOTHER_ACCOUNT, timeout=timeout):
        _pause(2)
        log("已点击「登录另一个账户」")
        return True

    return False


def _control_label(el: Any) -> str:
    """读取按钮/链接的可点击文案（inner_text + aria-label）。"""
    parts: list[str] = []
    try:
        text = (el.inner_text(timeout=1500) or "").strip()
        if text:
            parts.append(re.sub(r"\s+", " ", text))
    except Exception:  # noqa: BLE001
        pass
    for attr in ("aria-label", "title"):
        try:
            val = (el.get_attribute(attr) or "").strip()
            if val:
                parts.append(val)
        except Exception:  # noqa: BLE001
            pass
    return " | ".join(dict.fromkeys(parts))


def _is_choose_another_account_label(text: str) -> bool:
    label = re.sub(r"\s+", " ", (text or "").strip())
    if not label or len(label) > 120:
        return False
    if CHOOSE_ACCOUNT_EXCLUDE_RE.search(label):
        return False
    return bool(CHOOSE_ANOTHER_ACCOUNT_RE.search(label))


def _click_locator_if_match(
    loc: Any,
    pattern: re.Pattern[str],
    *,
    timeout: int = 10000,
    label_check: Callable[[str], bool] | None = None,
) -> bool:
    checker = label_check or (lambda text: bool(text and pattern.search(text)))
    try:
        count = loc.count()
    except Exception:  # noqa: BLE001
        return False
    for i in range(min(count, 12)):
        try:
            el = loc.nth(i)
            if not el.is_visible():
                continue
            label = _control_label(el)
            if not checker(label):
                continue
            el.scroll_into_view_if_needed(timeout=3000)
            try:
                el.click(timeout=timeout)
            except Exception:  # noqa: BLE001
                el.click(timeout=timeout, force=True)
            _pause(2)
            log(f"已点击: {label[:48]}")
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _click_choose_another_account(page: Page, *, timeout: int = 10000) -> bool:
    """choose-an-account 页：仅点击「登录至另一个账户」类链接，避免误点移除账号。"""
    if not _is_choose_account_page(page):
        return False

    log("choose-an-account 页，点击「登录至另一个账户」")
    _pause(1)

    # 页面常见繁体「帐户」，优先精确点击
    for exact_text in ("登录至另一个帐户", "登录至另一个账户", "Sign in to another account"):
        try:
            loc = page.get_by_text(exact_text, exact=True)
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click(timeout=timeout)
                _pause(2)
                log(f"已点击: {exact_text}")
                if _poll_until(lambda: not _is_choose_account_page(page), timeout_sec=12):
                    log("已离开 choose-an-account 页")
                    return True
        except Exception:  # noqa: BLE001
            pass

    candidates: list[Any] = []
    for text in BTN_ANOTHER_ACCOUNT:
        candidates.extend(
            [
                page.get_by_role("link", name=text, exact=True),
                page.get_by_role("button", name=text, exact=True),
                page.get_by_role("link", name=re.compile(re.escape(text), re.I)),
                page.get_by_role("button", name=re.compile(re.escape(text), re.I)),
            ]
        )
    candidates.extend(
        [
            page.get_by_role("link", name=CHOOSE_ANOTHER_ACCOUNT_RE),
            page.get_by_role("button", name=CHOOSE_ANOTHER_ACCOUNT_RE),
            page.get_by_text(CHOOSE_ANOTHER_ACCOUNT_RE),
            page.get_by_label(CHOOSE_ANOTHER_ACCOUNT_RE),
            page.locator("a, button, [role='button'], [role='link']").filter(
                has_text=CHOOSE_ANOTHER_ACCOUNT_RE
            ),
        ]
    )
    for text in ("登录至另一个帐户", "登录至另一个账户", "Sign in to another account"):
        candidates.append(page.get_by_label(text, exact=True))

    for loc in candidates:
        if _click_locator_if_match(
            loc,
            CHOOSE_ANOTHER_ACCOUNT_RE,
            timeout=timeout,
            label_check=_is_choose_another_account_label,
        ):
            if _poll_until(lambda: not _is_choose_account_page(page), timeout_sec=12):
                log("已离开 choose-an-account 页")
                return True
            log("点击后仍在 choose-an-account，尝试下一种方式…")

    try:
        text_loc = page.get_by_text(CHOOSE_ANOTHER_ACCOUNT_RE)
        for i in range(min(text_loc.count(), 6)):
            el = text_loc.nth(i)
            if not el.is_visible():
                continue
            label = _control_label(el)
            if not _is_choose_another_account_label(label):
                continue
            for xpath in (
                "xpath=ancestor-or-self::button[1]",
                "xpath=ancestor-or-self::a[1]",
                "xpath=ancestor-or-self::*[@role='button'][1]",
                "xpath=ancestor-or-self::*[@role='link'][1]",
            ):
                try:
                    target = el.locator(xpath)
                    if target.count() > 0 and target.first.is_visible():
                        target.first.click(timeout=timeout, force=True)
                        _pause(2)
                        if not _is_choose_account_page(page):
                            log("已通过祖先元素点击离开 choose-an-account")
                            return True
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass

    try:
        hints: list[str] = []
        for sel in ("a", "button", "[role=button]", "[role=link]"):
            loc = page.locator(sel)
            for i in range(min(loc.count(), 15)):
                try:
                    label = _control_label(loc.nth(i))
                    if label and len(label) < 80:
                        hints.append(label)
                except Exception:  # noqa: BLE001
                    pass
        if hints:
            log(f"choose-an-account 未点到，页内可见: {hints[:8]}")
    except Exception:  # noqa: BLE001
        pass

    return False


def _handle_choose_account(page: Page) -> bool:
    """choose-an-account 页点击「登录至另一个账户」。"""
    return _click_choose_another_account(page)


def _click_text(page: Page, texts: list[str], timeout: int = 8000) -> bool:
    for text in texts:
        for role in ("button", "link"):
            loc = page.get_by_role(role, name=re.compile(re.escape(text), re.I))
            try:
                if loc.count() > 0:
                    loc.first.click(timeout=timeout)
                    _pause()
                    return True
            except Exception:  # noqa: BLE001
                pass
        loc = page.locator(f"text={text}")
        try:
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click(timeout=timeout)
                _pause()
                return True
        except Exception:  # noqa: BLE001
            pass
    return False


def _first_visible(page: Page, selectors: list[str], timeout: int = 12000):
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            loc.wait_for(state="visible", timeout=timeout)
            return loc
        except Exception:  # noqa: BLE001
            continue
    return None


def _type_chars(el: Any, text: str) -> None:
    """逐字符输入（模拟手动键入）。"""
    el.click()
    el.fill("")
    el.press_sequentially(text, delay=TYPE_CHAR_DELAY_MS)


def _fill_and_go(page: Page, value: str, input_selectors: list[str]) -> None:
    el = _first_visible(page, input_selectors)
    if not el:
        raise RuntimeError(f"找不到输入框: {input_selectors}")
    _pause()
    _type_chars(el, value)
    _pause()
    if not _click_text(page, BTN_SUBMIT, timeout=5000):
        page.keyboard.press("Enter")
        _pause()


def _is_callback_url(url: str) -> bool:
    return bool(re.search(r"localhost:\d+", url))


def _password_visible(page: Page) -> bool:
    return (
        _first_visible(
            page,
            ['input[type="password"]', 'input[name="password"]', 'input[autocomplete="current-password"]'],
            timeout=1500,
        )
        is not None
    )


def _otp_visible(page: Page) -> bool:
    if _first_visible(
        page,
        [
            'input[inputmode="numeric"]',
            'input[autocomplete="one-time-code"]',
            'input[name="code"]',
            'input[type="tel"]',
        ],
        timeout=1500,
    ):
        return True
    # 分格 6 位验证码（无 inputmode 时）
    loc = page.locator(
        'input[maxlength="1"][inputmode="numeric"], '
        'input[maxlength="1"][type="tel"], '
        'input[autocomplete="one-time-code"]'
    )
    try:
        count = loc.count()
        visible = sum(
            1 for i in range(min(count, 8)) if loc.nth(i).is_visible()
        )
        return visible >= 4
    except Exception:  # noqa: BLE001
        return False


def _email_visible(page: Page) -> bool:
    return (
        _first_visible(
            page,
            ['input[type="email"]', 'input[name="email"]', 'input[autocomplete="email"]'],
            timeout=1500,
        )
        is not None
    )


def _handle_login_email(page: Page, email: str) -> bool:
    if _is_login_password_page(page) or _password_visible(page):
        return False
    if not _is_login_email_page(page):
        return False
    if not _email_visible(page):
        log("log-in 页未找到邮箱输入框")
        return False
    log(f"log-in 页填写邮箱: {email}")
    _fill_and_go(
        page,
        email,
        ['input[type="email"]', 'input[name="email"]', 'input[autocomplete="email"]', 'input[id*="email"]'],
    )
    return True


def _wait_after_login_email(page: Page, timeout_sec: int = 15) -> None:
    """邮箱提交后等待下一页（URL 可能仍为 /log-in，需看 DOM）。"""

    def ready() -> bool:
        if _password_visible(page) or _is_login_password_page(page):
            return True
        if _is_email_verification_page(page) or _is_mfa_challenge_page(page):
            return True
        if _is_codex_consent_page(page) or _is_callback_url(page.url):
            return True
        return not _is_login_email_page(page)

    if _poll_until(ready, timeout_sec=timeout_sec):
        log("邮箱提交后已进入下一页")


def _wait_after_otp_submit(page: Page, timeout_sec: int = 20) -> bool:
    """验证码提交后等待离开 OTP 或进入授权页。"""

    def ready() -> bool:
        if _is_codex_consent_page(page) or _is_callback_url(page.url):
            return True
        return not _is_otp_code_page(page)

    return _poll_until(ready, timeout_sec=timeout_sec)


def _handle_login_or_create_email(page: Page, email: str) -> bool:
    if not _is_login_or_create_page(page):
        return False
    if not _email_visible(page):
        log("log-in-or-create-account 页未找到邮箱输入框")
        return False
    log(f"log-in-or-create-account 页填写邮箱: {email}")
    _fill_and_go(
        page,
        email,
        ['input[type="email"]', 'input[name="email"]', 'input[autocomplete="email"]', 'input[id*="email"]'],
    )
    return True


def _wait_leave_login_email(page: Page, timeout_sec: int = 25) -> None:
    if not _is_login_email_page(page):
        return
    if _poll_until(lambda: not _is_login_email_page(page), timeout_sec=timeout_sec):
        log("已离开 log-in 邮箱页")


def _wait_leave_login_or_create(page: Page, timeout_sec: int = 25) -> None:
    if not _is_login_or_create_page(page):
        return
    if _poll_until(lambda: not _is_login_or_create_page(page), timeout_sec=timeout_sec):
        log("已离开 log-in-or-create-account 页")


def _password_submit_ready(page: Page) -> bool:
    if _is_mfa_challenge_page(page) or _is_email_verification_page(page):
        return True
    if _is_codex_consent_page(page) or _is_callback_url(page.url):
        return True
    return not _is_login_password_page(page)


def _wait_after_password(page: Page, timeout_sec: int = 35) -> bool:
    """密码提交后等待跳转（优先 wait_for_url，避免轮询 + load_state 叠加卡顿）。"""
    timeout_ms = max(1000, int(timeout_sec * 1000))
    try:
        page.wait_for_url(POST_LOGIN_NAV_URL_RE, timeout=timeout_ms)
        log("密码提交后已进入下一页")
        return True
    except Exception:  # noqa: BLE001
        pass

    if _poll_until(_password_submit_ready, timeout_sec=min(8.0, timeout_sec), interval=0.3):
        log("密码提交后已进入下一页")
        return True
    return _password_submit_ready(page)


def _fill_password(page: Page, password: str) -> bool:
    if not _is_login_password_page(page):
        return False
    pwd = _first_visible(
        page,
        ['input[type="password"]', 'input[name="password"]', 'input[autocomplete="current-password"]'],
        timeout=3000,
    )
    if not pwd:
        return False
    log("log-in/password 页填写密码")
    _pause()
    _type_chars(pwd, password)
    _pause()
    submitted = False
    for loc in (
        page.get_by_role("button", name=re.compile(r"^(继续|Continue|Log in|登录|Submit|提交)$", re.I)),
        page.locator('button[type="submit"]'),
    ):
        try:
            btn = loc.first
            if btn.is_visible(timeout=1500) and btn.is_enabled():
                btn.click(timeout=5000)
                submitted = True
                break
        except Exception:  # noqa: BLE001
            pass
    if not submitted:
        if not _click_text(page, ["继续", "Continue", "Log in", "登录"], timeout=3000):
            page.keyboard.press("Enter")
    _pause(0.3)
    return True


def _fill_split_otp_inputs(page: Page, code: str) -> bool:
    """分格验证码（多个单字符输入框）。"""
    loc = page.locator(
        'input[inputmode="numeric"], input[type="tel"], input[autocomplete="one-time-code"], input[maxlength="1"]'
    )
    try:
        count = loc.count()
    except Exception:  # noqa: BLE001
        return False
    if count < 4:
        return False

    visible_idx: list[int] = []
    for i in range(min(count, 8)):
        try:
            if loc.nth(i).is_visible():
                visible_idx.append(i)
        except Exception:  # noqa: BLE001
            pass
    if len(visible_idx) < 4:
        return False

    log(f"分格验证码输入（{len(visible_idx)} 格）")
    for i, digit in enumerate(code):
        if i >= len(visible_idx):
            break
        box = loc.nth(visible_idx[i])
        box.click()
        box.press_sequentially(digit, delay=TYPE_CHAR_DELAY_MS)
    return True


def _fill_otp(page: Page, mailapi_url: str) -> bool:
    if _is_codex_consent_page(page) or _is_callback_url(page.url):
        log("验证码已完成，已进入授权/回调页")
        return True
    if not _is_otp_code_page(page):
        return False
    page_label = "mfa email-otp" if _is_mfa_email_otp_page(page) else "email-verification"
    log(f"{page_label} 页填写验证码")
    if not _otp_visible(page):
        if _is_codex_consent_page(page):
            log("验证码页已跳转至授权页")
            return True
        log("验证码页未找到输入框，等待…")
        return False

    log("轮询 mailapi 获取验证码…")
    code = fetch_email_code(mailapi_url)
    _pause()

    if _fill_split_otp_inputs(page, code):
        _pause()
        if _wait_after_otp_submit(page, timeout_sec=8):
            log("验证码输入后已自动进入下一页")
            return True
    else:
        otp_input = _first_visible(
            page,
            [
                'input[inputmode="numeric"]',
                'input[autocomplete="one-time-code"]',
                'input[name="code"]',
                'input[type="tel"]',
                'input[type="text"]',
            ],
            timeout=5000,
        )
        if not otp_input:
            if _is_codex_consent_page(page):
                return True
            log("未找到验证码输入框")
            return False
        _type_chars(otp_input, code)
        _pause(1.5)
        if _wait_after_otp_submit(page, timeout_sec=6):
            log("验证码输入后已自动进入下一页")
            return True

    if _is_codex_consent_page(page) or _is_callback_url(page.url):
        return True
    if not _is_otp_code_page(page):
        return True

    if not _click_otp_submit(page):
        log("未点到「继续」，尝试 Enter")
        page.keyboard.press("Enter")
        _pause()
    _pause(1.5)

    if _is_codex_consent_page(page) or _is_callback_url(page.url):
        log("已进入授权/回调页")
        return True

    if _is_otp_code_page(page):
        _check_otp_error_or_raise(page)

    if _wait_after_otp_submit(page, timeout_sec=20):
        return True

    if _is_otp_code_page(page):
        _check_otp_error_or_raise(page)
        log("提交后仍在验证码页")
    return True


def _wait_mfa_email_otp_page(page: Page, timeout_sec: float = 30) -> bool:
    """等待跳转到 mfa-challenge/email-otp（wait_for_url + 轮询双保险）。"""
    if _is_mfa_email_otp_page(page):
        log("已进入 MFA 邮箱验证码页 email-otp")
        return True

    deadline = time.time() + timeout_sec
    remain_ms = max(1000, int((deadline - time.time()) * 1000))
    try:
        page.wait_for_url(re.compile(r"mfa-challenge/email-otp", re.I), timeout=min(remain_ms, 25000))
        log("已进入 MFA 邮箱验证码页 email-otp")
        return True
    except Exception:  # noqa: BLE001
        pass

    if _poll_until(lambda: _is_mfa_email_otp_page(page), timeout_sec=max(1, deadline - time.time())):
        log("已进入 MFA 邮箱验证码页 email-otp")
        return True

    log(f"未能进入 mfa-challenge/email-otp，当前 URL: {page.url.split('?', 1)[0]}")
    return False


def _advance_mfa_to_email_otp(page: Page) -> bool:
    """
    mfa-challenge 根路径：尝试其他方法 → 选电子邮件 → 等待 email-otp。
    任一步失败返回 False（调用方标记账号废弃）。
    """
    if _is_mfa_email_otp_page(page):
        return True
    if not _is_mfa_challenge_root(page):
        return False

    log(f"MFA 页，点击「尝试其他方法」 ({page.url.split('?', 1)[0]})")
    if not _click_text(page, BTN_MFA_TRY_OTHER, timeout=8000):
        log("未找到「尝试其他方法」")
        return False
    _pause(2)

    if _is_mfa_email_otp_page(page):
        return True
    if not _is_mfa_challenge_root(page):
        log(f"MFA 中间页非预期: {page.url.split('?', 1)[0]}")
        return False

    log("MFA 页，选择「电子邮件」")
    if not _click_text(page, BTN_MFA_EMAIL, timeout=8000):
        log("未找到「电子邮件」验证方式")
        return False
    _pause(1)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:  # noqa: BLE001
        pass

    return _wait_mfa_email_otp_page(page, timeout_sec=30)


def _process_otp_step(page: Page, mailapi_url: str, otp_submitted: bool) -> bool:
    """处理验证码页（email-verification / mfa email-otp），返回是否已提交过验证码。"""
    if _is_codex_consent_page(page) or _is_callback_url(page.url):
        return False
    if otp_submitted:
        if _is_codex_consent_page(page) or not _is_otp_code_page(page):
            return False
        log("验证码已填，重试点击「继续」…")
        _click_otp_submit(page)
        _pause(1.5)
        if _wait_after_otp_submit(page, timeout_sec=15):
            return False
        if _is_otp_code_page(page):
            _check_otp_error_or_raise(page)
        return True
    if _fill_otp(page, mailapi_url):
        if not _is_otp_code_page(page) or _is_codex_consent_page(page):
            return False
        return True
    return otp_submitted


def _advance_login_flow(
    page: Page,
    email: str,
    password: str,
    mailapi_url: str,
    *,
    auth_path: Path | None = None,
    timeout_sec: int = 180,
) -> None:
    """
    严格按 URL 执行操作，避免在错误页面点击「继续」：

    log-in-or-create-account / log-in → 填邮箱
    choose-an-account → 登录至另一个账户
    log-in/password → 填密码
    email-verification / mfa email-otp → 填验证码 + 继续
    mfa-challenge → 尝试其他方法 → 电子邮件 → email-otp
    codex/consent → 授权继续
    """
    deadline = time.time() + timeout_sec
    otp_submitted = False
    login_email_done = False
    password_done = False
    choose_account_tries = 0
    last_kind = ""

    while time.time() < deadline:
        _check_shutdown()
        if auth_path is not None and _auth_file_ready(auth_path):
            log("codex login 已写入 auth.json，结束浏览器登录流程")
            return
        # 密码/MFA 跳转期间避免每轮 wait_for_load_state(5s) 反复超时
        if not (password_done and _is_login_password_page(page)):
            try:
                page.wait_for_load_state("domcontentloaded", timeout=800)
            except Exception:  # noqa: BLE001
                pass

        kind = _login_page_kind(page)
        if kind != last_kind:
            _log_current_page(page, kind)
            last_kind = kind

        if kind == "callback":
            log("已进入 OAuth 回调")
            return

        if kind == "mfa_challenge":
            otp_submitted = False
            if not _advance_mfa_to_email_otp(page):
                raise MfaChallengeError(f"MFA 无法切换至邮箱验证: {page.url.split('?', 1)[0]}")
            _pause(2)
            continue

        if kind == "login_or_create":
            otp_submitted = False
            if _handle_login_or_create_email(page, email):
                login_email_done = True
                _wait_after_login_email(page)
            _pause(1)
            continue

        if kind == "choose_account":
            otp_submitted = False
            choose_account_tries += 1
            if choose_account_tries > 6:
                raise RuntimeError(
                    "choose-an-account 页多次未能点击「登录至另一个账户」，请检查页面文案或手动登录"
                )
            if _handle_choose_account(page):
                choose_account_tries = 0
                _wait_known_auth_page(page, timeout_sec=15)
            _pause(2)
            continue

        if kind == "login":
            otp_submitted = False
            if login_email_done or _password_visible(page) or _is_login_password_page(page):
                _pause(1)
                continue
            if _handle_login_email(page, email):
                login_email_done = True
                _wait_after_login_email(page)
            _pause(1)
            continue

        if kind == "password":
            otp_submitted = False
            login_email_done = True
            if password_done:
                if _is_login_password_page(page):
                    _wait_after_password(page, timeout_sec=35)
                continue
            if _fill_password(page, password):
                password_done = True
                _wait_after_password(page, timeout_sec=35)
            continue

        if kind in ("email_verification", "mfa_email_otp"):
            if _is_codex_consent_page(page):
                _pause(1)
                continue
            otp_submitted = _process_otp_step(page, mailapi_url, otp_submitted)
            _pause(1)
            continue

        if kind == "consent":
            otp_submitted = False
            if auth_path is not None and _auth_file_ready(auth_path):
                log("codex login 已写入 auth.json，跳过授权页")
                return
            if _click_codex_consent_continue(page):
                if _wait_oauth_complete_or_leave_consent(page, auth_path, timeout_sec=45):
                    if auth_path is not None and _auth_file_ready(auth_path):
                        log("codex login 回调已完成，结束浏览器登录流程")
                        return
                    if _is_callback_url(page.url):
                        log("浏览器已进入 OAuth 回调")
                        return
                    log("已离开 Codex 授权页")
                    continue
                log("授权后等待 OAuth 完成超时，重试…")
                _pause(2)
            else:
                if auth_path is not None and _auth_file_ready(auth_path):
                    log("codex login 已写入 auth.json，结束浏览器登录流程")
                    return
                log("codex/consent 授权页未点到「继续」，重试…")
                _pause(2)
            continue

        # OAuth 账户选择 / authorize 等中间页：先点「登录另一个账户」
        if _is_mfa_challenge_page(page):
            if _is_mfa_email_otp_page(page):
                otp_submitted = _process_otp_step(page, mailapi_url, otp_submitted)
            else:
                otp_submitted = False
                if not _advance_mfa_to_email_otp(page):
                    raise MfaChallengeError(
                        f"MFA 无法切换至邮箱验证: {page.url.split('?', 1)[0]}"
                    )
            _pause(2)
            continue

        if _try_another_account(page):
            _wait_known_auth_page(page, timeout_sec=15)
            continue

        log(f"等待已知登录页… {page.url[:90]}")
        _wait_known_auth_page(page, timeout_sec=10)
        _pause(1)

    if auth_path is not None and _auth_file_ready(auth_path):
        log("codex login 已写入 auth.json")
        return
    if _poll_until(lambda: _is_callback_url(page.url), timeout_sec=60):
        log("OAuth 回调已完成")
    else:
        log("等待 OAuth 回调超时，继续检查 auth.json")


def _system_chrome_user_data() -> Path:
    return MAC_CHROME_USER_DATA


def _reset_chrome_profile_if_needed(chrome_profile: Path) -> None:
    """每次启动前清空项目内 .chrome-profile，避免上次登录账号残留。"""
    if chrome_profile.name != ".chrome-profile":
        return
    if not chrome_profile.exists():
        return
    try:
        shutil.rmtree(chrome_profile)
        log(f"已清除 Chrome 配置: {chrome_profile}")
    except OSError as exc:
        log(f"清除 Chrome 配置失败: {exc}")


def _launch_browser_context(
    p: Any,
    *,
    chrome_profile: Path,
    use_system_chrome: bool,
    headless: bool,
    cdp_url: str | None,
) -> tuple[Any | None, Any, bool]:
    """
    返回 (browser, context, owns_browser)。
    persistent / CDP 模式下 owns_browser 表示脚本负责 close。
    """
    anti_detect_args = [
        "--disable-blink-features=AutomationControlled",
    ]
    common = {
        "headless": headless,
        "ignore_default_args": ["--enable-automation"],
        "args": anti_detect_args,
    }

    if cdp_url:
        log(f"连接已有 Chrome (CDP): {cdp_url}")
        browser = p.chromium.connect_over_cdp(cdp_url)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        return browser, context, False

    _reset_chrome_profile_if_needed(chrome_profile)
    chrome_profile.mkdir(parents=True, exist_ok=True)
    channel = "chrome" if use_system_chrome else None
    log(
        f"启动 Chrome 持久配置: {chrome_profile}"
        + (f" (channel=chrome)" if channel else " (内置 Chromium)")
    )
    kwargs = {**common, "user_data_dir": str(chrome_profile)}
    if channel:
        kwargs["channel"] = channel
    context = p.chromium.launch_persistent_context(**kwargs)
    return None, context, True


def _open_oauth_url(page: Page, oauth_url: str) -> None:
    """打开 OAuth 授权页；commit 优先，超时后若已在 auth.openai.com 则继续。"""
    last_err: Exception | None = None
    for wait_until, timeout_ms in (("commit", 45000), ("domcontentloaded", 60000)):
        try:
            page.goto(oauth_url, wait_until=wait_until, timeout=timeout_ms)
            return
        except PlaywrightError as exc:
            last_err = exc
            cur = page.url or ""
            if "auth.openai.com" in cur:
                log(f"OAuth 导航 {wait_until} 超时，但页面已打开: {cur.split('?', 1)[0]}")
                return
    if last_err is not None:
        raise last_err


def browser_oauth_login(
    oauth_url: str,
    email: str,
    password: str,
    mailapi_url: str,
    *,
    auth_path: Path | None = None,
    headless: bool = False,
    chrome_profile: Path = DEFAULT_CHROME_PROFILE,
    use_system_chrome: bool = True,
    cdp_url: str | None = None,
) -> None:
    if _shutdown_requested.is_set():
        _check_shutdown()
    try:
        with sync_playwright() as p:
            browser, context, owns = _launch_browser_context(
                p,
                chrome_profile=chrome_profile,
                use_system_chrome=use_system_chrome,
                headless=headless,
                cdp_url=cdp_url,
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(10000)

            try:
                log(f"打开 OAuth: {oauth_url[:80]}…")
                _open_oauth_url(page, oauth_url)
                _pause()

                if _is_mfa_challenge_page(page):
                    raise MfaChallengeError(f"遇到 MFA 验证页: {page.url.split('?', 1)[0]}")

                if not _try_another_account(page, timeout=10000):
                    log("未找到「另一个账户」按钮，可能已在登录页")

                _advance_login_flow(page, email, password, mailapi_url, auth_path=auth_path)
                _pause()
            except KeyboardInterrupt:
                raise
            except PlaywrightError:
                if _shutdown_requested.is_set():
                    raise KeyboardInterrupt from None
                raise
            finally:
                if owns and not _shutdown_requested.is_set():
                    try:
                        context.close()
                    except PlaywrightError:
                        pass
    except KeyboardInterrupt:
        raise


class RefreshTokenError(Exception):
    """refresh_token 刷新失败；permanent=True 表示需重新登录。"""

    def __init__(self, message: str, *, permanent: bool = False, code: str | None = None) -> None:
        super().__init__(message)
        self.permanent = permanent
        self.code = code


def _extract_refresh_error_code(body: str) -> str | None:
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    err = data.get("error")
    if isinstance(err, dict):
        code = err.get("code")
        return str(code) if code else None
    if isinstance(err, str):
        return err
    code = data.get("code")
    return str(code) if code else None


def load_refresh_token_for_account(
    account: dict[str, Any],
    proxy_dir: Path = CLI_PROXY_DIR,
) -> str | None:
    """优先 ~/.cli-proxy-api/<邮箱>.json，其次 accounts 中的 refresh_token。"""
    email = str(account.get("email", "")).strip()
    if email:
        proxy_path = proxy_dir / f"{email}.json"
        if proxy_path.exists():
            try:
                data = _load_json_file(proxy_path)
                rt = str(data.get("refresh_token", "")).strip()
                if rt:
                    return rt
            except json.JSONDecodeError as exc:
                log(f"读取 {proxy_path.name} 失败: {exc}")

    rt = str(account.get("refresh_token", "")).strip()
    return rt or None


def request_token_refresh(refresh_token: str, *, timeout: int = 30) -> dict[str, Any]:
    """POST https://auth.openai.com/oauth/token 用 refresh_token 换取新 token。"""
    resp = requests.post(
        OAUTH_TOKEN_URL,
        json={
            "client_id": OAUTH_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        headers={"Content-Type": "application/json"},
        timeout=timeout,
    )
    if resp.status_code == 200:
        data = resp.json()
        if not data.get("access_token"):
            raise RefreshTokenError("响应缺少 access_token", permanent=True)
        return data

    body = resp.text or ""
    code = _extract_refresh_error_code(body)
    permanent = resp.status_code == 401
    detail = body[:240] if body else resp.reason
    raise RefreshTokenError(
        f"HTTP {resp.status_code}: {detail}",
        permanent=permanent,
        code=code,
    )


def merge_refresh_response_into_account(
    account: dict[str, Any],
    tokens: dict[str, Any],
) -> dict[str, Any]:
    if tokens.get("access_token"):
        account["access_token"] = tokens["access_token"]
    if tokens.get("refresh_token"):
        account["refresh_token"] = tokens["refresh_token"]
    if tokens.get("id_token"):
        account["id_token"] = tokens["id_token"]
    account["last_token_refresh"] = datetime.now(timezone.utc).isoformat()
    account["auth_mode"] = account.get("auth_mode") or "chatgpt"
    return account


def try_refresh_via_refresh_token(
    account: dict[str, Any],
    *,
    proxy_dir: Path = CLI_PROXY_DIR,
) -> dict[str, Any] | None:
    """用 refresh_token 刷新；无 token 或失败返回 None。"""
    email = str(account.get("email", ""))
    refresh_token = load_refresh_token_for_account(account, proxy_dir=proxy_dir)
    if not refresh_token:
        log(f"{email}: 无 refresh_token，跳过 API 刷新")
        return None

    log(f"{email}: 尝试 refresh_token API 刷新…")
    try:
        tokens = request_token_refresh(refresh_token)
    except RefreshTokenError as exc:
        hint = f" ({exc.code})" if exc.code else ""
        log(f"{email}: refresh_token 刷新失败{hint}: {exc}")
        return None
    except requests.RequestException as exc:
        log(f"{email}: refresh_token 请求异常: {exc}")
        return None

    log(f"{email}: refresh_token API 刷新成功")
    return merge_refresh_response_into_account(account, tokens)


def merge_auth_into_account(account: dict[str, Any], auth: dict[str, Any]) -> dict[str, Any]:
    tokens = auth.get("tokens") or {}
    for src, dst in (
        ("access_token", "access_token"),
        ("id_token", "id_token"),
        ("refresh_token", "refresh_token"),
        ("account_id", "chatgpt_account_id"),
    ):
        if tokens.get(src):
            account[dst] = tokens[src]
    if auth.get("OPENAI_API_KEY"):
        account["openai_api_key"] = auth["OPENAI_API_KEY"]
    account["last_token_refresh"] = datetime.now(timezone.utc).isoformat()
    account["auth_mode"] = auth.get("auth_mode", "chatgpt")
    return account


def save_accounts(path: Path, rows: list[dict[str, Any]]) -> None:
    from accounts_io import write_accounts_jsonl

    write_accounts_jsonl(path, rows)


def _now_cn_iso() -> str:
    return datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S +0800")


def _jwt_exp_cn_iso(token: str) -> str | None:
    try:
        data = _jwt_payload(token)
        exp = data.get("exp")
        if exp:
            return datetime.fromtimestamp(int(exp), TZ_CN).strftime("%Y-%m-%d %H:%M:%S +0800")
    except Exception:  # noqa: BLE001
        pass
    return None


def _load_json_file(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    return json.loads(text)


def sync_cli_proxy_token(
    account: dict[str, Any],
    proxy_dir: Path = CLI_PROXY_DIR,
) -> Path:
    """刷新后同步 ~/.cli-proxy-api/<邮箱>.json 原件 token。"""
    email = str(account.get("email", "")).strip()
    if not email:
        raise ValueError("账号缺少 email")

    proxy_dir.mkdir(parents=True, exist_ok=True)
    path = proxy_dir / f"{email}.json"

    existing: dict[str, Any] = {}
    if path.exists():
        try:
            existing = _load_json_file(path)
        except json.JSONDecodeError as exc:
            log(f"原件 JSON 解析失败，将覆盖写入: {path} ({exc})")

    access = account.get("access_token") or ""
    refresh = account.get("refresh_token") or ""
    id_token = account.get("id_token") or ""
    account_id = account.get("chatgpt_account_id") or account.get("account_id") or existing.get("account_id")

    payload: dict[str, Any] = dict(existing)
    payload.update(
        {
            "email": email,
            "access_token": access,
            "refresh_token": refresh,
            "id_token": id_token,
            "account_id": account_id,
            "last_refresh": _now_cn_iso(),
            "type": existing.get("type") or "codex",
            "disabled": existing.get("disabled", False),
        }
    )
    if access:
        exp = _jwt_exp_cn_iso(access)
        if exp:
            payload["expired"] = exp

    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(path)
    log(f"已同步原件 -> {path}")
    return path


def persist_refresh_success(
    updated: dict[str, Any],
    *,
    email: str,
    accounts_path: Path,
    rows: list[dict[str, Any]],
    need_email_path: Path,
    accounts_dir: Path = DEFAULT_ACCOUNTS_DIR,
    cli_proxy_dir: Path = CLI_PROXY_DIR,
    sync_cli_proxy: bool = True,
) -> None:
    """refresh_token / 浏览器登录成功后统一写回：accounts、单账号文件、cli-proxy、need_email。"""
    save_accounts(accounts_path, rows)
    single_path = save_account_single(updated, accounts_dir=accounts_dir)
    log(f"已更新单账号文件 -> {single_path.name}")
    if sync_cli_proxy:
        sync_cli_proxy_token(updated, proxy_dir=cli_proxy_dir)
    mark_refreshed_in_need_emails(need_email_path, email)


def mark_cli_proxy_disabled(
    email: str,
    proxy_dir: Path = CLI_PROXY_DIR,
) -> None:
    """cli-proxy 原件标记 disabled=true。"""
    path = proxy_dir / f"{email.strip()}.json"
    if not path.exists():
        log(f"cli-proxy 原件不存在，跳过 disabled: {path.name}")
        return
    try:
        data = _load_json_file(path)
    except json.JSONDecodeError as exc:
        log(f"cli-proxy 原件解析失败，跳过 disabled: {path.name} ({exc})")
        return
    data["disabled"] = True
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(path)
    log(f"已标记 cli-proxy disabled=true -> {path.name}")


def persist_account_abandoned(
    email: str,
    *,
    need_email_path: Path,
    cli_proxy_dir: Path = CLI_PROXY_DIR,
    reason: str = "MFA",
    sync_cli_proxy: bool = True,
) -> None:
    """MFA 等不可恢复情况：标记 need_email 已废弃 + cli-proxy disabled。"""
    mark_abandoned_in_need_emails(need_email_path, email, reason=reason)
    if sync_cli_proxy:
        mark_cli_proxy_disabled(email, proxy_dir=cli_proxy_dir)


def persist_account_abnormal(
    email: str,
    *,
    need_email_path: Path,
    reason: str = "验证码错误",
) -> None:
    """验证码错误等异常：仅标记 need_email 异常。"""
    mark_abnormal_in_need_emails(need_email_path, email, reason=reason)


def save_account_single(account: dict[str, Any], accounts_dir: Path = DEFAULT_ACCOUNTS_DIR) -> Path:
    """同步更新 accounts/<邮箱>.json 单账号文件。"""
    from split_accounts import email_to_filename

    email = str(account.get("email", ""))
    if not email:
        raise ValueError("账号缺少 email")
    accounts_dir.mkdir(parents=True, exist_ok=True)
    path = accounts_dir / email_to_filename(email)
    with path.open("w", encoding="utf-8") as f:
        json.dump(account, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return path


def run_codex_logout() -> None:
    subprocess.run(["codex", "logout"], check=False, capture_output=True, text=True)


class CodexEnvBackup:
    """备份并还原 ~/.codex/auth.json 与 config.toml（退出/中断时也会还原）。"""

    def __init__(self, auth_path: Path = AUTH_PATH, config_path: Path = CONFIG_PATH) -> None:
        self.auth_path = auth_path
        self.config_path = config_path
        self.backup_dir = CODEX_BACKUP_DIR
        self._had_auth = False
        self._had_config = False
        self._restored = False

    def backup(self) -> None:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        if self.auth_path.exists():
            shutil.copy2(self.auth_path, self.backup_dir / "auth.json")
            self._had_auth = True
            log(f"已备份 {self.auth_path}")
        else:
            log(f"跳过备份（不存在）: {self.auth_path}")
        if self.config_path.exists():
            shutil.copy2(self.config_path, self.backup_dir / "config.toml")
            self._had_config = True
            log(f"已备份 {self.config_path}")
        else:
            log(f"跳过备份（不存在）: {self.config_path}")

    def restore(self) -> None:
        if self._restored:
            return
        self._restored = True
        try:
            if self._had_auth:
                shutil.copy2(self.backup_dir / "auth.json", self.auth_path)
                log(f"已还原 {self.auth_path}")
            elif self.auth_path.exists():
                self.auth_path.unlink()
                log(f"已删除刷新产生的 {self.auth_path}")
            if self._had_config:
                shutil.copy2(self.backup_dir / "config.toml", self.config_path)
                log(f"已还原 {self.config_path}")
            elif self.config_path.exists():
                self.config_path.unlink()
                log(f"已删除刷新产生的 {self.config_path}")
        except Exception as exc:  # noqa: BLE001
            log(f"还原 ~/.codex 配置失败: {exc}")

    def __enter__(self) -> CodexEnvBackup:
        self.backup()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.restore()


_active_codex_backup: CodexEnvBackup | None = None


def _install_codex_restore_hooks(backup: CodexEnvBackup) -> None:
    global _active_codex_backup, _sigint_count
    _active_codex_backup = backup
    _sigint_count = 0
    _shutdown_requested.clear()

    def _on_exit() -> None:
        if not _shutdown_requested.is_set():
            backup.restore()

    def _on_signal(signum: int, _frame: Any) -> None:
        _handle_interrupt(signum)

    atexit.register(_on_exit)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            pass


def refresh_one_via_browser(
    account: dict[str, Any],
    *,
    auth_path: Path,
    headless: bool,
    chrome_profile: Path,
    use_system_chrome: bool,
    cdp_url: str | None,
) -> dict[str, Any]:
    email = account["email"]
    password = account.get("password") or ""
    mailapi_url = get_mailapi_url(account)
    if not mailapi_url:
        raise ValueError(f"{email}: 缺少 mailapi_url")
    if not password:
        raise ValueError(f"{email}: 缺少 password")

    log(f"{email}: 使用浏览器模拟登录刷新…")
    run_codex_logout()
    if auth_path.exists():
        auth_path.unlink()

    login = CodexLoginProcess()
    global _active_login
    _active_login = login
    oauth_url = login.start()

    try:
        browser_oauth_login(
            oauth_url,
            email,
            password,
            mailapi_url,
            auth_path=auth_path,
            headless=headless,
            chrome_profile=chrome_profile,
            use_system_chrome=use_system_chrome,
            cdp_url=cdp_url,
        )
        auth = wait_for_auth(auth_path, login)
    except KeyboardInterrupt:
        _handle_interrupt()
    finally:
        _active_login = None
        if _shutdown_requested.is_set():
            login.terminate()
        else:
            login.wait_done(timeout=60)

    return merge_auth_into_account(account, auth)


def refresh_one(
    account: dict[str, Any],
    *,
    auth_path: Path,
    headless: bool,
    chrome_profile: Path,
    use_system_chrome: bool,
    cdp_url: str | None,
    proxy_dir: Path = CLI_PROXY_DIR,
    force_browser: bool = False,
) -> dict[str, Any]:
    if not force_browser:
        refreshed = try_refresh_via_refresh_token(account, proxy_dir=proxy_dir)
        if refreshed is not None:
            return refreshed

    return refresh_one_via_browser(
        account,
        auth_path=auth_path,
        headless=headless,
        chrome_profile=chrome_profile,
        use_system_chrome=use_system_chrome,
        cdp_url=cdp_url,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="全自动刷新 Codex token")
    parser.add_argument("--accounts", type=Path, default=DEFAULT_ACCOUNTS)
    parser.add_argument("--need-email", type=Path, default=DEFAULT_NEED_EMAIL)
    parser.add_argument("--auth-file", type=Path, default=AUTH_PATH)
    parser.add_argument("--email", action="append")
    parser.add_argument("--headless", action="store_true", default=False,
                        help="无头模式（更容易触发风控，不推荐）")
    parser.add_argument(
        "--chrome-profile",
        type=Path,
        default=DEFAULT_CHROME_PROFILE,
        help=f"Chrome 用户数据目录（持久 Cookie），默认 {DEFAULT_CHROME_PROFILE.name}/",
    )
    parser.add_argument(
        "--use-main-chrome-profile",
        action="store_true",
        help="使用本机 Chrome 主配置目录（需先完全退出 Chrome）",
    )
    parser.add_argument(
        "--no-system-chrome",
        action="store_true",
        help="不用本机 Google Chrome，改用 Playwright 自带 Chromium（易触发检测）",
    )
    parser.add_argument(
        "--cdp",
        type=str,
        default=os.environ.get("CHROME_CDP_URL", ""),
        help="连接已打开的 Chrome，如 http://127.0.0.1:9222（需先用 --remote-debugging-port 启动）",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--cli-proxy-dir",
        type=Path,
        default=CLI_PROXY_DIR,
        help="cli-proxy-api 原件目录，默认 ~/.cli-proxy-api",
    )
    parser.add_argument(
        "--no-cli-proxy",
        action="store_true",
        help="不同步 ~/.cli-proxy-api/<邮箱>.json",
    )
    parser.add_argument(
        "--force-browser",
        action="store_true",
        help="跳过 refresh_token API，强制浏览器登录",
    )
    args = parser.parse_args()

    chrome_profile = _system_chrome_user_data() if args.use_main_chrome_profile else args.chrome_profile
    use_system_chrome = not args.no_system_chrome
    cdp_url = args.cdp.strip() or None
    if args.use_main_chrome_profile:
        log("使用本机 Chrome 主配置 — 请先完全退出 Chrome，否则会因配置锁失败")
    if args.headless:
        log("警告: 无头模式更容易被 OpenAI 识别为自动化")

    rows, index = load_accounts(args.accounts)
    targets = args.email or load_need_emails(args.need_email)
    if not targets:
        log("need_email.txt 为空")
        return 1

    if not args.dry_run:
        backup = args.accounts.with_suffix(".json.bak")
        shutil.copy2(args.accounts, backup)
        log(f"备份 -> {backup}")

    codex_backup = CodexEnvBackup(
        auth_path=args.auth_file.expanduser(),
        config_path=CONFIG_PATH,
    )
    _install_codex_restore_hooks(codex_backup)

    ok = fail = abandoned = abnormal = 0
    try:
        with codex_backup:
            for idx, raw in enumerate(targets):
                if _shutdown_requested.is_set():
                    break
                key = raw.lower()
                if key not in index:
                    log(f"跳过 {raw}: 不在 accounts.json")
                    fail += 1
                    continue
                acc = rows[index[key]]
                if is_skipped_refreshed(args.need_email, acc["email"]):
                    log(f"跳过 {acc['email']}: need_email.txt 已标记 {REFRESHED_MARK}")
                    continue
                if is_skipped_abandoned(args.need_email, acc["email"]):
                    log(f"跳过 {acc['email']}: need_email.txt 已标记 {ABANDONED_MARK}")
                    continue
                if is_skipped_abnormal(args.need_email, acc["email"]):
                    log(f"跳过 {acc['email']}: need_email.txt 已标记 {ABNORMAL_MARK}")
                    continue
                log(f"========== {acc['email']} ==========")
                try:
                    rows[index[key]] = refresh_one(
                        acc,
                        auth_path=args.auth_file,
                        headless=args.headless,
                        chrome_profile=chrome_profile,
                        use_system_chrome=use_system_chrome,
                        cdp_url=cdp_url,
                        proxy_dir=args.cli_proxy_dir,
                        force_browser=args.force_browser,
                    )
                    if not args.dry_run:
                        persist_refresh_success(
                            rows[index[key]],
                            email=acc["email"],
                            accounts_path=args.accounts,
                            rows=rows,
                            need_email_path=args.need_email,
                            cli_proxy_dir=args.cli_proxy_dir,
                            sync_cli_proxy=not args.no_cli_proxy,
                        )
                    log(f"成功 {acc['email']}")
                    ok += 1
                except MfaChallengeError as exc:
                    log(f"废弃 {acc['email']}: {exc}")
                    if not args.dry_run:
                        persist_account_abandoned(
                            acc["email"],
                            need_email_path=args.need_email,
                            cli_proxy_dir=args.cli_proxy_dir,
                            sync_cli_proxy=not args.no_cli_proxy,
                        )
                    abandoned += 1
                except AccountAbnormalError as exc:
                    log(f"异常 {acc['email']}: {exc}")
                    if not args.dry_run:
                        persist_account_abnormal(
                            acc["email"],
                            need_email_path=args.need_email,
                            reason="验证码错误",
                        )
                    abnormal += 1
                except KeyboardInterrupt:
                    _handle_interrupt()
                except Exception as exc:  # noqa: BLE001
                    log(f"失败 {raw}: {exc}")
                    fail += 1

                if _shutdown_requested.is_set():
                    break
                if idx < len(targets) - 1:
                    log(f"等待 {ACCOUNT_GAP_SEC}s 后处理下一个账号…")
                    _interruptible_sleep(ACCOUNT_GAP_SEC)

        if _shutdown_requested.is_set():
            log("用户中断")
            return 130

        log(f"完成: 成功 {ok} 废弃 {abandoned} 异常 {abnormal} 失败 {fail}")
        return 0 if fail == 0 else 2
    except KeyboardInterrupt:
        _handle_interrupt()


if __name__ == "__main__":
    raise SystemExit(main())