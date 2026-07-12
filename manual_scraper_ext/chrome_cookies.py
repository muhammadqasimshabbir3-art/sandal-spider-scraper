"""
chrome_cookies.py
~~~~~~~~~~~~~~~~~
Load / save Chrome session cookies for Scrapy and Selenium.

Cookies are exported by ``tools/setup_chrome_login.py`` into
``cookies/session.json`` after you log into Google and visit target sites
in a persistent Chrome profile.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Default paths relative to the project root (sandal-spider-scraper/)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE_DIR = PROJECT_ROOT / "chrome_profile"
DEFAULT_COOKIES_FILE = PROJECT_ROOT / "cookies" / "session.json"

TARGET_SITES: list[dict[str, str]] = [
    {"name": "Google", "url": "https://accounts.google.com/"},
    {"name": "Nordstrom", "url": "https://www.nordstrom.com/browse/men/shoes/sandals"},
    {
        "name": "Farfetch",
        "url": "https://www.farfetch.com/us/",
    },
    {"name": "Zappos", "url": "https://www.zappos.com/men-sandals/CK_XARC51wHAAQLiAgMBAhg.zso"},
    {"name": "Selle Sandals", "url": "https://www.selle-sandals.com/collections/all"},
]


def profile_dir(settings=None) -> Path:
    if settings is not None:
        raw = settings.get("CHROME_PROFILE_DIR")
        if raw:
            return Path(raw)
    return DEFAULT_PROFILE_DIR


def cookies_file(settings=None) -> Path:
    if settings is not None:
        raw = settings.get("CHROME_COOKIES_FILE")
        if raw:
            return Path(raw)
    return DEFAULT_COOKIES_FILE


def load_cookies(path: Path | None = None) -> list[dict[str, Any]]:
    """Load cookie list from session JSON. Returns [] if missing/invalid."""
    path = path or DEFAULT_COOKIES_FILE
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load cookies from %s: %s", path, exc)
        return []
    if isinstance(data, dict):
        cookies = data.get("cookies") or []
    elif isinstance(data, list):
        cookies = data
    else:
        cookies = []
    return [c for c in cookies if isinstance(c, dict) and c.get("name")]


def save_cookies(cookies: list[dict[str, Any]], path: Path | None = None) -> Path:
    """Persist cookies to session JSON (creates parent dirs)."""
    path = path or DEFAULT_COOKIES_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    # Deduplicate by (domain, name, path)
    seen: set[tuple] = set()
    unique: list[dict[str, Any]] = []
    for c in cookies:
        key = (c.get("domain"), c.get("name"), c.get("path", "/"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)
    payload = {"cookies": unique}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info("Saved %d cookies to %s", len(unique), path)
    return path


def merge_cookies(
    existing: list[dict[str, Any]],
    new_cookies: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge cookie lists; newer entries win on (domain, name, path)."""
    by_key: dict[tuple, dict[str, Any]] = {}
    for c in existing + new_cookies:
        key = (c.get("domain"), c.get("name"), c.get("path", "/"))
        by_key[key] = c
    return list(by_key.values())


def selenium_cookies_to_dict(raw: list[dict]) -> list[dict[str, Any]]:
    """Normalize Selenium get_cookies() entries for JSON + Scrapy."""
    out: list[dict[str, Any]] = []
    for c in raw:
        entry: dict[str, Any] = {
            "name": c.get("name"),
            "value": c.get("value"),
            "domain": c.get("domain"),
            "path": c.get("path") or "/",
        }
        if c.get("expiry") is not None:
            entry["expires"] = c["expiry"]
        if "secure" in c:
            entry["secure"] = bool(c["secure"])
        if "httpOnly" in c:
            entry["httpOnly"] = bool(c["httpOnly"])
        if c.get("sameSite"):
            entry["sameSite"] = c["sameSite"]
        out.append(entry)
    return out


def cookie_matches_url(cookie: dict[str, Any], url: str) -> bool:
    """Return True if cookie domain/path applies to url."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    domain = (cookie.get("domain") or "").lstrip(".").lower()
    if not domain:
        return False
    if not (host == domain or host.endswith("." + domain)):
        return False
    path = cookie.get("path") or "/"
    url_path = parsed.path or "/"
    return url_path.startswith(path) if path != "/" else True


def cookies_for_url(cookies: list[dict[str, Any]], url: str) -> list[dict[str, Any]]:
    return [c for c in cookies if cookie_matches_url(c, url)]


def apply_cookies_to_request(request, cookies: list[dict[str, Any]]) -> None:
    """
    Attach matching cookies to a Scrapy Request via the Cookie header / jar.

    Prefer Scrapy's cookiejar by setting request.cookies as a name→value dict
    for cookies that match the request URL.
    """
    matched = cookies_for_url(cookies, request.url)
    if not matched:
        return
    jar: dict[str, str] = {}
    existing = request.cookies
    if isinstance(existing, dict):
        jar.update({str(k): str(v) for k, v in existing.items()})
    for c in matched:
        name = c.get("name")
        value = c.get("value")
        if name is not None and value is not None:
            jar[str(name)] = str(value)
    request.cookies = jar


def inject_into_cookiejar(crawler, cookies: list[dict[str, Any]], url: str) -> None:
    """Push cookies into Scrapy's downloader cookiejar for the given URL."""
    if not crawler or not cookies:
        return
    try:
        from scrapy.http.cookies import CookieJar

        jar = CookieJar()
        # Use a dummy response-like set via scrapy's cookie middleware internals
        # Simplest reliable path: set on engine downloader middleware cookiejar
        mw = None
        for middleware in getattr(crawler.engine.downloader, "middleware", None) or []:
            pass
        # Fallback: store on crawler for ChromeCookieMiddleware to re-apply
        crawler.chrome_session_cookies = merge_cookies(
            getattr(crawler, "chrome_session_cookies", []) or [],
            cookies,
        )
    except Exception as exc:
        logger.debug("inject_into_cookiejar: %s", exc)
        crawler.chrome_session_cookies = merge_cookies(
            getattr(crawler, "chrome_session_cookies", []) or [],
            cookies,
        )
