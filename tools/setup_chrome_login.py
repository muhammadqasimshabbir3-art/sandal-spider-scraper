#!/usr/bin/env python3
"""
One-time Chrome profile setup for bot-protected sites.

1. Opens Chrome with a persistent profile (chrome_profile/).
2. You log into Google with your own credentials.
3. Visit Nordstrom / Farfetch / Zappos / Selle Sandals
   (tabs are opened for you — pass any checks if prompted).
4. Press Enter in this terminal to export cookies → cookies/session.json.

Close any other Chrome windows using this profile before running.

Usage:
    cd sandal-spider-scraper
    python tools/setup_chrome_login.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# Allow importing project package when run as a script
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from manual_scraper_ext.chrome_cookies import (
    DEFAULT_COOKIES_FILE,
    DEFAULT_PROFILE_DIR,
    TARGET_SITES,
    load_cookies,
    merge_cookies,
    save_cookies,
    selenium_cookies_to_dict,
)
from manual_scraper_ext.uc_chrome import build_undetected_chrome


def build_driver(profile: Path):
    """Same undetected Chrome + profile as Farfetch/Nordstrom spiders."""
    return build_undetected_chrome(profile, headless=False, block_heavy=False)


def main() -> int:
    profile = DEFAULT_PROFILE_DIR
    cookies_path = DEFAULT_COOKIES_FILE

    print("=" * 60)
    print("Chrome profile login setup (undetected Chrome)")
    print("=" * 60)
    print(f"Profile dir : {profile}")
    print(f"Cookies file: {cookies_path}")
    print()
    print("IMPORTANT: Close other Chrome windows that use this profile.")
    print("Nordstrom requires a US VPN — connect it before continuing.")
    print()
    print("Steps:")
    print("  1. Log into Google in the opened browser (your credentials).")
    print("  2. On each site tab, dismiss cookies / pass any human checks.")
    print("  3. Return here and press ENTER to save cookies.")
    print("=" * 60)

    driver = build_driver(profile)
    all_cookies: list[dict] = load_cookies(cookies_path)

    try:
        for site in TARGET_SITES:
            name, url = site["name"], site["url"]
            print(f"\nOpening {name}: {url}")
            try:
                driver.get(url)
                time.sleep(2.5)
                raw = driver.get_cookies()
                all_cookies = merge_cookies(
                    all_cookies, selenium_cookies_to_dict(raw)
                )
                print(f"  Collected {len(raw)} cookies from {name}")
            except Exception as exc:
                print(f"  Warning: failed to open {name}: {exc}")

        print("\n" + "=" * 60)
        print("Browser is ready. Finish Google login + site checks now.")
        print("Then press ENTER here to export the final cookie jar...")
        print("=" * 60)
        try:
            input()
        except EOFError:
            print("No stdin — waiting 30s then exporting...")
            time.sleep(30)

        # Re-visit each site briefly to refresh cookies after login
        for site in TARGET_SITES:
            url = site["url"]
            try:
                driver.get(url)
                time.sleep(1.5)
                all_cookies = merge_cookies(
                    all_cookies, selenium_cookies_to_dict(driver.get_cookies())
                )
            except Exception as exc:
                print(f"  Refresh skip ({site['name']}): {exc}")

        path = save_cookies(all_cookies, cookies_path)
        print(f"\nDone. Saved {len(all_cookies)} cookies → {path}")
        print("You can now run: scrapy crawl nordstrom")
        print("                scrapy crawl farfetch")
        print("                scrapy crawl zappos")
        print("                scrapy crawl selle-sandals")
        print("                scrapy crawl all")
        return 0
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
