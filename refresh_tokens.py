#!/usr/bin/env python3
"""全自动刷新 Codex token：codex login 后台 + 浏览器登录 + 正则解析验证码 + 回写 accounts.json"""

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
from typing import Any
from urllib.parse import urlparse

import requests

try:
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
ACTION_DELAY_SEC = 1  # 浏览器操作之间的间隔（秒）
ACCOUNT_GAP_SEC = 30  # 每个账号刷新之间的间隔（秒）
TYPE_CHAR_DELAY_MS = 300  # 逐字符输入间隔（毫秒）

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
KNOWN_AUTH_PAGE_URL_RE = re.compile(
    r"auth\.openai\.com/(?:log-in(?:/password)?|email-verification)|"
    r"auth\.openai\.com/sign-in-with-chatgpt/codex/consent|"
    r"localhost:\d+",
    re.I,
)
BTN_ANOTHER_ACCOUNT = [
    "Use another account", "Sign in with a different account", "Log in with another account",
    "使用其他账号", "使用另一个账户", "登录其他帐户", "登录另一个账户", "使用其他帐户",
]
BTN_SUBMIT = ["Continue", "继续", "Log in", "登录", "Verify", "验证", "Submit", "提交", "Next", "下一步"]


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


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


def _refresh_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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
            if _is_marked_refreshed(line):
                continue
            email = _parse_need_email_line(line)
            if email:
                emails.append(email)
    return emails


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
        try:
            resp = requests.get(mailapi_url, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            time.sleep(interval)
            continue

        status = str(data.get("status", "")).lower()
        if status and status not in ("success", "ok", ""):
            last_error = f"status={status}"
            time.sleep(interval)
            continue

        code = extract_code_from_mailapi(data)
        if code:
            log(f"验证码: {code} (from={data.get('from', '?')})")
            return code

        time.sleep(interval)

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
                    log(f"已捕获 OAuth URL")

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
            if self.oauth_url:
                return self.oauth_url
            if self.proc.poll() is not None:
                break
            time.sleep(0.3)

        with self._lock:
            buf = "".join(self._output)
        raise RuntimeError(f"未能从 codex login 输出解析 OAuth URL:\n{buf}")

    def wait_done(self, timeout: int = 300) -> None:
        """等待 codex login 自然结束（回调完成），不主动 kill。"""
        if not self.proc:
            return
        try:
            self.proc.wait(timeout=timeout)
            log(f"codex login 进程已结束 (code={self.proc.returncode})")
        except subprocess.TimeoutExpired:
            log("codex login 仍在运行（回调可能已完成，继续检查 auth.json）")

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


def wait_for_auth(auth_path: Path, login: CodexLoginProcess, timeout: int = 300) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if auth_path.exists():
            try:
                data = json.loads(auth_path.read_text(encoding="utf-8"))
                tokens = data.get("tokens") or {}
                if tokens.get("access_token") or data.get("OPENAI_API_KEY"):
                    return data
                # chatgpt 模式：tokens 在顶层
                if data.get("auth_mode") == "chatgpt" and tokens:
                    return data
            except json.JSONDecodeError:
                pass
        time.sleep(2)

    raise TimeoutError(f"等待 {auth_path} 写入超时")


def _pause(sec: float = ACTION_DELAY_SEC) -> None:
    time.sleep(sec)


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


def _is_login_password_page(page: Page) -> bool:
    """https://auth.openai.com/log-in/password"""
    return bool(LOGIN_PASSWORD_URL_RE.search(page.url)) or _auth_path(page.url) == "/log-in/password"


def _login_page_kind(page: Page) -> str:
    """按 URL 判定当前登录阶段（不依赖 DOM 猜测）。"""
    if _is_callback_url(page.url):
        return "callback"
    if _is_codex_consent_page(page):
        return "consent"
    if _is_email_verification_page(page):
        return "email_verification"
    if _is_login_password_page(page):
        return "password"
    if _is_login_email_page(page):
        return "login"
    return "unknown"


def _log_current_page(page: Page, kind: str | None = None) -> None:
    kind = kind or _login_page_kind(page)
    short = page.url.split("?", 1)[0]
    log(f"当前页面[{kind}]: {short}")


def _wait_known_auth_page(page: Page, timeout_sec: int = 20) -> None:
    try:
        page.wait_for_url(KNOWN_AUTH_PAGE_URL_RE, timeout=timeout_sec * 1000)
    except Exception:  # noqa: BLE001
        pass


def _wait_email_verification_page(page: Page, timeout_sec: int = 30) -> bool:
    try:
        page.wait_for_url(EMAIL_VERIFICATION_URL_RE, timeout=timeout_sec * 1000)
        log("已进入邮箱验证码页 email-verification")
        return True
    except Exception:  # noqa: BLE001
        return _is_email_verification_page(page)


def _wait_codex_consent_page(page: Page, timeout_sec: int = 30) -> bool:
    try:
        page.wait_for_url(CODEX_CONSENT_URL_RE, timeout=timeout_sec * 1000)
        log("已进入 Codex 授权页")
        return True
    except Exception:  # noqa: BLE001
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
            if target.is_visible(timeout=3000):
                target.click(timeout=timeout)
                _pause()
                log("已点击「继续」")
                return True
        except Exception:  # noqa: BLE001
            pass

    return False


def _click_otp_submit(page: Page, timeout: int = 10000) -> bool:
    """邮箱验证码页提交：优先点击「继续」（仅 email-verification URL）。"""
    if not _is_email_verification_page(page):
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


def _wait_leave_email_verification(page: Page, timeout_sec: int = 20) -> bool:
    if not _is_email_verification_page(page):
        return True
    try:
        page.wait_for_function(
            "() => !window.location.href.includes('email-verification')",
            timeout=timeout_sec * 1000,
        )
        log("已离开邮箱验证码页")
        return True
    except Exception:  # noqa: BLE001
        return not _is_email_verification_page(page)


def _try_another_account(page: Page, *, timeout: int = 8000) -> bool:
    """OAuth/账户选择页：点击「登录另一个账户」进入 log-in。"""
    if (
        _is_login_email_page(page)
        or _is_login_password_page(page)
        or _is_email_verification_page(page)
        or _is_codex_consent_page(page)
        or _is_callback_url(page.url)
    ):
        return False

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


def _wait_leave_login_email(page: Page, timeout_sec: int = 25) -> None:
    if not _is_login_email_page(page):
        return
    try:
        page.wait_for_function(
            "() => new URL(location.href).pathname.replace(/\\/$/, '') !== '/log-in'",
            timeout=timeout_sec * 1000,
        )
        log("已离开 log-in 邮箱页")
    except Exception:  # noqa: BLE001
        pass


def _wait_after_password(page: Page, timeout_sec: int = 25) -> None:
    if not _is_login_password_page(page):
        return
    try:
        page.wait_for_function(
            "() => !new URL(location.href).pathname.replace(/\\/$/, '').endsWith('/log-in/password')",
            timeout=timeout_sec * 1000,
        )
        log("已离开 log-in/password 密码页")
    except Exception:  # noqa: BLE001
        pass


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
    if not _click_text(page, BTN_SUBMIT, timeout=5000):
        page.keyboard.press("Enter")
        _pause()
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:  # noqa: BLE001
        _pause(2)
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
    if not _is_email_verification_page(page):
        return False
    log("email-verification 页填写验证码")
    if not _otp_visible(page):
        log("验证码页未找到输入框，等待…")
        return False

    log("轮询 mailapi 获取验证码…")
    code = fetch_email_code(mailapi_url)
    _pause()

    if _fill_split_otp_inputs(page, code):
        _pause()
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
            log("未找到验证码输入框")
            return False
        _type_chars(otp_input, code)
        _pause(1.5)

    if not _click_otp_submit(page):
        log("未点到「继续」，尝试 Enter")
        page.keyboard.press("Enter")
        _pause()
    if not _wait_leave_email_verification(page, timeout_sec=20):
        log("提交后仍在验证码页")
    return True


def _advance_login_flow(
    page: Page,
    email: str,
    password: str,
    mailapi_url: str,
    *,
    timeout_sec: int = 180,
) -> None:
    """
    严格按 URL 执行操作，避免在错误页面点击「继续」：

    log-in → 填邮箱
    log-in/password → 填密码
    email-verification → 填验证码 + 继续
    codex/consent → 授权继续
    """
    deadline = time.time() + timeout_sec
    otp_submitted = False
    last_kind = ""

    while time.time() < deadline:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:  # noqa: BLE001
            pass

        kind = _login_page_kind(page)
        if kind != last_kind:
            _log_current_page(page, kind)
            last_kind = kind

        if kind == "callback":
            log("已进入 OAuth 回调")
            return

        if kind == "login":
            otp_submitted = False
            if _handle_login_email(page, email):
                _wait_leave_login_email(page)
                _wait_known_auth_page(page)
            _pause(2)
            continue

        if kind == "password":
            otp_submitted = False
            if _fill_password(page, password):
                _wait_after_password(page)
                _wait_known_auth_page(page)
            _pause(2)
            continue

        if kind == "email_verification":
            if otp_submitted:
                log("验证码已填，重试点击「继续」…")
                _click_otp_submit(page)
                _wait_leave_email_verification(page, timeout_sec=15)
                if not _is_email_verification_page(page):
                    otp_submitted = False
            elif _fill_otp(page, mailapi_url):
                otp_submitted = True
                if not _is_email_verification_page(page):
                    otp_submitted = False
            _pause(2)
            continue

        if kind == "consent":
            otp_submitted = False
            if _click_codex_consent_continue(page):
                _pause(3)
            else:
                log("codex/consent 授权页未点到「继续」，重试…")
                _pause(2)
            continue

        # OAuth 账户选择 / authorize 等中间页：先点「登录另一个账户」
        if _try_another_account(page):
            _wait_known_auth_page(page, timeout_sec=15)
            continue

        log(f"等待已知登录页… {page.url[:90]}")
        _wait_known_auth_page(page, timeout_sec=10)
        _pause(1)

    try:
        page.wait_for_url(re.compile(r"localhost:\d+"), timeout=60000)
        log("OAuth 回调已完成")
    except Exception:  # noqa: BLE001
        log("等待 OAuth 回调超时，继续检查 auth.json")


def _system_chrome_user_data() -> Path:
    return MAC_CHROME_USER_DATA


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


def browser_oauth_login(
    oauth_url: str,
    email: str,
    password: str,
    mailapi_url: str,
    *,
    headless: bool = False,
    chrome_profile: Path = DEFAULT_CHROME_PROFILE,
    use_system_chrome: bool = True,
    cdp_url: str | None = None,
) -> None:
    with sync_playwright() as p:
        browser, context, owns = _launch_browser_context(
            p,
            chrome_profile=chrome_profile,
            use_system_chrome=use_system_chrome,
            headless=headless,
            cdp_url=cdp_url,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(45000)

        log(f"打开 OAuth: {oauth_url[:80]}…")
        page.goto(oauth_url, wait_until="domcontentloaded")
        _pause()

        if not _try_another_account(page, timeout=10000):
            log("未找到「另一个账户」按钮，可能已在登录页")

        _advance_login_flow(page, email, password, mailapi_url)

        _pause()
        if owns:
            context.close()
        elif browser:
            pass  # CDP 模式不关闭用户自己的 Chrome


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
    return datetime.now(TZ_CN).strftime("%Y-%m-%dT%H:%M:%S%z").replace("+0800", "+08:00")


def _jwt_exp_cn_iso(token: str) -> str | None:
    try:
        payload = token.split(".")[1]
        padding = "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload + padding))
        exp = data.get("exp")
        if exp:
            return datetime.fromtimestamp(int(exp), TZ_CN).strftime("%Y-%m-%dT%H:%M:%S%z").replace(
                "+0800", "+08:00"
            )
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
    global _active_codex_backup
    _active_codex_backup = backup

    def _on_exit() -> None:
        backup.restore()

    def _on_signal(signum: int, _frame: Any) -> None:
        log(f"收到信号 {signum}，正在还原 ~/.codex …")
        backup.restore()
        raise SystemExit(128 + signum)

    atexit.register(_on_exit)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            pass


def refresh_one(
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

    run_codex_logout()
    if auth_path.exists():
        auth_path.unlink()

    login = CodexLoginProcess()
    oauth_url = login.start()

    try:
        browser_oauth_login(
            oauth_url,
            email,
            password,
            mailapi_url,
            headless=headless,
            chrome_profile=chrome_profile,
            use_system_chrome=use_system_chrome,
            cdp_url=cdp_url,
        )
        auth = wait_for_auth(auth_path, login)
    finally:
        login.wait_done(timeout=60)

    return merge_auth_into_account(account, auth)


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

    ok = fail = 0
    with codex_backup:
        for idx, raw in enumerate(targets):
            key = raw.lower()
            if key not in index:
                log(f"跳过 {raw}: 不在 accounts.json")
                fail += 1
                continue
            acc = rows[index[key]]
            if is_skipped_refreshed(args.need_email, acc["email"]):
                log(f"跳过 {acc['email']}: need_email.txt 已标记 {REFRESHED_MARK}")
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
                )
                if not args.dry_run:
                    updated = rows[index[key]]
                    save_accounts(args.accounts, rows)
                    single_path = save_account_single(updated)
                    log(f"已更新单账号文件 -> {single_path.name}")
                    if not args.no_cli_proxy:
                        sync_cli_proxy_token(updated, proxy_dir=args.cli_proxy_dir)
                    mark_refreshed_in_need_emails(args.need_email, acc["email"])
                log(f"成功 {acc['email']}")
                ok += 1
            except Exception as exc:  # noqa: BLE001
                log(f"失败 {raw}: {exc}")
                fail += 1

            if idx < len(targets) - 1:
                log(f"等待 {ACCOUNT_GAP_SEC}s 后处理下一个账号…")
                time.sleep(ACCOUNT_GAP_SEC)

    log(f"完成: 成功 {ok} 失败 {fail}")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
