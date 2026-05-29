#!/usr/bin/env python3
"""先 ChatGPT 网页登录，再本地 OAuth 回调服务完成 Codex 授权，回写 accounts 等。

刷新策略（默认）：
1. 检查 ~/.cli-proxy-api 中 email 字段匹配的 JSON 的 access_token 是否仍有效 → 有效则仅更新「已刷新」
2. access_token 失效 → 用 refresh_token API 刷新
3. refresh_token 也失效 → 网页登录 + 本地 OAuth 回调换 token

不依赖本机 `codex login` CLI。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import shutil
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright
except ImportError:
    print("请先: pip install -r requirements.txt && playwright install chromium", file=sys.stderr)
    raise

import refresh_tokens as rt
import cli_proxy_io
import refresh_web_session as web
from fetch_quota import fetch_openai_usage, is_token_invalid_error

SCRIPT_DIR = Path(__file__).resolve().parent

OAUTH_AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
OAUTH_REDIRECT_URI = "http://localhost:1455/auth/callback"
OAUTH_CALLBACK_HOST = "127.0.0.1"
OAUTH_CALLBACK_PORT = 1455
OAUTH_SCOPE = "openid email profile offline_access"
ACCESS_TOKEN_SKEW_SEC = 300
DEFAULT_QUOTA_PROXY = os.environ.get("QUOTA_PROXY_URL", "http://127.0.0.1:11080")
EXPIRED_DT_FORMATS = (
    "%Y-%m-%d %H:%M:%S +0800",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S+08:00",
)


def load_cli_proxy_data(email: str, proxy_dir: Path) -> dict[str, Any]:
    path = cli_proxy_io.find_cli_proxy_file(email, proxy_dir)
    if path is None:
        return {}
    try:
        return cli_proxy_io.load_cli_proxy_json(path)
    except json.JSONDecodeError as exc:
        rt.log(f"读取 {path.name} 失败: {exc}")
        return {}


def merge_cli_proxy_into_account(
    account: dict[str, Any],
    proxy_data: dict[str, Any],
) -> dict[str, Any]:
    if proxy_data.get("access_token"):
        account["access_token"] = proxy_data["access_token"]
    if proxy_data.get("refresh_token"):
        account["refresh_token"] = proxy_data["refresh_token"]
    if proxy_data.get("id_token"):
        account["id_token"] = proxy_data["id_token"]
    account_id = proxy_data.get("account_id") or proxy_data.get("chatgpt_account_id")
    if account_id:
        account["chatgpt_account_id"] = account_id
        account["project_id"] = account_id
        account["workspace_id"] = account_id
    account["auth_mode"] = account.get("auth_mode") or "chatgpt"
    return account


def _parse_expired_at(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    for fmt in EXPIRED_DT_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=rt.TZ_CN)
            return dt
        except ValueError:
            continue
    return None


def _access_token_expired_by_field(proxy_data: dict[str, Any]) -> bool | None:
    expired = proxy_data.get("expired")
    if not expired:
        return None
    dt = _parse_expired_at(str(expired))
    if dt is None:
        return None
    now = datetime.now(dt.tzinfo or rt.TZ_CN)
    return now >= dt - timedelta(seconds=ACCESS_TOKEN_SKEW_SEC)


def _access_token_expired_by_jwt(access_token: str) -> bool | None:
    try:
        payload = rt._jwt_payload(access_token)
        exp = payload.get("exp")
        if not exp:
            return None
        return time.time() >= int(exp) - ACCESS_TOKEN_SKEW_SEC
    except Exception:  # noqa: BLE001
        return None


def _probe_access_token_api(
    access_token: str,
    account_id: str,
    *,
    proxy_url: str | None,
) -> bool:
    try:
        fetch_openai_usage(access_token, account_id, proxy_url=proxy_url)
        return True
    except requests.HTTPError as exc:
        if is_token_invalid_error(exc):
            return False
        status = exc.response.status_code if exc.response is not None else "?"
        rt.log(f"token 探测 HTTP {status}，按本地 expiry 视为仍有效")
        return True
    except requests.RequestException as exc:
        rt.log(f"token 探测网络异常: {exc}，按本地 expiry 视为仍有效")
        return True


def is_cli_proxy_access_token_valid(
    proxy_data: dict[str, Any],
    *,
    proxy_url: str | None = None,
) -> bool:
    """判断 cli-proxy 原件中的 access_token 是否仍可用。"""
    access = str(proxy_data.get("access_token") or "").strip()
    account_id = str(proxy_data.get("account_id") or "").strip()
    if not access:
        return False

    by_field = _access_token_expired_by_field(proxy_data)
    if by_field is True:
        return False
    by_jwt = _access_token_expired_by_jwt(access)
    if by_jwt is True:
        return False

    if by_field is False or by_jwt is False:
        if account_id:
            return _probe_access_token_api(access, account_id, proxy_url=proxy_url)
        return True

    if account_id:
        return _probe_access_token_api(access, account_id, proxy_url=proxy_url)
    return False


def _resolve_proxy_url(proxy_url: str | None) -> str | None:
    if proxy_url is None:
        proxy_url = DEFAULT_QUOTA_PROXY
    if str(proxy_url).strip().lower() in ("", "none", "direct"):
        return None
    return proxy_url.strip()


def _browser_login_and_merge(
    account: dict[str, Any],
    *,
    auth_path: Path,
    headless: bool,
    chrome_profile: Path,
    use_system_chrome: bool,
    use_main_chrome_profile: bool,
    cdp_url: str | None,
) -> tuple[dict[str, Any], bool]:
    """返回 (更新后的账号, 是否因手机验证改走 session API)。"""
    email = account["email"]
    password = account.get("password") or ""
    mailapi_url = rt.get_mailapi_url(account)
    if not mailapi_url:
        raise ValueError(f"{email}: 缺少 mailapi_url")
    if not password:
        raise ValueError(f"{email}: 缺少 password")

    web._clear_local_profile_before_account(
        chrome_profile,
        email,
        cdp_url=cdp_url,
        use_main_chrome_profile=use_main_chrome_profile,
    )

    rt.log(f"{email}: 网页登录 + 本地 OAuth 刷新…")
    if auth_path.exists():
        auth_path.unlink()

    session, auth, used_session_fallback = browser_web_then_codex_login(
        email,
        password,
        mailapi_url,
        auth_path=auth_path,
        headless=headless,
        chrome_profile=chrome_profile,
        use_system_chrome=use_system_chrome,
        cdp_url=cdp_url,
    )

    updated = web.merge_session_into_account(dict(account), session)
    return rt.merge_auth_into_account(updated, auth), used_session_fallback


def _generate_pkce() -> tuple[str, str]:
    code_verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


def _extract_account_id(id_token: str | None, access_token: str | None) -> str | None:
    for token in (id_token, access_token):
        if not token:
            continue
        try:
            payload = rt._jwt_payload(token)
        except Exception:  # noqa: BLE001
            continue
        if aid := payload.get("chatgpt_account_id"):
            return str(aid)
        auth_ns = payload.get("https://api.openai.com/auth") or {}
        if isinstance(auth_ns, dict) and (aid := auth_ns.get("chatgpt_account_id")):
            return str(aid)
        orgs = payload.get("organizations") or []
        if orgs and isinstance(orgs[0], dict) and (aid := orgs[0].get("id")):
            return str(aid)
    return None


def _exchange_authorization_code(code: str, code_verifier: str) -> dict[str, Any]:
    resp = requests.post(
        rt.OAUTH_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": rt.OAUTH_CLIENT_ID,
            "code": code,
            "redirect_uri": OAUTH_REDIRECT_URI,
            "code_verifier": code_verifier,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if resp.status_code != 200:
        body = (resp.text or "")[:240]
        raise RuntimeError(f"OAuth token 交换失败 HTTP {resp.status_code}: {body}")
    data = resp.json()
    if not data.get("access_token"):
        raise RuntimeError("OAuth token 响应缺少 access_token")
    return data


def _build_auth_payload(token_response: dict[str, Any]) -> dict[str, Any]:
    access = token_response.get("access_token") or ""
    refresh = token_response.get("refresh_token") or ""
    id_token = token_response.get("id_token") or ""
    account_id = _extract_account_id(id_token, access)
    tokens: dict[str, Any] = {
        "access_token": access,
        "refresh_token": refresh,
        "id_token": id_token,
    }
    if account_id:
        tokens["account_id"] = account_id
    return {
        "auth_mode": "chatgpt",
        "tokens": tokens,
    }


def _build_auth_from_session(session: dict[str, Any]) -> dict[str, Any]:
    """从 chatgpt.com/api/auth/session 响应构建 auth 结构。"""
    access = session.get("accessToken") or ""
    if not access:
        raise RuntimeError("session 响应缺少 accessToken")

    account_id: str | None = None
    acc = session.get("account") or {}
    if acc.get("id"):
        account_id = str(acc["id"])
    if not account_id:
        account_id = _extract_account_id(None, access)

    tokens: dict[str, Any] = {"access_token": access}
    if account_id:
        tokens["account_id"] = account_id
    return {
        "auth_mode": "chatgpt",
        "tokens": tokens,
    }


def _session_auth_fallback(page: Any, auth_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """OAuth 遇到手机验证/手机 OTP 时，改用 session API 获取 token 并写入 auth 文件。"""
    rt.log("OAuth 遇到手机验证/OTP，跳过 OAuth，改用 session API 获取 access_token…")
    session = web.get_session_data(page, navigate_fallback=True)
    if not session.get("accessToken"):
        raise RuntimeError("session API 未返回 accessToken")
    auth = _build_auth_from_session(session)
    _write_auth_file(auth_path, auth)
    rt.log("已通过 session API 获取 access_token 并写入 auth 文件")
    return session, auth


def _write_auth_file(auth_path: Path, payload: dict[str, Any]) -> None:
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = auth_path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp.replace(auth_path)
    rt.log(f"已写入 {auth_path}")


class LocalOAuthServer:
    """本地 OAuth 回调服务（等价于 codex login 的 localhost 监听）。"""

    def __init__(self, auth_path: Path) -> None:
        self.auth_path = auth_path
        self.code_verifier, self.code_challenge = _generate_pkce()
        self.state = secrets.token_hex(16)
        self.oauth_url = self._build_authorize_url()
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._done = threading.Event()
        self._error: str | None = None
        self._auth: dict[str, Any] | None = None

    def _build_authorize_url(self) -> str:
        params = {
            "response_type": "code",
            "client_id": rt.OAUTH_CLIENT_ID,
            "redirect_uri": OAUTH_REDIRECT_URI,
            "scope": OAUTH_SCOPE,
            "code_challenge": self.code_challenge,
            "code_challenge_method": "S256",
            "state": self.state,
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "prompt": "login",
        }
        return f"{OAUTH_AUTHORIZE_URL}?{urlencode(params)}"

    def start(self) -> str:
        if self._thread and self._thread.is_alive():
            return self.oauth_url

        handler_cls = _make_callback_handler(self)
        self._server = HTTPServer((OAUTH_CALLBACK_HOST, OAUTH_CALLBACK_PORT), handler_cls)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="oauth-callback",
            daemon=True,
        )
        self._thread.start()
        rt.log(f"本地 OAuth 回调服务已启动: {OAUTH_REDIRECT_URI}")
        return self.oauth_url

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:  # noqa: BLE001
                pass
            try:
                self._server.server_close()
            except Exception:  # noqa: BLE001
                pass
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def wait_result(self, timeout: int = 300) -> dict[str, Any]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            rt._check_shutdown()
            if self._done.is_set():
                if self._error:
                    raise RuntimeError(self._error)
                if self._auth is not None:
                    return self._auth
                if rt._auth_file_ready(self.auth_path):
                    return json.loads(self.auth_path.read_text(encoding="utf-8"))
                raise RuntimeError("OAuth 回调完成但未得到 auth 数据")
            rt._interruptible_sleep(0.5)
        raise TimeoutError(f"等待 OAuth 回调超时 ({timeout}s)")

    def _on_callback(self, code: str | None, error: str | None) -> None:
        if error:
            self._error = error
            self._done.set()
            return
        if not code:
            self._error = "回调缺少 authorization code"
            self._done.set()
            return
        try:
            rt.log("收到 OAuth 回调，交换 token…")
            token_response = _exchange_authorization_code(code, self.code_verifier)
            auth = _build_auth_payload(token_response)
            _write_auth_file(self.auth_path, auth)
            self._auth = auth
        except Exception as exc:  # noqa: BLE001
            self._error = str(exc)
        finally:
            self._done.set()


def _make_callback_handler(oauth: LocalOAuthServer) -> type[BaseHTTPRequestHandler]:
    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != "/auth/callback":
                self.send_response(404)
                self.end_headers()
                return

            params = parse_qs(parsed.query)
            received_state = params.get("state", [None])[0]
            error: str | None = None
            code: str | None = None

            if received_state != oauth.state:
                error = "state 不匹配"
            elif "error" in params:
                error = params.get("error_description", params.get("error", ["Unknown error"]))[0]
            else:
                code = params.get("code", [None])[0]
                if not code:
                    error = "缺少 code 参数"

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if error:
                html = f"<html><body><h1>授权失败</h1><p>{error}</p></body></html>"
            else:
                html = "<html><body><h1>授权成功</h1><p>可以关闭此页面。</p></body></html>"
            self.wfile.write(html.encode("utf-8"))

            threading.Thread(target=oauth._on_callback, args=(code, error), daemon=True).start()
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    return CallbackHandler


class _OAuthLoginHandle:
    """供 refresh_tokens 中断处理终止本地 OAuth 服务。"""

    def __init__(self, oauth: LocalOAuthServer) -> None:
        self.oauth = oauth

    def terminate(self) -> None:
        rt.log("停止本地 OAuth 回调服务…")
        self.oauth.stop()


def _run_web_login_on_page(
    page: Any,
    email: str,
    password: str,
    mailapi_url: str,
) -> dict[str, Any]:
    rt.log(f"打开 ChatGPT: {web.CHATGPT_HOME_URL}")
    rt._open_oauth_url(page, web.CHATGPT_HOME_URL)
    rt._pause(0)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:  # noqa: BLE001
        pass

    email_filled = False
    if web._has_session_cookie(page):
        rt.log("已登录，跳过登录弹窗")
    else:
        if not web._open_chatgpt_login_modal(page):
            raise RuntimeError("未能打开 ChatGPT 登录弹窗")
        if web._fill_email_any(page, email):
            email_filled = True
            web._wait_after_modal_email(page)
        else:
            rt.log("弹窗邮箱未立即出现，交由后续流程处理")

    web.advance_web_login_flow(
        page,
        email,
        password,
        mailapi_url,
        login_email_done=email_filled,
    )
    session = web.get_session_data(page, navigate_fallback=True)
    if not session.get("accessToken"):
        raise RuntimeError("网页登录完成但未获取到 session accessToken")
    rt.log(f"网页 session 获取成功: {email}")
    return session


def _run_codex_oauth_on_page(
    page: Any,
    oauth_url: str,
    email: str,
    password: str,
    mailapi_url: str,
    *,
    auth_path: Path,
) -> bool:
    """执行 Codex OAuth 流程。返回 True 表示 OAuth 正常完成；False 表示已改走 session 回退。"""
    rt.log(f"打开 Codex OAuth: {oauth_url[:96]}…")
    rt._open_oauth_url(page, oauth_url)
    rt._pause()

    if rt._is_mfa_challenge_page(page):
        raise rt.MfaChallengeError(f"OAuth 遇到 MFA 验证页: {page.url.split('?', 1)[0]}")

    if rt._is_choose_account_page(page):
        rt._click_current_account_on_choose_page(page, email)
        rt._pause(0)
        if rt._is_phone_auth_block_page(page):
            _session_auth_fallback(page, auth_path)
            return False
    elif not rt._is_codex_consent_page(page) and not rt._is_callback_url(page.url):
        rt.log("OAuth 页未在 choose-an-account，继续后续流程")

    if rt._is_phone_auth_block_page(page):
        _session_auth_fallback(page, auth_path)
        return False

    try:
        rt._advance_login_flow(
            page,
            email,
            password,
            mailapi_url,
            auth_path=auth_path,
            use_current_account=True,
        )
    except rt.PhoneVerificationError:
        _session_auth_fallback(page, auth_path)
        return False
    rt._pause()
    return True


def browser_web_then_codex_login(
    email: str,
    password: str,
    mailapi_url: str,
    *,
    auth_path: Path,
    headless: bool = False,
    chrome_profile: Path = rt.DEFAULT_CHROME_PROFILE,
    use_system_chrome: bool = True,
    cdp_url: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """同一浏览器：网页登录 → 本地 OAuth 授权 → 返回 (session, auth, used_session_fallback)。"""
    if rt._shutdown_requested.is_set():
        rt._check_shutdown()

    oauth = LocalOAuthServer(auth_path)
    handle = _OAuthLoginHandle(oauth)
    rt._active_login = handle
    try:
        oauth.start()
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
                rt.log("步骤 1/2: ChatGPT 网页登录…")
                session = _run_web_login_on_page(page, email, password, mailapi_url)

                rt.log("步骤 2/2: Codex OAuth 授权（本地回调服务）…")
                oauth_ok = _run_codex_oauth_on_page(
                    page,
                    oauth.oauth_url,
                    email,
                    password,
                    mailapi_url,
                    auth_path=auth_path,
                )
                if oauth_ok:
                    auth = oauth.wait_result(timeout=300)
                    return session, auth, False
                auth = json.loads(auth_path.read_text(encoding="utf-8"))
                session = web.get_session_data(page, navigate_fallback=True)
                return session, auth, True
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
    except KeyboardInterrupt:
        rt._handle_interrupt()
    finally:
        rt._active_login = None
        oauth.stop()

    raise RuntimeError("未完成 web + codex 登录")


def refresh_one_via_web_then_codex(
    account: dict[str, Any],
    *,
    auth_path: Path,
    headless: bool,
    chrome_profile: Path,
    use_system_chrome: bool,
    use_main_chrome_profile: bool,
    cdp_url: str | None,
    proxy_dir: Path = rt.CLI_PROXY_DIR,
    proxy_url: str | None = None,
    force_browser: bool = False,
) -> tuple[dict[str, Any], str]:
    """返回 (更新后的账号, 刷新方式: skip_valid | refresh_token | browser | browser_session)。"""
    email = account["email"]
    proxy_url = _resolve_proxy_url(proxy_url)

    if not force_browser:
        proxy_data = load_cli_proxy_data(email, proxy_dir)
        if proxy_data.get("access_token"):
            if is_cli_proxy_access_token_valid(proxy_data, proxy_url=proxy_url):
                rt.log(f"{email}: cli-proxy access_token 仍有效，跳过登录")
                return merge_cli_proxy_into_account(dict(account), proxy_data), "skip_valid"

            rt.log(f"{email}: cli-proxy access_token 已失效，尝试 refresh_token API…")
            merged = merge_cli_proxy_into_account(dict(account), proxy_data)
            refreshed = rt.try_refresh_via_refresh_token(merged, proxy_dir=proxy_dir)
            if refreshed is not None:
                return refreshed, "refresh_token"

            rt.log(f"{email}: refresh_token 刷新失败，改走网页登录 + OAuth…")

    updated, used_session_fallback = _browser_login_and_merge(
        account,
        auth_path=auth_path,
        headless=headless,
        chrome_profile=chrome_profile,
        use_system_chrome=use_system_chrome,
        use_main_chrome_profile=use_main_chrome_profile,
        cdp_url=cdp_url,
    )
    method = "browser_session" if used_session_fallback else "browser"
    return updated, method


def main() -> int:
    parser = argparse.ArgumentParser(
        description="先 ChatGPT 网页登录，再本地 OAuth 回调完成 Codex 授权并回写 token",
    )
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
    parser.add_argument(
        "--proxy-url",
        default=os.environ.get("QUOTA_PROXY_URL", DEFAULT_QUOTA_PROXY),
        help="探测 access_token 时使用的代理，传 none 表示直连",
    )
    parser.add_argument(
        "--force-browser",
        action="store_true",
        help="跳过 token 有效性检查与 refresh_token API，强制网页登录",
    )
    args = parser.parse_args()

    chrome_profile = (
        rt._system_chrome_user_data() if args.use_main_chrome_profile else args.chrome_profile
    )
    use_system_chrome = not args.no_system_chrome
    cdp_url = args.cdp.strip() or None
    auth_path = args.auth_file.expanduser()

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
        auth_path=auth_path,
        config_path=rt.CONFIG_PATH,
    )
    rt._install_codex_restore_hooks(codex_backup)

    ok = fail = abandoned = abnormal = skipped = api_ok = browser_ok = session_ok = 0
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
                    updated, method = refresh_one_via_web_then_codex(
                        acc,
                        auth_path=auth_path,
                        headless=args.headless,
                        chrome_profile=chrome_profile,
                        use_system_chrome=use_system_chrome,
                        use_main_chrome_profile=args.use_main_chrome_profile,
                        cdp_url=cdp_url,
                        proxy_dir=args.cli_proxy_dir,
                        proxy_url=args.proxy_url,
                        force_browser=args.force_browser,
                    )
                    rows[index[key]] = updated
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
                    if method == "skip_valid":
                        skipped += 1
                        rt.log(f"成功 {acc['email']} (token 仍有效，已更新刷新状态)")
                    elif method == "refresh_token":
                        api_ok += 1
                        rt.log(f"成功 {acc['email']} (refresh_token API)")
                    elif method == "browser_session":
                        session_ok += 1
                        rt.log(f"成功 {acc['email']} (网页登录 + session API，OAuth 遇手机验证)")
                    else:
                        browser_ok += 1
                        rt.log(f"成功 {acc['email']} (网页登录 + OAuth)")
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

        rt.log(
            f"完成: 成功 {ok} (有效跳过 {skipped} API刷新 {api_ok} 浏览器 {browser_ok} "
            f"session回退 {session_ok}) 废弃 {abandoned} 异常 {abnormal} 失败 {fail}"
        )
        return 0 if fail == 0 else 2
    except KeyboardInterrupt:
        rt._handle_interrupt()


if __name__ == "__main__":
    raise SystemExit(main())
