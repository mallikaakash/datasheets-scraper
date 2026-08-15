"""
HTTP helper: curl_cffi plus a one-time headed Chromium bootstrap for Cloudflare.

datasheets.com serves a JS challenge ("Just a moment...") that TLS impersonation
alone cannot pass. A short headed Playwright visit obtains cf_clearance; later
requests reuse those cookies.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from curl_cffi import requests as curl

import config
from utils import ensure_dirs

SESSION_PATH = Path("data/.cf_session.json")
_lock = threading.Lock()


def is_challenge(status: int, html: str | None) -> bool:
    if not html:
        return status in (403, 503)
    # Real catalog pages include Cloudflare scripts; only the interstitial counts.
    if "<title>Just a moment...</title>" in html:
        return True
    return status in (403, 503) and "Just a moment" in html


def _load_session() -> dict:
    if not SESSION_PATH.is_file():
        return {}
    try:
        return json.loads(SESSION_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_session(cookies: dict, ua: str) -> None:
    ensure_dirs(str(SESSION_PATH.parent))
    SESSION_PATH.write_text(
        json.dumps({"cookies": cookies, "ua": ua}, indent=2),
        encoding="utf-8",
    )


def _bootstrap(start_url: str) -> dict:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError(
            "Cloudflare blocked the request. Install a browser helper:\n"
            "  pip install playwright && playwright install chromium"
        ) from e

    # Homepage is enough to mint cf_clearance; category URLs are flakier.
    boot_url = f"{config.BASE_URL}/"
    print(
        "[http] Cloudflare challenge — a Chrome window will open. "
        "Leave it in front until the datasheets.com homepage appears (up to ~90s)."
    )
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(viewport={"width": 1400, "height": 900})
        page = context.new_page()
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page.goto(boot_url, wait_until="domcontentloaded", timeout=90000)
        try:
            page.wait_for_function(
                """() => {
                    const titleOk = document.title && document.title !== 'Just a moment...';
                    const cookieOk = document.cookie.includes('cf_clearance');
                    return titleOk || cookieOk;
                }""",
                timeout=90000,
            )
        except Exception:
            # Cookie can land even if the title wait times out.
            page.wait_for_timeout(3000)

        ua = page.evaluate("navigator.userAgent")
        cookies = {c["name"]: c["value"] for c in context.cookies()}
        html = page.content()
        browser.close()

    if "cf_clearance" not in cookies and is_challenge(200, html):
        raise RuntimeError(
            "Cloudflare challenge did not complete. Keep the Chrome window visible and retry."
        )
    _save_session(cookies, ua)
    print("[http] Saved Cloudflare cookies to data/.cf_session.json")
    return {"cookies": cookies, "ua": ua}


def get(url: str, timeout: float = 30.0):
    """GET url. Bootstraps Cloudflare cookies if a challenge page is returned."""
    last = None
    for attempt in range(3):
        session = _load_session()
        cookies = session.get("cookies") or {}
        ua = session.get("ua")
        headers = dict(config.HEADERS)
        if ua:
            headers["User-Agent"] = ua
        try:
            last = curl.get(
                url,
                impersonate="chrome",
                timeout=timeout,
                cookies=cookies,
                headers=headers,
                allow_redirects=True,
            )
        except Exception:
            last = None
            continue

        html = last.text if last is not None else ""
        if last is not None and not is_challenge(last.status_code, html):
            return last

        with _lock:
            # Another thread may have already refreshed cookies.
            session = _load_session()
            probe = curl.get(
                url,
                impersonate="chrome",
                timeout=timeout,
                cookies=session.get("cookies") or {},
                headers={**config.HEADERS, **({"User-Agent": session["ua"]} if session.get("ua") else {})},
                allow_redirects=True,
            )
            if not is_challenge(probe.status_code, probe.text):
                return probe
            try:
                _bootstrap(url)
            except Exception as e:
                print(f"[http] Cloudflare bootstrap failed: {e}")
                if attempt == 2:
                    return last
                continue
        if attempt == 2:
            return last
    return last
