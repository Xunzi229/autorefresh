#!/usr/bin/env python3
"""通过 ChatGPT 网页登录获取 token（无需 Codex OAuth 授权页）。

流程：打开 https://chatgpt.com/ → 点击左上角「登录」弹窗 → 填邮箱继续
→ 密码/验证码 → 读取 session API → 写回 accounts.json 等文件。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import Page, sync_playwright
except ImportError:
    print("请先: pip install -r requirements.txt && playwright install chromium", file=sys.stderr)
    raise

import refresh_tokens as rt

SCRIPT_DIR = Path(__file__).resolve().parent
CHATGPT_HOME_URL = "https://chatgpt.com/"
SESSION_URL = "https://chatgpt.com/api/auth/session"
LOGIN_BTN_TEXTS = ["Log in", "登录", "Sign in", "登入"]
EMAIL_SELECTORS = [
    'input[type="email"]',
    'input[name="email"]',
    'input[autocomplete="email"]',
    'input[id*="email"]',
]
PASSWORD_SELECTORS = [
    'input[type="password"]',
    'input[name="password"]',
    'input[autocomplete="current-password"]',
]
SUBMIT_BTN_TEXTS = ["Continue", "继续", "Verify", "验证", "Submit", "提交", "Next", "下一步"]
OAUTH_BTN_EXCLUDE_RE = re.compile(
    r"google|gmail|microsoft|apple|facebook|github|oauth|微信|wechat|sso|phone|手机",
    re.I,
)
FORM_CONTINUE_PATTERNS = [
    re.compile(r"^\s*Continue\s*$", re.I),
    re.compile(r"^\s*继续\s*$"),
    re.compile(r"^\s*Next\s*$", re.I),
    re.compile(r"^\s*下一步\s*$"),
]


def _is_chatgpt_logged_in_url(url: str) -> bool:
    u = (url or "").lower()
    if "auth.openai.com" in u:
        return False
    return "chatgpt.com" in u or "chat.openai.com" in u


def _is_chatgpt_homepage(page: Page) -> bool:
    u = (page.url or "").lower().split("?", 1)[0].rstrip("/")
    if "auth.openai.com" in u or "/api/" in u:
        return False
    if not ("chatgpt.com" in u or "chat.openai.com" in u):
        return False
    if re.search(r"chatgpt\.com/(c/|g/|chat)", u):
        return False
    return True


def _is_on_chatgpt_site(page: Page) -> bool:
    u = (page.url or "").lower()
    if "auth.openai.com" in u or "/api/" in u:
        return False
    return "chatgpt.com" in u or "chat.openai.com" in u


def _has_session_cookie(page: Page) -> bool:
    """登录过程中用 cookie 判断，避免轮询 session API。"""
    try:
        for c in page.context.cookies():
            name = c.get("name", "")
            if name == "__Secure-next-auth.session-token" or name.startswith(
                "__Secure-next-auth.session-token."
            ):
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _fetch_session_in_browser(page: Page) -> dict[str, Any]:
    """在页面同源上下文 fetch session，与浏览器共享 cookie。"""
    try:
        if not _is_on_chatgpt_site(page):
            rt._open_oauth_url(page, CHATGPT_HOME_URL)
            rt._pause(0)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:  # noqa: BLE001
                pass
        result = page.evaluate(
            """async () => {
            try {
                const r = await fetch('/api/auth/session', {
                    credentials: 'include',
                    headers: { Accept: 'application/json' },
                });
                if (!r.ok) return { __status: r.status };
                return await r.json();
            } catch (e) {
                return { __error: String(e) };
            }
        }"""
        )
        if isinstance(result, dict):
            if result.get("__status") or result.get("__error"):
                err = result.get("__status") or result.get("__error")
                rt.log(f"浏览器内 fetch session 失败: {err}")
                return {}
            return result
    except Exception as exc:  # noqa: BLE001
        rt.log(f"浏览器内 fetch session 异常: {exc}")
    return {}


def _auth_roots(page: Page) -> list[Any]:
    """登录弹窗/iframe 内 auth 表单可能所在的上下文。"""
    roots: list[Any] = []
    seen: set[int] = set()
    for frame in page.frames:
        url = (frame.url or "").lower()
        if "auth.openai.com" in url:
            fid = id(frame)
            if fid not in seen:
                seen.add(fid)
                roots.append(frame)
    if "auth.openai.com" in (page.url or "").lower():
        if id(page) not in seen:
            roots.insert(0, page)
    if not roots:
        roots.append(page)
    else:
        if page not in roots:
            roots.append(page)
    return roots


def _root_url(root: Any, page: Page) -> str:
    return (getattr(root, "url", None) or page.url or "").lower()


def _first_visible_in_roots(page: Page, selectors: list[str], *, timeout: int = 1500) -> tuple[Any, Any] | tuple[None, None]:
    for root in _auth_roots(page):
        el = rt._first_visible(root, selectors, timeout=timeout)
        if el is not None:
            return root, el
    return None, None


def _email_visible_any(page: Page) -> bool:
    return _first_visible_in_roots(page, EMAIL_SELECTORS)[1] is not None


def _password_visible_any(page: Page) -> bool:
    return _first_visible_in_roots(page, PASSWORD_SELECTORS)[1] is not None


def _is_oauth_button_label(label: str) -> bool:
    text = re.sub(r"\s+", " ", (label or "").strip())
    if not text:
        return False
    return bool(OAUTH_BTN_EXCLUDE_RE.search(text))


def _click_form_continue(root: Any, *, timeout: int = 8000) -> bool:
    """点击邮箱/密码表单「继续」，排除 Continue with Google 等 OAuth 按钮。"""
    for pat in FORM_CONTINUE_PATTERNS:
        try:
            loc = root.get_by_role("button", name=pat)
            count = loc.count()
        except Exception:  # noqa: BLE001
            continue
        for i in range(min(count, 8)):
            try:
                btn = loc.nth(i)
                if not btn.is_visible():
                    continue
                label = rt._control_label(btn)
                if _is_oauth_button_label(label):
                    continue
                btn.scroll_into_view_if_needed(timeout=3000)
                btn.click(timeout=timeout)
                rt._pause()
                rt.log(f"已点击表单继续: {label[:40]}")
                return True
            except Exception:  # noqa: BLE001
                continue

    try:
        loc = root.locator('button[type="submit"]')
        for i in range(min(loc.count(), 8)):
            btn = loc.nth(i)
            if not btn.is_visible():
                continue
            label = rt._control_label(btn)
            if _is_oauth_button_label(label):
                continue
            btn.click(timeout=timeout)
            rt._pause()
            rt.log(f"已点击 submit: {label[:40]}")
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _click_text_on_root(root: Any, texts: list[str], *, timeout: int = 8000) -> bool:
    for text in texts:
        pat = re.compile(rf"^\s*{re.escape(text)}\s*$", re.I)
        for role in ("button", "link"):
            loc = root.get_by_role(role, name=pat)
            try:
                count = loc.count()
            except Exception:  # noqa: BLE001
                continue
            for i in range(min(count, 8)):
                try:
                    el = loc.nth(i)
                    if not el.is_visible():
                        continue
                    label = rt._control_label(el)
                    if _is_oauth_button_label(label):
                        continue
                    el.click(timeout=timeout)
                    rt._pause()
                    return True
                except Exception:  # noqa: BLE001
                    pass
    return False


def _frame_login_kind(root: Any, page: Page) -> str:
    url = _root_url(root, page).split("?", 1)[0]
    if "choose-an-account" in url:
        return "choose_account"
    if "log-in/password" in url:
        return "password"
    if "log-in-or-create" in url:
        return "login_or_create"
    if "email-verification" in url:
        return "email_verification"
    if "mfa-challenge/email-otp" in url:
        return "mfa_email_otp"
    if re.search(r"/mfa-challenge(?:/|$)", url):
        return "mfa_challenge"
    if url.rstrip("/").endswith("/log-in"):
        return "login"
    return "unknown"


def _web_login_page_kind(page: Page) -> str:
    kind = rt._login_page_kind(page)
    if kind != "unknown":
        return kind
    best = "unknown"
    for root in _auth_roots(page):
        if root is page and "auth.openai.com" not in _root_url(root, page):
            continue
        fk = _frame_login_kind(root, page)
        if fk != "unknown":
            best = fk
    if best != "unknown":
        return best
    if _is_email_verification_any(page) or _otp_visible_any(page):
        if any("email-otp" in _root_url(r, page) for r in _auth_roots(page)) or rt._is_mfa_email_otp_page(page):
            return "mfa_email_otp"
        return "email_verification"
    if _password_visible_any(page):
        return "password"
    if _email_visible_any(page):
        return "login_or_create"
    if _is_chatgpt_homepage(page):
        return "chatgpt_home"
    return "unknown"


def _is_login_modal_open(page: Page) -> bool:
    if page.locator('[role="dialog"]').count() > 0:
        return True
    return _email_visible_any(page) or _password_visible_any(page)


def _find_chatgpt_login_button(page: Page) -> Any | None:
    selectors = [
        '[data-testid="login-button"]',
        '[data-testid="mobile-login-button"]',
        'a[href*="/auth/login"]',
        'button:has-text("Log in")',
        'button:has-text("登录")',
        'a:has-text("Log in")',
        'a:has-text("登录")',
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                return loc.first
        except Exception:  # noqa: BLE001
            pass
    for scope in (page.locator("header"), page.locator("nav"), page):
        for text in LOGIN_BTN_TEXTS:
            for role in ("button", "link"):
                try:
                    loc = scope.get_by_role(role, name=re.compile(re.escape(text), re.I))
                    if loc.count() > 0 and loc.first.is_visible():
                        return loc.first
                except Exception:  # noqa: BLE001
                    pass
            try:
                loc = scope.get_by_text(re.compile(f"^{re.escape(text)}$", re.I))
                if loc.count() > 0 and loc.first.is_visible():
                    return loc.first
            except Exception:  # noqa: BLE001
                pass
    return None


def _wait_chatgpt_login_button(page: Page, *, timeout_sec: float = 25) -> bool:
    return rt._poll_until(lambda: _find_chatgpt_login_button(page) is not None, timeout_sec=timeout_sec)


def _click_chatgpt_header_login(page: Page, *, timeout: int = 10000) -> bool:
    """点击 chatgpt.com「登录」按钮（打开弹窗，非跳转）。"""
    if not _is_on_chatgpt_site(page):
        rt.log(f"非 ChatGPT 页面，跳过点击登录: {(page.url or '')[:80]}")
        return False
    rt.log("ChatGPT 首页，等待并点击「登录」…")
    if not _wait_chatgpt_login_button(page, timeout_sec=25):
        rt.log("等待登录按钮超时")
        return False
    btn = _find_chatgpt_login_button(page)
    if btn is None:
        rt.log("未找到 ChatGPT 登录按钮")
        return False
    try:
        btn.scroll_into_view_if_needed(timeout=3000)
        btn.click(timeout=timeout)
        rt.log("已点击 ChatGPT 登录按钮")
        rt._pause(0)
        return True
    except Exception as exc:  # noqa: BLE001
        rt.log(f"点击登录按钮失败，尝试 force: {exc}")
        try:
            btn.click(timeout=timeout, force=True)
            rt.log("已 force 点击 ChatGPT 登录按钮")
            rt._pause(0)
            return True
        except Exception as exc2:  # noqa: BLE001
            rt.log(f"force 点击登录按钮仍失败: {exc2}")
            return False


def _wait_login_modal(page: Page, *, timeout_sec: float = 15) -> bool:
    return rt._poll_until(
        lambda: _is_login_modal_open(page) or _email_visible_any(page),
        timeout_sec=timeout_sec,
    )


def _open_chatgpt_login_modal(page: Page) -> bool:
    if _is_login_modal_open(page) or _email_visible_any(page):
        return True
    if not _click_chatgpt_header_login(page):
        return False
    return _wait_login_modal(page)


def _fill_email_any(page: Page, email: str) -> bool:
    root, el = _first_visible_in_roots(page, EMAIL_SELECTORS, timeout=3000)
    if el is None:
        rt.log("登录弹窗未找到邮箱输入框")
        return False
    rt.log(f"登录弹窗填写邮箱: {email}")
    rt._type_chars(el, email)
    rt._pause()
    # 优先 Enter 提交，避免误点「Continue with Google」
    el.press("Enter")
    rt._pause(0)
    if _email_visible_any(page):
        if not _click_form_continue(root):
            rt.log("Enter 未跳转，尝试精确点击「继续」")
            root.keyboard.press("Enter")
            rt._pause()
    return True


def _fill_password_any(page: Page, password: str) -> bool:
    root, pwd = _first_visible_in_roots(page, PASSWORD_SELECTORS, timeout=3000)
    if pwd is None:
        return False
    rt.log("登录弹窗填写密码")
    rt._pause()
    rt._type_chars(pwd, password)
    rt._pause()
    pwd.press("Enter")
    rt._pause(0)
    if _password_visible_any(page):
        if not _click_form_continue(root):
            root.keyboard.press("Enter")
    rt._pause(0)
    return True


def _is_email_verification_any(page: Page) -> bool:
    if rt._is_email_verification_page(page) or rt._is_mfa_email_otp_page(page):
        return True
    for root in _auth_roots(page):
        url = _root_url(root, page)
        if "email-verification" in url or "email-otp" in url:
            return True
    return False


def _otp_visible_any(page: Page) -> bool:
    for root in _auth_roots(page):
        if rt._otp_visible(root):
            return True
    return False


def _primary_otp_root(page: Page) -> Any | None:
    for root in _auth_roots(page):
        url = _root_url(root, page)
        if "email-verification" in url or "email-otp" in url:
            return root
    for root in _auth_roots(page):
        if rt._otp_visible(root):
            return root
    if rt._is_otp_code_page(page):
        return page
    return None


def _is_still_on_otp_page(page: Page, root: Any) -> bool:
    url = _root_url(root, page)
    if "email-verification" in url or "email-otp" in url:
        return rt._otp_visible(root) or "email-verification" in url or "email-otp" in url
    return rt._is_otp_code_page(page)


def _web_click_otp_submit(root: Any, page: Page, *, timeout: int = 10000) -> bool:
    """email-verification / mfa email-otp 页点击「继续」。"""
    url = _root_url(root, page)
    if not (
        "email-verification" in url
        or "email-otp" in url
        or rt._otp_visible(root)
        or rt._is_otp_code_page(root)
        or rt._is_otp_code_page(page)
    ):
        return False
    rt.log("email-verification 页，点击「继续」")
    rt._pause(0)
    for loc in (
        root.get_by_role("button", name="继续"),
        root.get_by_role("button", name=re.compile(r"^\s*继续\s*$")),
        root.locator('button:has-text("继续")'),
        root.get_by_role("button", name=re.compile(r"^\s*Continue\s*$", re.I)),
        root.locator('button:has-text("Continue")'),
        root.locator('button[type="submit"]'),
    ):
        try:
            btn = loc.first
            btn.wait_for(state="visible", timeout=5000)
            for _ in range(24):
                if btn.is_enabled():
                    break
                rt._pause(0)
            btn.click(timeout=timeout)
            rt._pause()
            rt.log("已点击 email-verification「继续」")
            return True
        except Exception:  # noqa: BLE001
            pass
    if _click_text_on_root(root, ["继续", "Continue", "Verify", "验证", "Submit", "提交"], timeout=timeout):
        rt.log("已点击 email-verification「继续」")
        return True
    return rt._click_otp_submit(root)


def _web_wait_after_otp_submit(page: Page, root: Any, *, timeout_sec: float = 20) -> bool:
    def ready() -> bool:
        if _has_session_cookie(page):
            return True
        if not _is_still_on_otp_page(page, root):
            return True
        return False

    return rt._poll_until(ready, timeout_sec=timeout_sec)


def _web_fill_otp(page: Page, mailapi_url: str, *, refetch: bool = False) -> bool:
    """https://auth.openai.com/email-verification 获取验证码、输入并提交。"""
    root = _primary_otp_root(page)
    if root is None:
        rt.log("等待进入 email-verification 页…")
        if not rt._poll_until(
            lambda: _primary_otp_root(page) is not None,
            timeout_sec=25,
        ):
            return False
        root = _primary_otp_root(page)
    if root is None:
        return False

    url = _root_url(root, page)
    page_label = "mfa email-otp" if "email-otp" in url else "email-verification"
    rt.log(f"{page_label} 页处理验证码")

    if not rt._otp_visible(root):
        rt.log("等待验证码输入框…")
        rt._poll_until(lambda: rt._otp_visible(root), timeout_sec=20)

    if not rt._otp_visible(root):
        rt.log("email-verification 页未找到验证码输入框")
        return False

    rt.log("轮询 mailapi 获取邮箱验证码…")
    code = rt._fetch_otp_code_from_mail(mailapi_url, refetch=refetch)
    rt._pause()

    if rt._fill_split_otp_inputs(root, code):
        rt._pause()
        if _web_wait_after_otp_submit(page, root, timeout_sec=8):
            rt.log("验证码输入后已进入下一页")
            return True
    else:
        otp_input = rt._first_visible(
            root,
            [
                'input[inputmode="numeric"]',
                'input[autocomplete="one-time-code"]',
                'input[name="code"]',
                'input[type="tel"]',
                'input[type="text"]',
            ],
            timeout=8000,
        )
        if not otp_input:
            rt.log("未找到验证码输入框")
            return False
        rt._type_chars(otp_input, code)
        rt._pause(0)
        if _web_wait_after_otp_submit(page, root, timeout_sec=6):
            rt.log("验证码输入后已进入下一页")
            return True

    if not _web_click_otp_submit(root, page):
        rt.log("未点到「继续」，尝试 Enter")
        root.keyboard.press("Enter")
        rt._pause()
    rt._pause(0)

    if _web_wait_after_otp_submit(page, root, timeout_sec=20):
        return True

    if _is_still_on_otp_page(page, root):
        rt._check_otp_error_or_raise(root)
        rt.log("提交后仍在 email-verification 页")
    return True


def _web_process_otp_step(page: Page, mailapi_url: str, otp_submitted: bool) -> bool:
    """email-verification / mfa email-otp：获取验证码、输入、点击继续。"""
    if _has_session_cookie(page):
        return False

    if not _is_email_verification_any(page) and not _otp_visible_any(page):
        rt._poll_until(
            lambda: _is_email_verification_any(page) or _otp_visible_any(page),
            timeout_sec=15,
        )

    if not _is_email_verification_any(page) and not _otp_visible_any(page):
        return otp_submitted

    if otp_submitted:
        root = _primary_otp_root(page)
        if root is None or not _is_still_on_otp_page(page, root):
            return False
        if rt._detect_otp_error(root):
            rt.log(f"验证码错误，{rt.OTP_PAGE_DELAY_SEC}s 后重新从邮箱获取…")
            if _web_fill_otp(page, mailapi_url, refetch=True):
                root = _primary_otp_root(page)
                if root is None or not _is_still_on_otp_page(page, root):
                    return False
                return True
            return True
        rt.log("验证码已填，重试点击「继续」…")
        _web_click_otp_submit(root, page)
        rt._pause(0)
        if _web_wait_after_otp_submit(page, root, timeout_sec=15):
            return False
        if _is_still_on_otp_page(page, root):
            if rt._detect_otp_error(root):
                rt.log(f"验证码错误，{rt.OTP_PAGE_DELAY_SEC}s 后重新从邮箱获取…")
                if _web_fill_otp(page, mailapi_url, refetch=True):
                    root = _primary_otp_root(page)
                    if root is None or not _is_still_on_otp_page(page, root):
                        return False
                    return True
            rt._check_otp_error_or_raise(root)
        return True

    if _web_fill_otp(page, mailapi_url):
        root = _primary_otp_root(page)
        if root is None or not _is_still_on_otp_page(page, root):
            return False
        return True
    return otp_submitted


def _web_handle_choose_account(page: Page) -> bool:
    for root in _auth_roots(page):
        if "choose-an-account" not in _root_url(root, page):
            continue
        rt.log("choose-an-account 弹窗，点击「登录至另一个账户」")
        rt._pause(0)
        for exact_text in ("登录至另一个帐户", "登录至另一个账户", "Sign in to another account"):
            try:
                loc = root.get_by_text(exact_text, exact=True)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click(timeout=10000)
                    rt._pause(0)
                    rt.log(f"已点击: {exact_text}")
                    return True
            except Exception:  # noqa: BLE001
                pass
        for text in rt.BTN_ANOTHER_ACCOUNT:
            if _click_text_on_root(root, [text], timeout=8000):
                rt._pause(0)
                return True
    return rt._handle_choose_account(page)


def _web_advance_mfa_to_email_otp(page: Page) -> bool:
    for root in _auth_roots(page):
        url = _root_url(root, page)
        if "mfa-challenge" not in url or "email-otp" in url:
            continue
        if rt._click_text(root, rt.BTN_MFA_TRY_OTHER, timeout=8000):
            rt._pause(0)
        if rt._click_text(root, rt.BTN_MFA_EMAIL, timeout=8000):
            rt._pause(0)
            return True
    return rt._advance_mfa_to_email_otp(page)


def _wait_after_modal_email(page: Page, *, timeout_sec: float = 15) -> None:
    def ready() -> bool:
        if _password_visible_any(page) or rt._password_visible(page):
            return True
        if _is_email_verification_any(page) or _otp_visible_any(page):
            return True
        if rt._is_email_verification_page(page) or rt._is_mfa_challenge_page(page):
            return True
        for root in _auth_roots(page):
            url = _root_url(root, page)
            if any(k in url for k in ("password", "email-verification", "mfa-challenge")):
                return True
        return not _email_visible_any(page)

    if rt._poll_until(ready, timeout_sec=timeout_sec):
        rt.log("邮箱提交后已进入下一页")


def fetch_session_from_page(page: Page) -> dict[str, Any]:
    """导航到 session 页并解析 body JSON。"""
    rt._open_oauth_url(page, SESSION_URL)
    rt._pause(0)
    try:
        text = page.evaluate("() => document.body ? document.body.innerText : ''")
        text = (text or "").strip()
        if not text:
            return {}
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        rt.log(f"解析 session 页面失败: {exc}")
        return {}


def get_session_data(page: Page, *, navigate_fallback: bool = False) -> dict[str, Any]:
    data = _fetch_session_in_browser(page)
    if data.get("accessToken"):
        return data
    if navigate_fallback:
        return fetch_session_from_page(page)
    return {}


def merge_session_into_account(account: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
    access = session.get("accessToken") or ""
    if not access:
        raise RuntimeError("session 响应缺少 accessToken")

    account["access_token"] = access
    if session.get("sessionToken"):
        account["session_token"] = session["sessionToken"]

    user = session.get("user") or {}
    acc = session.get("account") or {}
    if user.get("email"):
        account["email"] = user["email"]
        account["login_identity"] = user["email"]
        account["account_claims_email"] = user["email"]
    if user.get("id"):
        account["chatgpt_user_id"] = user["id"]
    if acc.get("id"):
        account["chatgpt_account_id"] = acc["id"]
        account["project_id"] = acc["id"]
        account["workspace_id"] = acc["id"]

    account["last_token_refresh"] = datetime.now(timezone.utc).isoformat()
    account["auth_mode"] = "chatgpt"
    return account


def advance_web_login_flow(
    page: Page,
    email: str,
    password: str,
    mailapi_url: str,
    *,
    timeout_sec: int = 180,
    login_email_done: bool = False,
) -> None:
    """参考 refresh_tokens 登录流程，登录完成后读取 session（无 Codex 授权页）。"""
    deadline = time.time() + timeout_sec
    otp_submitted = False
    password_done = False
    choose_account_tries = 0
    last_kind = ""

    while time.time() < deadline:
        rt._check_shutdown()

        if _has_session_cookie(page):
            rt.log("检测到 session cookie，登录完成")
            return

        if not (password_done and rt._is_login_password_page(page)):
            try:
                page.wait_for_load_state("domcontentloaded", timeout=800)
            except Exception:  # noqa: BLE001
                pass

        kind = _web_login_page_kind(page)
        if kind != last_kind:
            rt._log_current_page(page, kind)
            last_kind = kind

        if kind == "chatgpt_home":
            if _has_session_cookie(page):
                rt.log("ChatGPT 已登录")
                return
            if _open_chatgpt_login_modal(page):
                rt._pause(0)
            continue

        if kind == "callback":
            rt.log("意外进入 OAuth 回调…")
            if _has_session_cookie(page):
                return
            rt._pause(0)
            continue

        if kind == "mfa_challenge":
            otp_submitted = False
            if not _web_advance_mfa_to_email_otp(page):
                raise rt.MfaChallengeError(f"MFA 无法切换至邮箱验证: {page.url.split('?', 1)[0]}")
            rt._pause(0)
            continue

        if kind == "login_or_create":
            otp_submitted = False
            if login_email_done or rt._password_visible(page) or _password_visible_any(page):
                rt._pause(0)
                continue
            if _fill_email_any(page, email) or rt._handle_login_or_create_email(page, email):
                login_email_done = True
                _wait_after_modal_email(page)
                rt._wait_after_login_email(page)
            rt._pause(0)
            continue

        if kind == "choose_account":
            otp_submitted = False
            choose_account_tries += 1
            if choose_account_tries > 6:
                raise RuntimeError(
                    "choose-an-account 页多次未能点击「登录至另一个账户」，请检查页面文案或手动登录"
                )
            if _web_handle_choose_account(page):
                choose_account_tries = 0
                rt._wait_known_auth_page(page, timeout_sec=15)
            rt._pause(0)
            continue

        if kind == "login":
            otp_submitted = False
            if login_email_done or rt._password_visible(page) or _password_visible_any(page) or rt._is_login_password_page(page):
                rt._pause(0)
                continue
            if _fill_email_any(page, email) or rt._handle_login_email(page, email):
                login_email_done = True
                _wait_after_modal_email(page)
                rt._wait_after_login_email(page)
            rt._pause(0)
            continue

        if kind == "password":
            otp_submitted = False
            login_email_done = True
            if password_done:
                if rt._is_login_password_page(page) or _password_visible_any(page):
                    rt._wait_after_password(page, timeout_sec=35)
                continue
            if _fill_password_any(page, password) or rt._fill_password(page, password):
                password_done = True
                rt._wait_after_password(page, timeout_sec=35)
            continue

        if kind in ("email_verification", "mfa_email_otp"):
            otp_submitted = _web_process_otp_step(page, mailapi_url, otp_submitted)
            rt._pause(0)
            continue

        if kind == "consent":
            rt.log("遇到 Codex 授权页，网页登录流程跳过授权…")
            if _has_session_cookie(page):
                return
            rt._open_oauth_url(page, CHATGPT_HOME_URL)
            rt._pause(0)
            continue

        if rt._is_mfa_challenge_page(page) or any(
            "mfa-challenge" in _root_url(r, page) for r in _auth_roots(page)
        ):
            if rt._is_mfa_email_otp_page(page) or any(
                "email-otp" in _root_url(r, page) for r in _auth_roots(page)
            ):
                otp_submitted = _web_process_otp_step(page, mailapi_url, otp_submitted)
            else:
                otp_submitted = False
                if not _web_advance_mfa_to_email_otp(page):
                    raise rt.MfaChallengeError(
                        f"MFA 无法切换至邮箱验证: {page.url.split('?', 1)[0]}"
                    )
            rt._pause(0)
            continue

        if _is_chatgpt_homepage(page) and not login_email_done:
            if _has_session_cookie(page):
                rt.log("ChatGPT 已登录")
                return
            if _open_chatgpt_login_modal(page):
                if _fill_email_any(page, email):
                    login_email_done = True
                    _wait_after_modal_email(page)
            rt._pause(0)
            continue

        if rt._try_another_account(page):
            rt._wait_known_auth_page(page, timeout_sec=15)
            continue

        if _is_email_verification_any(page) or _otp_visible_any(page):
            otp_submitted = _web_process_otp_step(page, mailapi_url, otp_submitted)
            rt._pause(0)
            continue

        rt.log(f"等待已知登录页… {page.url[:90]}")
        rt._wait_known_auth_page(page, timeout_sec=10)
        rt._pause(0)

    if _has_session_cookie(page):
        rt.log("超时前检测到 session cookie")
        return
    session = get_session_data(page, navigate_fallback=True)
    if session.get("accessToken"):
        rt.log("超时前已获取 session")
        return
    raise RuntimeError("网页登录超时，未获取到 session accessToken")


def browser_web_session_login(
    email: str,
    password: str,
    mailapi_url: str,
    *,
    headless: bool = False,
    chrome_profile: Path = rt.DEFAULT_CHROME_PROFILE,
    use_system_chrome: bool = True,
    cdp_url: str | None = None,
) -> dict[str, Any]:
    if rt._shutdown_requested.is_set():
        rt._check_shutdown()
    with sync_playwright() as p:
        _browser, context, owns = rt._launch_browser_context(
            p,
            chrome_profile=chrome_profile,
            use_system_chrome=use_system_chrome,
            headless=headless,
            cdp_url=cdp_url,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(10000)
        try:
            rt.log(f"打开 ChatGPT: {CHATGPT_HOME_URL}")
            rt._open_oauth_url(page, CHATGPT_HOME_URL)
            rt._pause(0)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:  # noqa: BLE001
                pass

            email_filled = False
            if _has_session_cookie(page):
                rt.log("已登录，跳过登录弹窗")
            else:
                if not _open_chatgpt_login_modal(page):
                    raise RuntimeError("未能打开 ChatGPT 登录弹窗")
                if _fill_email_any(page, email):
                    email_filled = True
                    _wait_after_modal_email(page)
                else:
                    rt.log("弹窗邮箱未立即出现，交由后续流程处理")

            advance_web_login_flow(
                page,
                email,
                password,
                mailapi_url,
                login_email_done=email_filled,
            )
            session = get_session_data(page, navigate_fallback=True)
            if not session.get("accessToken"):
                raise RuntimeError("登录完成但未获取到 accessToken")
            rt.log(f"session 获取成功: {email}")
            return session
        except KeyboardInterrupt:
            rt._handle_interrupt()
        except PlaywrightError:
            if rt._shutdown_requested.is_set():
                raise KeyboardInterrupt from None
            raise
        finally:
            if owns and not rt._shutdown_requested.is_set():
                try:
                    context.close()
                except Exception:  # noqa: BLE001
                    pass


def _clear_local_profile_before_account(
    chrome_profile: Path,
    email: str,
    *,
    cdp_url: str | None,
    use_main_chrome_profile: bool,
) -> None:
    """每个新邮箱开始前清空项目内 .chrome-profile，避免 Cookie/登录态残留。"""
    if cdp_url:
        rt.log(f"{email}: CDP 模式，跳过清除 Chrome profile")
        return
    if use_main_chrome_profile:
        rt.log(f"{email}: 使用主 Chrome 配置，跳过清除")
        return
    if chrome_profile.name != ".chrome-profile":
        rt.log(f"{email}: 非 .chrome-profile，跳过清除 ({chrome_profile.name})")
        return
    rt.log(f"{email}: 清除本地 Chrome profile …")
    rt._reset_chrome_profile_if_needed(chrome_profile)


def refresh_one_via_web_session(
    account: dict[str, Any],
    *,
    headless: bool,
    chrome_profile: Path,
    use_system_chrome: bool,
    use_main_chrome_profile: bool,
    cdp_url: str | None,
) -> dict[str, Any]:
    email = account["email"]
    password = account.get("password") or ""
    mailapi_url = rt.get_mailapi_url(account)
    if not mailapi_url:
        raise ValueError(f"{email}: 缺少 mailapi_url")
    if not password:
        raise ValueError(f"{email}: 缺少 password")

    _clear_local_profile_before_account(
        chrome_profile,
        email,
        cdp_url=cdp_url,
        use_main_chrome_profile=use_main_chrome_profile,
    )
    rt.log(f"{email}: 网页登录获取 session token…")
    session = browser_web_session_login(
        email,
        password,
        mailapi_url,
        headless=headless,
        chrome_profile=chrome_profile,
        use_system_chrome=use_system_chrome,
        cdp_url=cdp_url,
    )
    return merge_session_into_account(dict(account), session)


def main() -> int:
    parser = argparse.ArgumentParser(description="ChatGPT 网页登录获取 session token")
    parser.add_argument("--accounts", type=Path, default=rt.DEFAULT_ACCOUNTS)
    parser.add_argument("--need-email", type=Path, default=rt.DEFAULT_NEED_EMAIL)
    parser.add_argument("--auth-file", type=Path, default=rt.AUTH_PATH)
    parser.add_argument("--email", action="append")
    parser.add_argument("--headless", action="store_true", default=False)
    parser.add_argument(
        "--chrome-profile",
        type=Path,
        default=rt.DEFAULT_CHROME_PROFILE,
        help=f"Chrome 用户数据目录，默认 {rt.DEFAULT_CHROME_PROFILE.name}/",
    )
    parser.add_argument("--use-main-chrome-profile", action="store_true")
    parser.add_argument("--no-system-chrome", action="store_true")
    parser.add_argument(
        "--cdp",
        type=str,
        default=os.environ.get("CHROME_CDP_URL", ""),
        help="连接已打开的 Chrome CDP",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--cli-proxy-dir", type=Path, default=rt.CLI_PROXY_DIR)
    parser.add_argument("--no-cli-proxy", action="store_true")
    args = parser.parse_args()

    chrome_profile = (
        rt._system_chrome_user_data() if args.use_main_chrome_profile else args.chrome_profile
    )
    use_system_chrome = not args.no_system_chrome
    cdp_url = args.cdp.strip() or None
    if args.use_main_chrome_profile:
        rt.log("使用本机 Chrome 主配置 — 请先完全退出 Chrome，否则会因配置锁失败")
    if args.headless:
        rt.log("警告: 无头模式更容易被 OpenAI 识别为自动化")

    rows, index = rt.load_accounts(args.accounts)
    targets = args.email or rt.load_need_emails(args.need_email)
    if not targets:
        rt.log("need_email.txt 为空")
        return 1

    if not args.dry_run:
        backup = args.accounts.with_suffix(".json.bak")
        shutil.copy2(args.accounts, backup)
        rt.log(f"备份 -> {backup}")

    codex_backup = rt.CodexEnvBackup(
        auth_path=args.auth_file.expanduser(),
        config_path=rt.CONFIG_PATH,
    )
    rt._install_codex_restore_hooks(codex_backup)

    ok = fail = abandoned = abnormal = 0
    try:
        with codex_backup:
            for idx, raw in enumerate(targets):
                if rt._shutdown_requested.is_set():
                    break
                key = raw.lower()
                if key not in index:
                    rt.log(f"跳过 {raw}: 不在 accounts.json")
                    fail += 1
                    continue
                acc = rows[index[key]]
                if rt.is_skipped_refreshed(args.need_email, acc["email"]):
                    rt.log(f"跳过 {acc['email']}: need_email.txt 已标记 {rt.REFRESHED_MARK}")
                    continue
                if rt.is_skipped_abandoned(args.need_email, acc["email"]):
                    rt.log(f"跳过 {acc['email']}: need_email.txt 已标记 {rt.ABANDONED_MARK}")
                    continue
                if rt.is_skipped_abnormal(args.need_email, acc["email"]):
                    rt.log(f"跳过 {acc['email']}: need_email.txt 已标记 {rt.ABNORMAL_MARK}")
                    continue

                rt.log(f"========== {acc['email']} ==========")
                try:
                    rows[index[key]] = refresh_one_via_web_session(
                        acc,
                        headless=args.headless,
                        chrome_profile=chrome_profile,
                        use_system_chrome=use_system_chrome,
                        use_main_chrome_profile=args.use_main_chrome_profile,
                        cdp_url=cdp_url,
                    )
                    if not args.dry_run:
                        rt.persist_refresh_success(
                            rows[index[key]],
                            email=acc["email"],
                            accounts_path=args.accounts,
                            rows=rows,
                            need_email_path=args.need_email,
                            cli_proxy_dir=args.cli_proxy_dir,
                            sync_cli_proxy=not args.no_cli_proxy,
                        )
                    rt.log(f"成功 {acc['email']}")
                    ok += 1
                except rt.MfaChallengeError as exc:
                    rt.log(f"废弃 {acc['email']}: {exc}")
                    if not args.dry_run:
                        rt.persist_account_abandoned(
                            acc["email"],
                            need_email_path=args.need_email,
                            cli_proxy_dir=args.cli_proxy_dir,
                            sync_cli_proxy=not args.no_cli_proxy,
                        )
                    abandoned += 1
                except rt.AccountAbnormalError as exc:
                    rt.log(f"异常 {acc['email']}: {exc}")
                    if not args.dry_run:
                        rt.persist_account_abnormal(
                            acc["email"],
                            need_email_path=args.need_email,
                            reason="验证码错误",
                        )
                    abnormal += 1
                except KeyboardInterrupt:
                    rt._handle_interrupt()
                except Exception as exc:  # noqa: BLE001
                    rt.log(f"失败 {raw}: {exc}")
                    fail += 1

                if rt._shutdown_requested.is_set():
                    break
                if idx < len(targets) - 1:
                    rt.log(f"等待 {rt.ACCOUNT_GAP_SEC}s 后处理下一个账号…")
                    rt._interruptible_sleep(rt.ACCOUNT_GAP_SEC)

        if rt._shutdown_requested.is_set():
            rt.log("用户中断")
            return 130

        rt.log(f"完成: 成功 {ok} 废弃 {abandoned} 异常 {abnormal} 失败 {fail}")
        return 0 if fail == 0 else 2
    except KeyboardInterrupt:
        rt._handle_interrupt()


if __name__ == "__main__":
    raise SystemExit(main())
