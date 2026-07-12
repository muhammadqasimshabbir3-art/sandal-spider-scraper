"""
uc_chrome.py
~~~~~~~~~~~~
Shared undetected-chromedriver launcher for Akamai-protected sites
(Farfetch, Nordstrom). Matches the working probe: pin Chrome major version,
reuse ``chrome_profile/``, clear stale Singleton locks.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def chrome_major_version() -> int:
    for cmd in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
        try:
            out = subprocess.check_output(
                [cmd, "--version"], text=True, stderr=subprocess.DEVNULL
            )
            m = re.search(r"(\d+)\.", out)
            if m:
                return int(m.group(1))
        except Exception:
            continue
    return 148


def clear_profile_locks(profile: Path) -> None:
    """Remove stale Chrome Singleton locks so a new session can start."""
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        path = profile / name
        try:
            if path.exists() or path.is_symlink():
                path.unlink()
        except Exception as exc:
            logger.debug("Could not remove %s: %s", path, exc)


def build_undetected_chrome(
    profile: Path,
    *,
    headless: bool = False,
    block_heavy: bool = True,
    # "offscreen" = headed Chrome parked off-display (beats Akamai headless bans)
    # "real" = true --headless=new (faster but Nordstrom → invitation.html)
    headless_style: str = "offscreen",
    log: logging.Logger | None = None,
) -> Any:
    """
    Launch Chrome via undetected-chromedriver (preferred) or plain Selenium.

    ``block_heavy``: after start, CDP-block ads/trackers so ``driver.get``
    returns quickly (image URLs still come from JSON-LD / HTML attrs).

    ``headless`` + ``headless_style="offscreen"`` (default): no real headless
    flag — window is moved off-screen. Sites like Nordstrom block true
    headless but allow this, and it stays out of the way like headless.
    """
    log = log or logger
    profile = Path(profile)
    profile.mkdir(parents=True, exist_ok=True)
    clear_profile_locks(profile)

    version_main = chrome_major_version()
    style = (headless_style or "offscreen").strip().lower()
    use_real_headless = bool(headless) and style == "real"
    use_offscreen = bool(headless) and not use_real_headless

    log.info(
        "[UC] Launching undetected Chrome (profile=%s, chrome=%s, "
        "headless=%s, style=%s) …",
        profile.resolve(),
        version_main,
        headless,
        "real" if use_real_headless else ("offscreen" if use_offscreen else "windowed"),
    )

    try:
        import undetected_chromedriver as uc

        options = uc.ChromeOptions()
        # Don't wait for ads/analytics — that looks "stuck"
        options.page_load_strategy = "eager"
        if use_real_headless:
            # Pass headless=True to uc.Chrome below (applies stealth patches)
            pass
        elif use_offscreen:
            options.add_argument("--window-size=1400,900")
            options.add_argument("--window-position=-2400,-2400")
        else:
            options.add_argument("--start-maximized")
        options.add_argument(f"--user-data-dir={str(profile.resolve())}")
        options.add_argument("--lang=en-US")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-blink-features=AutomationControlled")
        driver = uc.Chrome(
            options=options,
            use_subprocess=True,
            version_main=version_main,
            headless=use_real_headless,
        )
        log.info(
            "[UC] undetected Chrome started (profile=%s, chrome=%s, mode=%s)",
            profile,
            version_main,
            "real-headless"
            if use_real_headless
            else ("offscreen" if use_offscreen else "windowed"),
        )
    except Exception as exc:
        log.warning("[UC] undetected-chromedriver failed (%s); Selenium fallback", exc)
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from webdriver_manager.chrome import ChromeDriverManager

        options = Options()
        options.page_load_strategy = "eager"
        if use_real_headless:
            options.add_argument("--headless=new")
            options.add_argument("--window-size=1400,900")
        elif use_offscreen:
            options.add_argument("--window-size=1400,900")
            options.add_argument("--window-position=-2400,-2400")
        else:
            options.add_argument("--start-maximized")
        options.add_argument(f"--user-data-dir={str(profile.resolve())}")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--lang=en-US")
        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=options,
        )
        log.info("[UC] Selenium Chrome started (profile=%s)", profile)

    # Cap hangs from third-party scripts; callers catch TimeoutException and continue
    driver.set_page_load_timeout(20)
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": (
                    "Object.defineProperty(navigator, 'webdriver', "
                    "{get: () => undefined});"
                )
            },
        )
    except Exception:
        pass

    if use_offscreen:
        try:
            driver.set_window_rect(x=-2400, y=-2400, width=1400, height=900)
        except Exception:
            pass

    if block_heavy:
        _block_heavy_resources(driver, log)

    return driver


def _block_heavy_resources(driver: Any, log: logging.Logger) -> None:
    """Block ads/trackers so pages become interactive faster."""
    try:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd(
            "Network.setBlockedURLs",
            {
                "urls": [
                    "*googlesyndication*",
                    "*doubleclick*",
                    "*google-analytics*",
                    "*googletagmanager*",
                    "*facebook.net*",
                    "*hotjar*",
                    "*optimizely*",
                    "*newrelic*",
                    "*clarity.ms*",
                    "*akamaihd.net/mpvideos*",
                ]
            },
        )
        log.info("[UC] CDP: blocked ads/trackers for faster page loads")
    except Exception as exc:
        log.debug("[UC] CDP block skipped: %s", exc)
