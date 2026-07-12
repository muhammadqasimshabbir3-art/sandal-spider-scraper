"""
farfetch.py
~~~~~~~~~~~
Spider for https://www.farfetch.com/ — Men's sandals.

Fast path:
  1. undetected Chrome (headless) + chrome_profile/ for Akamai
  2. Listing pages via Selenium
  3. PDP: load HTML only → pull ALL gallery URLs from JSON-LD
  4. Download each CDN image via HTTP (Chrome cookies) in a background
     thread pool — Selenium moves to the next product immediately
  5. Resume skips products that already have enough images on disk
"""

from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future
from pathlib import Path
from typing import Any, Iterator

import scrapy
from scrapy.http import HtmlResponse
from scrapy import signals

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException

from manual_scraper_ext.base.ecommerce_spider import EcommerceSpider
from manual_scraper_ext.base.parser_utils import (
    extract_next_data,
    first_text,
    clean_price,
)
from manual_scraper_ext.base.image_utils import (
    is_product_image,
    clean_url,
    add_unique,
)
from manual_scraper_ext.base.metadata import safe_name, write_metadata_json
from manual_scraper_ext.chrome_cookies import profile_dir
from manual_scraper_ext.uc_chrome import build_undetected_chrome

_IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


_CDN = ("cdn-images.farfetch-contents.com", "cdn-static.farfetch.net")

_WARM_URL = "https://www.farfetch.com/us/"
_LISTING_PATH = "/shopping/men/sandals-2/items.aspx"

# Prefer US/JP; /pk/ often works for listings from PK IPs (homepage redirect)
_PREFERRED_LOCALES = ("us", "jp")
_LOCALE_FALLBACKS = (
    "us",
    "jp",
    "uk",
    "de",
    "fr",
    "it",
    "es",
    "ca",
    "au",
    "hk",
    "sg",
    "ae",
    "pk",
)
# Prefer not to *choose* these first; still OK if Akamai lands there with products
_BLOCKED_LISTING_LOCALES = frozenset({"in", "ng", "eg"})

# Live PDP gallery (Chrome probe 2026-07-11)
_GALLERY_XPATH = (
    '//div[@id="selected-image"]//img/@src'
    ' | //div[@aria-label="Product images"]//img/@src'
    ' | //button[@data-carousel-item-button]//img/@src'
)
_GALLERY_CSS = (
    "#selected-image img::attr(src), "
    '[aria-label="Product images"] img::attr(src), '
    "button[data-carousel-item-button] img::attr(src)"
)


_PRODUCT_HREF_RE = re.compile(
    r"(?:https?://(?:www\.)?farfetch\.com)?(/[a-z]{2}/shopping/men/[^?\s\"'<>]+-\d+\.aspx)",
    re.I,
)
_PRODUCT_HREF_RE_NO_CC = re.compile(
    r"(?:https?://(?:www\.)?farfetch\.com)?(/shopping/men/[^?\s\"'<>]+-\d+\.aspx)",
    re.I,
)
_PRODUCT_ID_RE = re.compile(r"-(\d+)\.aspx", re.I)
_LOCALE_RE = re.compile(r"farfetch\.com/([a-z]{2})/", re.I)
_CDN_IMG_RE = re.compile(
    r"https://cdn-images\.farfetch-contents\.com/[^\"'\s\\]+?\.(?:jpg|jpeg|png|webp)",
    re.I,
)


def _with_locale(url: str, locale: str) -> str:
    if not url or not locale:
        return url
    if re.search(rf"farfetch\.com/{re.escape(locale)}/", url, re.I):
        return url
    return re.sub(
        r"(https?://(?:www\.)?farfetch\.com)/(?:[a-z]{2}/)?(shopping/)",
        rf"\1/{locale}/\2",
        url,
        count=1,
        flags=re.I,
    )


class FarfetchSpider(EcommerceSpider):
    """Crawls Farfetch men's sandals via undetected Chrome + locale fallback."""

    name = "farfetch"
    brand = "Farfetch"
    category = "Sandals"
    gender = "Men"
    cdn_patterns = _CDN

    # Empty — Scrapy 2.16 ``start()`` would otherwise HTTP-GET these (Akamai 403)
    start_urls: list[str] = []

    custom_settings = {
        **EcommerceSpider.custom_settings,
        # Requests are synthetic data: URLs — delay only paces the scheduler
        "DOWNLOAD_DELAY": 0.15,
        "RANDOMIZE_DOWNLOAD_DELAY": False,
        "CONCURRENT_REQUESTS": 1,  # single shared Chrome driver
        "AUTOTHROTTLE_ENABLED": False,
        "DOWNLOAD_HANDLERS": {},
        # Accidental HTTP to farfetch.com hangs ~180s; fail fast if anything slips through
        "DOWNLOAD_TIMEOUT": 30,
        "RETRY_TIMES": 1,
        "SELENIUM_CHALLENGE_ENABLED": False,
        "BROWSER_RENDERING_ENABLED": False,
        # Headless by default (override: -s FARFETCH_SELENIUM_HEADLESS=False)
        "FARFETCH_SELENIUM_HEADLESS": True,
        # PDP: only wait for JSON-LD / title (not gallery paint)
        "FARFETCH_PDP_WAIT": 4,
        # Parallel HTTP downloads of CDN images (Chrome cookies)
        "FARFETCH_CDN_WORKERS": 8,
        # Skip PDP if disk already has a typical gallery
        "FARFETCH_RESUME_MIN_IMAGES": 4,
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._driver = None
        self._warmed = False
        self._cookie_sess = None
        self._cookie_ua = ""
        self._cookie_lock = threading.Lock()
        self._dl_pool: ThreadPoolExecutor | None = None
        self._dl_futures: list[Future] = []
        preferred = (
            str(kwargs.get("locale") or "").strip().lower()
            or str(getattr(self, "locale", "") or "").strip().lower()
        )
        if preferred in _BLOCKED_LISTING_LOCALES:
            preferred = ""
        self._locale: str | None = (
            preferred if preferred in ("us", "jp", "uk", "de", "fr") else "us"
        )
        self._seen_product_ids: set[str] = set()
        self._done_product_ids: set[str] = set()
        self._skipped_resume = 0

    def _loc_url(self, url: str) -> str:
        loc = self._locale or "us"
        if loc in _BLOCKED_LISTING_LOCALES:
            loc = "us"
        return _with_locale(url, loc)

    def _listing_url(self, locale: str | None = None) -> str:
        loc = locale or self._locale or "us"
        if loc in _BLOCKED_LISTING_LOCALES:
            loc = "us"
        return f"https://www.farfetch.com/{loc}{_LISTING_PATH}"

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        crawler.signals.connect(spider._spider_closed, signal=signals.spider_closed)
        return spider

    def _spider_closed(self, spider=None, reason=None):
        if self._dl_futures:
            self.logger.info(
                "[Farfetch] Waiting for %d background image download(s)…",
                len(self._dl_futures),
            )
            done_ok = 0
            for fut in self._dl_futures:
                try:
                    if fut.result(timeout=60):
                        done_ok += 1
                except Exception:
                    pass
            self.logger.info(
                "[Farfetch] Background downloads finished (%d ok)", done_ok
            )
        if self._dl_pool is not None:
            self._dl_pool.shutdown(wait=False)
            self._dl_pool = None
        if self._skipped_resume:
            self.logger.info(
                "[Farfetch] Resume skipped %d already-downloaded product(s)",
                self._skipped_resume,
            )
        self._quit_driver()
        self._cookie_sess = None

    async def start(self):
        """Scrapy 2.13+ entrypoint.

        Do **not** yield https://www.farfetch.com — Akamai hangs Scrapy's HTTP
        downloader for minutes with no logs (looks "stuck" after Telnet).
        Open the listing in Chrome here, then yield PDP/pagination seeds.
        """
        self._done_product_ids = self._load_done_product_ids()
        if self._done_product_ids:
            self.logger.info(
                "[Farfetch] Resume: %d product(s) already on disk — will skip them",
                len(self._done_product_ids),
            )
        self.logger.info(
            "[Farfetch] Bootstrap: launching Chrome for listing "
            "(headless=%s, no Scrapy HTTP to farfetch.com)",
            self.settings.getbool("FARFETCH_SELENIUM_HEADLESS", True),
        )
        rendered = self._open_listing()
        if rendered is None:
            self.logger.error(
                "[Farfetch] Could not open sandals listing on any locale "
                "(Access Denied / Akamai). Try: VPN, "
                "python tools/setup_chrome_login.py, then re-run. "
                "Note: /pk/ homepage can work while /pk/ category pages are blocked."
            )
            return
        rendered.meta["farfetch_page"] = 1
        rendered.meta["farfetch_rendered"] = True
        for result in self.parse(rendered):
            yield result

    def start_requests(self) -> Iterator[scrapy.Request]:
        # Scrapy 2.13+ ignores this when ``start()`` exists; kept for older Scrapy.
        yield scrapy.Request(
            "data:text/html,<html><body>farfetch-bootstrap</body></html>",
            callback=self._bootstrap,
            dont_filter=True,
        )

    def _bootstrap(self, _response) -> Iterator:
        """Legacy data: callback (older Scrapy / start_requests path)."""
        self._done_product_ids = self._load_done_product_ids()
        if self._done_product_ids:
            self.logger.info(
                "[Farfetch] Resume: %d product(s) already on disk — will skip them",
                len(self._done_product_ids),
            )
        rendered = self._open_listing()
        if rendered is None:
            self.logger.error(
                "[Farfetch] Could not open sandals listing on any locale "
                "(Access Denied / Akamai). Try: VPN, "
                "python tools/setup_chrome_login.py, then re-run. "
                "Note: /pk/ homepage can work while /pk/ category pages are blocked."
            )
            return
        rendered.meta["farfetch_page"] = 1
        rendered.meta["farfetch_rendered"] = True
        yield from self.parse(rendered)

    def _bootstrap_page(self, response) -> Iterator:
        url = response.meta.get("farfetch_list_url") or self._listing_url()
        page = int(response.meta.get("farfetch_page") or 1)
        rendered = self._selenium_fetch(
            url, wait_seconds=14, wait_for_products=True
        )
        if rendered is None or self._is_blocked(rendered):
            self.logger.error("[Farfetch] Selenium failed for page %s", page)
            return
        rendered.meta["farfetch_page"] = page
        rendered.meta["farfetch_rendered"] = True
        yield from self.parse(rendered)

    def _open_listing(self) -> HtmlResponse | None:
        """Warm session, then open listing on US/JP (never /pk/)."""
        driver = self._get_driver()
        self._ensure_locale(driver)

        # Always try US then JP first — ignore warm redirect to /pk/
        candidates: list[str] = []
        for loc in _PREFERRED_LOCALES:
            if loc not in candidates:
                candidates.append(loc)
        if self._locale and self._locale not in _BLOCKED_LISTING_LOCALES:
            if self._locale not in candidates:
                candidates.append(self._locale)
        for loc in _LOCALE_FALLBACKS:
            if loc not in _BLOCKED_LISTING_LOCALES and loc not in candidates:
                candidates.append(loc)

        for loc in candidates:
            url = self._listing_url(loc)
            self.logger.info("[Farfetch] Trying listing locale=/%s/ → %s", loc, url)
            rendered = self._selenium_fetch(
                url, wait_seconds=14, wait_for_products=True, force_locale=loc
            )
            if rendered is None:
                continue
            if self._is_blocked(rendered):
                self.logger.warning(
                    "[Farfetch] Access Denied on /%s/ listing — trying next locale",
                    loc,
                )
                continue
            products = self._extract_product_urls(rendered)
            if not products:
                self.logger.warning(
                    "[Farfetch] /%s/ listing loaded but 0 products (len=%d)",
                    loc,
                    len(rendered.text or ""),
                )
                continue
            self._locale = loc
            self.logger.info(
                "[Farfetch] Using locale=/%s/ (%d products on page 1)",
                loc,
                len(products),
            )
            return rendered

        return None

    def _ensure_locale(self, driver) -> None:
        """Warm /us/ for cookies; do not adopt /pk/ as crawl locale."""
        if getattr(self, "_warmed", False):
            return
        try:
            driver.get(_WARM_URL)
        except TimeoutException:
            self.logger.debug("[Farfetch] warm page load timed out (continuing)")
        time.sleep(1.5)
        self._dismiss_cookies(driver)
        time.sleep(0.3)
        current = driver.current_url or ""
        m = _LOCALE_RE.search(current)
        landed = m.group(1).lower() if m else "us"
        self._warmed = True
        # Keep preferred US/JP even if Akamai redirects homepage to /pk/
        if self._locale in _BLOCKED_LISTING_LOCALES or not self._locale:
            self._locale = "us"
        self.logger.info(
            "[Farfetch] Warmed %s → landed=/%s/; crawl locale=/%s/ (skip /pk/ listings)",
            _WARM_URL,
            landed,
            self._locale,
        )
    def parse(self, response) -> Iterator:
        page_num = int(response.meta.get("farfetch_page") or 1)

        if not response.meta.get("farfetch_rendered"):
            rendered = self._selenium_fetch(
                self._loc_url(response.url), wait_seconds=14, wait_for_products=True
            )
            if rendered is not None:
                response = rendered

        if self._is_blocked(response):
            self.logger.error(
                "[Farfetch] Access Denied at %s (title=%r). "
                "Category pages on /pk/ are often blocked even when the homepage works. "
                "Spider will try other locales on next run; or use VPN + "
                "tools/setup_chrome_login.py",
                response.url,
                (response.css("title::text").get() or "").strip(),
            )
            return

        product_urls = self._extract_product_urls(response)
        fresh: list[str] = []
        for url in product_urls:
            pid = self._product_id(url)
            if not pid or pid in self._seen_product_ids:
                continue
            self._seen_product_ids.add(pid)
            fresh.append(url)

        self.logger.info(
            "[Farfetch] Listing page %d: %d product URL(s) (%d new) at %s",
            page_num,
            len(product_urls),
            len(fresh),
            response.url,
        )
        if not product_urls:
            self.logger.warning(
                "[Farfetch] No products on page %d — stopping. (len=%d, title=%r)",
                page_num,
                len(response.text or ""),
                (response.css("title::text").get() or "").strip(),
            )
            return

        queued = 0
        page_skipped = 0
        for url in fresh:
            pdp = self._loc_url(response.urljoin(url))
            pid = self._product_id(pdp)
            if pid and pid in self._done_product_ids:
                self._skipped_resume += 1
                page_skipped += 1
                continue
            queued += 1
            yield scrapy.Request(
                f"data:text/html,farfetch-pdp-{pid}",
                callback=self.parse_product,
                meta={"farfetch_pdp_url": pdp, "dont_cache": True},
                dont_filter=True,
            )
        if page_skipped:
            self.logger.info(
                "[Farfetch] Page %d: queued %d PDP(s), resume-skipped %d",
                page_num,
                queued,
                page_skipped,
            )

        next_url = self._next_page_url(response, page_num)
        if next_url and len(product_urls) >= 12 and page_num < 40:
            yield scrapy.Request(
                f"data:text/html,farfetch-page-{page_num + 1}",
                callback=self._bootstrap_page,
                meta={
                    "farfetch_page": page_num + 1,
                    "farfetch_list_url": self._loc_url(next_url),
                },
                dont_filter=True,
            )
        else:
            self.logger.info(
                "[Farfetch] Stopping pagination after page %d "
                "(products=%d, next=%s, seen=%d)",
                page_num,
                len(product_urls),
                bool(next_url),
                len(self._seen_product_ids),
            )

    def parse_product(self, response) -> Iterator:
        pdp_url = response.meta.get("farfetch_pdp_url") or response.url
        pid_early = self._product_id(pdp_url)
        if pid_early and pid_early in self._done_product_ids:
            self._skipped_resume += 1
            return

        pdp_wait = int(self.settings.getint("FARFETCH_PDP_WAIT", 4) or 4)
        # Fast path: load PDP HTML only — image URLs come from JSON-LD
        rendered = self._selenium_fetch(
            self._loc_url(pdp_url),
            wait_seconds=pdp_wait,
            wait_for_jsonld=True,
        )
        if rendered is not None:
            response = rendered

        if self._is_blocked(response):
            self.logger.warning("[Farfetch] Blocked PDP: %s", pdp_url)
            return

        ld = self._json_ld_product(response)
        next_data = extract_next_data(response)
        product = (
            self._next_data_deep(
                next_data, "props", "pageProps", "initialState", "pdp", "product"
            )
            or self._next_data_deep(next_data, "props", "pageProps", "productData")
            or self._next_data_deep(next_data, "props", "pageProps", "product")
            or {}
        )

        brand = ""
        if isinstance(ld.get("brand"), dict):
            brand = (ld["brand"].get("name") or "").strip()
        brand_data = product.get("brand") or {}
        if not brand:
            brand = (
                brand_data.get("name", "")
                if isinstance(brand_data, dict)
                else str(brand_data)
            ).strip()
        if not brand:
            crumbs = [
                t.strip()
                for t in response.css(
                    'nav[data-component="Breadcrumbs"] a::text, '
                    '[data-component="Breadcrumb"]::text'
                ).getall()
                if t and t.strip() and t.strip().lower() not in {
                    "men home", "men", "shoes", "sandals",
                    "flip-flops & slides", "home",
                }
            ]
            brand = crumbs[0] if crumbs else ""
        brand = brand or self.brand

        short = first_text(
            ld.get("name", ""),
            product.get("name", ""),
            product.get("shortDescription", ""),
            response.css('[data-testid="product-short-description"]::text').get(),
        )
        og = (
            response.css('meta[property="og:title"]::attr(content)').get() or ""
        ).strip()
        og_title, og_color = self._split_og_title(og)

        title = first_text(
            f"{brand} {short}".strip() if short else "",
            og_title,
            product.get("name", ""),
            response.css('[data-component="ProductName"]::text').get(),
        )
        if not title or title.strip() in ("\xa0", "&nbsp;"):
            self.logger.warning("No title on %s", response.url)
            return

        sku = str(
            ld.get("productGroupID")
            or product.get("id")
            or product.get("styleId")
            or self._product_id(response.url)
            or ""
        )

        price = self._price_from_ld(ld) or clean_price(
            first_text(
                response.css('meta[property="og:price:amount"]::attr(content)').get(),
                response.css(
                    '[data-component="PriceCallout"] '
                    '[data-component="PriceFinal"]::text'
                ).get(),
                response.css('[data-component="PriceFinal"]::text').get(),
            )
        )

        avail_raw = ""
        for variant in ld.get("hasVariant") or []:
            if isinstance(variant, dict):
                offers = variant.get("offers") or {}
                avail_raw = str(offers.get("availability") or "")
                if avail_raw:
                    break
        availability = (
            "Out of Stock"
            if "OutOfStock" in avail_raw
            else (
                "In Stock" if product.get("isAvailable", True) else "Out of Stock"
            )
        )

        color = first_text(
            str(ld.get("color") or ""),
            og_color,
            self._color_from_alt(response),
        ) or "Default"

        # Collect ALL gallery URLs from JSON-LD / DOM — no carousel clicking
        all_images = self._farfetch_images(product, response, ld=ld, pid=sku)
        if len(all_images) < 2:
            # Cheap fallback: regex on page HTML (still no Selenium clicks)
            live = self._collect_gallery_from_html(response.text or "", sku)
            if len(live) > len(all_images):
                all_images = live

        json_colors = self._farfetch_colors(product)
        if json_colors:
            colors = json_colors
        else:
            colors = [(color, "", {"_urls": all_images})]

        self.logger.info(
            "[Farfetch] %s (%s / %s) — %d image URLs, %d colours on %s",
            title,
            brand,
            color,
            len(all_images),
            len(colors),
            response.url,
        )

        product_url = response.url
        yielded_any = False
        for color_name, color_id, variant_dict in colors:
            color_urls: list[str] = []
            color_seen: set[str] = set()
            for img in variant_dict.get("_urls") or []:
                add_unique(color_urls, img, color_seen)
            for img in self._farfetch_color_images(variant_dict, response):
                add_unique(color_urls, img, color_seen)
            if not color_urls:
                for img in all_images:
                    add_unique(color_urls, img, color_seen)
            color_urls = color_urls[:16]
            if not color_urls:
                continue

            existing_n = self._folder_image_count(brand, title, color_name or "Default")
            min_done = int(self.settings.getint("FARFETCH_RESUME_MIN_IMAGES", 4) or 4)
            if existing_n >= max(len(color_urls), min_done):
                self.logger.info(
                    "[Farfetch] Skip colour (already have %d imgs) %s / %s / %s",
                    existing_n,
                    brand,
                    title,
                    color_name,
                )
                yielded_any = True
                continue

            yielded_any = True
            # Enqueue HTTP downloads (Chrome cookies); do not block Selenium
            saved = self._enqueue_cdn_downloads(
                color_urls,
                brand,
                title,
                color_name or "Default",
                product_url=product_url,
            )
            item = self.make_item(
                product_name=title,
                product_url=product_url,
                image_urls=[],
                color=color_name or "Default",
                sku=sku,
                variant=color_id,
                price=price,
                availability=availability,
            )
            item["brand"] = brand
            item["meta"]["brand"] = brand
            item["meta"]["images"] = saved
            item["images"] = saved
            yield item

        if not yielded_any and all_images:
            saved = self._enqueue_cdn_downloads(
                all_images[:16], brand, title, color, product_url=product_url
            )
            item = self.make_item(
                product_name=title,
                product_url=product_url,
                image_urls=[],
                color=color,
                sku=sku,
                price=price,
                availability=availability,
            )
            item["brand"] = brand
            item["meta"]["brand"] = brand
            item["meta"]["images"] = saved
            item["images"] = saved
            yield item

        if sku or pid_early:
            self._done_product_ids.add(sku or pid_early)

    # ── Chrome ───────────────────────────────────────────────────────────────

    def _get_driver(self):
        if self._driver is not None:
            try:
                _ = self._driver.current_url
                return self._driver
            except Exception:
                self._quit_driver()

        profile = profile_dir(self.settings)
        headless = bool(self.settings.getbool("FARFETCH_SELENIUM_HEADLESS", True))
        self._driver = build_undetected_chrome(
            profile, headless=headless, block_heavy=True, log=self.logger
        )
        return self._driver

    def _quit_driver(self) -> None:
        if self._driver is None:
            return
        try:
            self._driver.quit()
        except Exception:
            pass
        self._driver = None
        self._cookie_sess = None

    def _dismiss_cookies(self, driver) -> None:
        for sel in (
            "button#onetrust-accept-btn-handler",
            'button[id*="accept" i]',
            '[data-testid="consent-accept"]',
        ):
            try:
                btns = driver.find_elements(By.CSS_SELECTOR, sel)
                if btns:
                    btns[0].click()
                    time.sleep(0.3)
                    return
            except Exception:
                continue

    def _selenium_fetch(
        self,
        url: str,
        wait_seconds: int = 6,
        wait_for_products: bool = False,
        wait_for_gallery: bool = False,
        wait_for_jsonld: bool = False,
        force_locale: str | None = None,
    ) -> HtmlResponse | None:
        try:
            driver = self._get_driver()
            self._ensure_locale(driver)
            loc = force_locale or self._locale or "us"
            if loc in _BLOCKED_LISTING_LOCALES:
                loc = "us"
            target = _with_locale(url, loc)
            try:
                driver.get(target)
            except TimeoutException:
                self.logger.debug(
                    "[Farfetch] page load timed out (continuing) %s", target
                )
            time.sleep(0.25)
            self._dismiss_cookies(driver)

            current = driver.current_url or ""
            m = _LOCALE_RE.search(current)
            if m:
                landed = m.group(1).lower()
                if landed not in _BLOCKED_LISTING_LOCALES and force_locale:
                    self._locale = force_locale
                elif (
                    landed not in _BLOCKED_LISTING_LOCALES
                    and landed != (self._locale or "")
                    and not force_locale
                ):
                    self._locale = landed
                    self.logger.info("[Farfetch] Locale now /%s/", self._locale)

            if wait_for_products:
                try:
                    WebDriverWait(driver, max(wait_seconds, 10)).until(
                        EC.any_of(
                            EC.presence_of_element_located(
                                (By.CSS_SELECTOR, 'li[data-testid="productCard"]')
                            ),
                            EC.presence_of_element_located(
                                (By.CSS_SELECTOR, 'a[href*="/shopping/men/"][href*=".aspx"]')
                            ),
                            EC.title_contains("Access Denied"),
                        )
                    )
                except Exception:
                    self.logger.warning(
                        "[Farfetch] Timed out waiting for products on %s", target
                    )
                time.sleep(0.6)
                for y in (900, 1800, 2700):
                    try:
                        driver.execute_script(f"window.scrollTo(0, {y});")
                        time.sleep(0.3)
                    except Exception:
                        break
            elif wait_for_jsonld:
                # Optimal: JSON-LD has full gallery URLs — no need to wait for imgs
                try:
                    WebDriverWait(driver, wait_seconds).until(
                        EC.any_of(
                            EC.presence_of_element_located(
                                (
                                    By.CSS_SELECTOR,
                                    'script[type="application/ld+json"]',
                                )
                            ),
                            EC.presence_of_element_located(
                                (By.CSS_SELECTOR, '[data-component="ProductName"]')
                            ),
                            EC.title_contains("Access Denied"),
                        )
                    )
                except Exception:
                    self.logger.debug(
                        "[Farfetch] JSON-LD wait timed out on %s", target
                    )
                time.sleep(0.15)
            elif wait_for_gallery:
                try:
                    WebDriverWait(driver, wait_seconds).until(
                        EC.any_of(
                            EC.presence_of_element_located(
                                (By.CSS_SELECTOR, "#selected-image img")
                            ),
                            EC.presence_of_element_located(
                                (
                                    By.CSS_SELECTOR,
                                    'script[type="application/ld+json"]',
                                )
                            ),
                            EC.title_contains("Access Denied"),
                        )
                    )
                except Exception:
                    pass
                time.sleep(0.2)
            else:
                time.sleep(min(wait_seconds, 2))

            html = driver.page_source or ""
            resp_url = driver.current_url or target
            landed_m = _LOCALE_RE.search(resp_url)
            if landed_m and landed_m.group(1).lower() in _BLOCKED_LISTING_LOCALES:
                resp_url = _with_locale(resp_url, loc)
            return HtmlResponse(
                url=resp_url,
                body=html.encode("utf-8"),
                encoding="utf-8",
                request=scrapy.Request(target),
            )
        except Exception as exc:
            self.logger.warning("[Farfetch] Selenium fetch failed: %s", exc)
            self._quit_driver()
            return None

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _load_done_product_ids(self) -> set[str]:
        """Scan dataset metadata for Farfetch products that already have images."""
        store = Path(self.settings.get("IMAGES_STORE", "dataset"))
        root = store / "full"
        if not root.is_dir():
            return set()
        min_imgs = int(self.settings.getint("FARFETCH_RESUME_MIN_IMAGES", 4) or 4)
        done: set[str] = set()
        for meta_path in root.rglob("metadata.json"):
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            source = (data.get("source") or "").lower()
            url = (data.get("product_url") or "").lower()
            if source == "farfetch" or "farfetch.com" in url:
                pass
            else:
                continue
            pid = self._product_id(data.get("product_url") or "") or str(
                data.get("sku") or ""
            )
            if not pid:
                continue
            n = sum(
                1
                for p in meta_path.parent.iterdir()
                if p.is_file()
                and p.suffix.lower() in _IMG_EXTS
                and p.stat().st_size > 1000
            )
            if n >= min_imgs:
                done.add(pid)
        return done

    def _folder_path(self, brand: str, product_name: str, color: str) -> Path:
        store = Path(self.settings.get("IMAGES_STORE", "dataset"))
        return (
            store
            / "full"
            / safe_name(brand, 60)
            / safe_name(product_name, 80)
            / safe_name(color, 40)
        )

    def _folder_image_count(
        self, brand: str, product_name: str, color: str
    ) -> int:
        folder = self._folder_path(brand, product_name, color)
        if not folder.is_dir():
            return 0
        return sum(
            1
            for p in folder.iterdir()
            if p.is_file()
            and p.suffix.lower() in _IMG_EXTS
            and p.stat().st_size > 1000
        )

    def _expand_gallery(self) -> None:
        """Click carousel thumbs / next so lazy gallery images load into the DOM."""
        if self._driver is None:
            return
        driver = self._driver
        try:
            thumbs = driver.find_elements(
                By.CSS_SELECTOR, "button[data-carousel-item-button]"
            )
            # Click up to 6 thumbs to force lazy src loads
            for btn in thumbs[:6]:
                try:
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block:'nearest'});", btn
                    )
                    driver.execute_script("arguments[0].click();", btn)
                    time.sleep(0.08)
                except Exception:
                    continue
        except Exception:
            pass
        try:
            for _ in range(2):
                nxt = driver.find_elements(
                    By.CSS_SELECTOR,
                    'button[aria-label*="Next"], '
                    'button[aria-label*="next"], '
                    'button[data-component*="CarouselNext"]',
                )
                if not nxt:
                    break
                try:
                    driver.execute_script("arguments[0].click();", nxt[0])
                    time.sleep(0.1)
                except Exception:
                    break
        except Exception:
            pass

    @staticmethod
    def _product_id(url: str) -> str:
        m = _PRODUCT_ID_RE.search(url or "")
        return m.group(1) if m else ""

    def _is_blocked(self, response) -> bool:
        title = (response.css("title::text").get() or "").strip().lower()
        text = (response.text or "")[:6000].lower()
        url = (response.url or "").lower()
        return (
            "access denied" in title
            or "access denied" in text
            or "errors.edgesuite.net" in text
            or "errors.edgesuite.net" in url
            or len(response.text or "") < 2000
            and "denied" in text
        )

    def _extract_product_urls(self, response) -> list[str]:
        seen: set[str] = set()
        urls: list[str] = []

        def _add(raw: str) -> None:
            if not raw:
                return
            m = _PRODUCT_HREF_RE.search(raw) or _PRODUCT_HREF_RE_NO_CC.search(raw)
            path = m.group(1) if m else ""
            if not path or "items.aspx" in path.lower():
                return
            full = self._loc_url(response.urljoin(path))
            pid = self._product_id(full)
            key = pid or full.split("?")[0].rstrip("/").lower()
            if key not in seen:
                seen.add(key)
                urls.append(full)

        next_data = extract_next_data(response)
        items = (
            self._next_data_deep(
                next_data, "props", "pageProps", "initialState", "listings", "items"
            )
            or self._next_data_deep(next_data, "props", "pageProps", "products")
            or []
        )
        for item in items or []:
            if isinstance(item, dict):
                _add(
                    item.get("url")
                    or (item.get("shortDescription") or {}).get("url")
                    or ""
                )

        # Prefer catalog grid cards (live: li[data-testid=productCard] a ProductCardLink)
        for href in response.css(
            'li[data-testid="productCard"] a[data-component="ProductCardLink"]::attr(href), '
            'li[data-testid="productCard"] a[href*="/shopping/men/"]::attr(href), '
            'ul#catalog-grid a[href*="/shopping/men/"]::attr(href), '
            '[data-testid="productCard"] a[href*=".aspx"]::attr(href)'
        ).getall():
            _add(href)

        if not urls:
            for href in response.css(
                'a[href*="/shopping/men/"][href*=".aspx"]::attr(href)'
            ).getall():
                _add(href)

        for m in _PRODUCT_HREF_RE.finditer(response.text or ""):
            _add(m.group(1))
        for m in _PRODUCT_HREF_RE_NO_CC.finditer(response.text or ""):
            _add(m.group(1))
        return urls

    def _next_page_url(self, response, page_num: int) -> str | None:
        """Use real Next only — live DOM: PaginationNextActionButton (?page=N)."""
        btn = response.css('a[data-component="PaginationNextActionButton"]')
        if btn:
            disabled = (btn.attrib.get("aria-disabled") or "").lower()
            if disabled == "true":
                return None
            href = btn.attrib.get("href") or btn.css("::attr(href)").get()
            if href and "javascript:" not in href.lower():
                return response.urljoin(href)

        next_href = (
            response.css('a[rel="next"]::attr(href)').get()
            or response.xpath(
                '//a[@data-component="PaginationNextActionButton"]/@href'
            ).get()
        )
        if next_href and "javascript:" not in next_href.lower():
            return response.urljoin(next_href)

        want = page_num + 1
        for href in response.css(
            'a[data-component*="Pagination"][href*="page="]::attr(href), '
            'a[href*="page="]::attr(href)'
        ).getall():
            m = re.search(r"[?&]page=(\d+)", href)
            if m and int(m.group(1)) == want:
                return response.urljoin(href)
        return None

    def _dom_color_swatches(self, response) -> list[tuple[str, str, dict]]:
        seen: set[str] = set()
        result: list[tuple[str, str, dict]] = []
        # No cssselect ``i`` flag — match common casings + xpath fallback
        els = response.css(
            '[data-component*="Color"] button[aria-label], '
            '[data-testid*="color"] button[aria-label], '
            '[data-testid*="Color"] button[aria-label], '
            'button[aria-label*="olor"], '
            'button[aria-label*="Colour"], '
            'button[aria-label*="Color"]'
        )
        if not els:
            els = response.xpath(
                '//button[@aria-label['
                'contains(translate(.,"COLOUR","colour"),"colour") or '
                'contains(translate(.,"COLOR","color"),"color")]]'
            )
        for el in els:
            name = (
                el.attrib.get("aria-label")
                or el.xpath("./@aria-label").get()
                or ""
            ).strip()
            if not name or name.lower() in seen:
                continue
            # Skip gallery / nav controls that mention "color" loosely
            low = name.lower()
            if any(x in low for x in ("image", "previous", "next", "zoom", "view")):
                continue
            seen.add(low)
            result.append((name, "", {}))
        return result

    def _collect_gallery_from_driver(self) -> list[str]:
        if self._driver is None:
            return []
        driver = self._driver
        pid = self._product_id(driver.current_url or "")
        imgs: list[str] = []
        seen: set[str] = set()

        def _add_src(src: str) -> None:
            if not src or src.lower().endswith(".svg"):
                return
            # srcset: take largest candidate
            if " " in src and "," in src:
                parts = [p.strip().split(" ")[0] for p in src.split(",") if p.strip()]
                src = parts[-1] if parts else src
            elif " " in src:
                src = src.strip().split(" ")[0]
            if not is_product_image(src, _CDN):
                return
            if pid and not self._pid_in_image_url(src, pid):
                return
            add_unique(imgs, self._upsize_ff(clean_url(src)), seen)

        selectors = (
            "#selected-image img",
            '[aria-label="Product images"] img',
            "button[data-carousel-item-button] img",
        )
        try:
            for sel in selectors:
                for el in driver.find_elements(By.CSS_SELECTOR, sel):
                    for attr in ("src", "data-src", "srcset", "data-srcset"):
                        val = el.get_attribute(attr) or ""
                        if val:
                            _add_src(val)
        except Exception:
            pass

        if len(imgs) < 3:
            try:
                html = driver.page_source or ""
            except Exception:
                html = ""
            for m in _CDN_IMG_RE.finditer(html):
                src = m.group(0).rstrip("\\")
                if (not pid or self._pid_in_image_url(src, pid)) and is_product_image(
                    src, _CDN
                ):
                    add_unique(imgs, self._upsize_ff(clean_url(src)), seen)

        return imgs[:16]

    def _selenium_color_galleries(self) -> list[tuple[str, str, list[str]]]:
        """Click colour swatches on PDP and collect each gallery."""
        if self._driver is None:
            return []
        driver = self._driver
        try:
            buttons = driver.find_elements(
                By.CSS_SELECTOR,
                '[data-component*="Color"] button[aria-label], '
                '[data-testid*="color"] button[aria-label], '
                '[data-testid*="Color"] button[aria-label], '
                'button[aria-label*="Colour"], '
                'button[aria-label*="Color"], '
                'button[aria-label*="colour"], '
                'button[aria-label*="color"]',
            )
        except Exception:
            return []

        by_label: dict[str, object] = {}
        for btn in buttons:
            try:
                label = (btn.get_attribute("aria-label") or "").strip()
            except Exception:
                continue
            if not label or label.lower() in by_label:
                continue
            low = label.lower()
            if any(x in low for x in ("image", "previous", "next", "zoom", "view")):
                continue
            by_label[label] = btn

        if len(by_label) <= 1:
            return []

        results: list[tuple[str, str, list[str]]] = []
        for label, btn in by_label.items():
            try:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});", btn
                )
                time.sleep(0.1)
                try:
                    btn.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", btn)
                time.sleep(0.55)
                self._expand_gallery()
            except Exception:
                continue
            imgs = self._collect_gallery_from_driver()
            if imgs:
                results.append((label, "", imgs))

        return results

    def _collect_gallery_from_html(self, html: str, pid: str = "") -> list[str]:
        """Extract CDN image URLs from raw HTML (no Selenium interaction)."""
        imgs: list[str] = []
        seen: set[str] = set()
        for m in _CDN_IMG_RE.finditer(html or ""):
            src = m.group(0).rstrip("\\")
            if not is_product_image(src, _CDN):
                continue
            if pid and not self._pid_in_image_url(src, pid):
                continue
            add_unique(imgs, self._upsize_ff(clean_url(src)), seen)
        return imgs[:16]

    def _cdn_session(self):
        """Reuse a requests.Session primed with Chrome cookies."""
        import requests

        with self._cookie_lock:
            if self._cookie_sess is not None:
                return self._cookie_sess, self._cookie_ua

            driver = self._get_driver()
            sess = requests.Session()
            try:
                for c in driver.get_cookies():
                    sess.cookies.set(
                        c["name"], c["value"], domain=c.get("domain") or None
                    )
                ua = driver.execute_script("return navigator.userAgent")
                self._cookie_sess = sess
                self._cookie_ua = ua or ""
                return sess, self._cookie_ua
            except Exception as exc:
                self.logger.warning("[Farfetch] Cookie session failed: %s", exc)
                return None, ""

    def _ensure_dl_pool(self) -> ThreadPoolExecutor:
        if self._dl_pool is None:
            workers = max(2, int(self.settings.getint("FARFETCH_CDN_WORKERS", 8) or 8))
            self._dl_pool = ThreadPoolExecutor(max_workers=workers)
            self.logger.info(
                "[Farfetch] Image download pool started (%d workers)", workers
            )
        return self._dl_pool

    def _download_one_image(
        self,
        index: int,
        url: str,
        folder: Path,
        store: Path,
        referer: str,
    ) -> str | None:
        """Download a single CDN image; skip if already on disk."""
        existing = list(folder.glob(f"{index:03d}.*"))
        if existing and existing[0].stat().st_size > 1000:
            return str(existing[0].relative_to(store))

        sess, ua = self._cdn_session()
        if sess is None:
            return None
        try:
            r = sess.get(
                url,
                headers={
                    "User-Agent": ua,
                    "Referer": referer or "https://www.farfetch.com/",
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                },
                timeout=25,
            )
            if r.status_code != 200 or len(r.content) < 800:
                self.logger.warning(
                    "[Farfetch] CDN #%03d HTTP %s (%d bytes)",
                    index,
                    r.status_code,
                    len(r.content),
                )
                return None
            ct = (r.headers.get("Content-Type") or "").lower()
            ext = "jpg"
            if "webp" in ct:
                ext = "webp"
            elif "png" in ct:
                ext = "png"
            out = folder / f"{index:03d}.{ext}"
            out.write_bytes(r.content)
            return str(out.relative_to(store))
        except Exception as exc:
            self.logger.warning("[Farfetch] CDN #%03d failed: %s", index, exc)
            return None

    def _enqueue_cdn_downloads(
        self,
        urls: list[str],
        brand: str,
        product_name: str,
        color: str,
        *,
        product_url: str = "",
    ) -> list[str]:
        """
        Collect image URLs → download each via HTTP (Chrome cookies) in a
        background pool so Selenium can move to the next PDP immediately.
        """
        if not urls:
            return []

        store = Path(self.settings.get("IMAGES_STORE", "dataset"))
        folder = self._folder_path(brand, product_name, color)
        folder.mkdir(parents=True, exist_ok=True)

        try:
            referer = (
                product_url
                or (self._driver.current_url if self._driver else "")
                or "https://www.farfetch.com/"
            )
        except Exception:
            referer = "https://www.farfetch.com/"

        # Refresh cookies once before enqueueing (Akamai)
        self._cdn_session()
        pool = self._ensure_dl_pool()

        saved_now: list[str] = []
        pending = 0
        for i, url in enumerate(urls):
            existing = list(folder.glob(f"{i:03d}.*"))
            if existing and existing[0].stat().st_size > 1000:
                saved_now.append(str(existing[0].relative_to(store)))
                continue
            fut = pool.submit(
                self._download_one_image, i, url, folder, store, referer
            )
            self._dl_futures.append(fut)
            pending += 1
            # Expected path (jpg default); final ext may differ
            saved_now.append(str((folder / f"{i:03d}.jpg").relative_to(store)))

        try:
            write_metadata_json(
                str(folder),
                {
                    "brand": brand,
                    "product_name": product_name,
                    "source": self.name,
                    "gender": getattr(self, "gender", "Men"),
                    "category": getattr(self, "category", "Sandals"),
                    "color": color,
                    "product_url": product_url or referer,
                    "images": saved_now,
                    "image_urls": urls,
                },
            )
        except Exception:
            pass

        self.logger.info(
            "[Farfetch] Queued %d downloads (%d already on disk) → %s",
            pending,
            len(urls) - pending,
            folder,
        )
        return saved_now

    def _farfetch_colors(self, product: dict) -> list[tuple[str, str, dict]]:
        seen: set[str] = set()
        result: list[tuple[str, str, dict]] = []
        for key in ("colors", "variants", "availableColors", "colourOptions"):
            for v in product.get(key) or []:
                if not isinstance(v, dict):
                    continue
                name = (
                    v.get("color")
                    or v.get("name")
                    or v.get("colorName")
                    or v.get("description")
                    or ""
                ).strip()
                cid = str(v.get("id") or v.get("colorId") or "")
                if name and name.lower() not in seen:
                    seen.add(name.lower())
                    result.append((name, cid, v))
        return result

    @staticmethod
    def _upsize_ff(url: str) -> str:
        return re.sub(
            r"_(\d{2,3})\.(jpg|jpeg|png|webp)", r"_1000.\2", url, flags=re.I
        )

    @staticmethod
    def _pid_in_image_url(url: str, pid: str) -> bool:
        if not pid or not url:
            return True
        if f"{pid}_" in url or f"/{pid}/" in url:
            return True
        if pid.isdigit() and len(pid) >= 8:
            chunks = "/".join(pid[i : i + 2] for i in range(0, 8, 2))
            if chunks in url:
                return True
        return False

    @staticmethod
    def _split_og_title(og: str) -> tuple[str, str]:
        """'Prada … flip-flops | Blue | FARFETCH PK' → title, color."""
        if not og:
            return "", ""
        parts = [p.strip() for p in og.split("|") if p.strip()]
        title = parts[0] if parts else og
        color = ""
        for p in parts[1:]:
            if p.upper().startswith("FARFETCH"):
                continue
            # skip category-ish crumbs sometimes present
            if p.lower() in {"sandals", "shoes", "men", "flip-flops & slides"}:
                continue
            color = p
            break
        return title, color

    @staticmethod
    def _color_from_alt(response) -> str:
        alt = (
            response.css("#selected-image img::attr(alt)").get()
            or response.css(
                '[aria-label="Product images"] img::attr(alt)'
            ).get()
            or ""
        )
        # 'Prada … | Blue | Image 1'
        parts = [p.strip() for p in alt.split("|")]
        if len(parts) >= 2 and not parts[1].lower().startswith("image"):
            cand = parts[1]
            if cand.lower() not in {
                "sandals",
                "shoes",
                "flip-flops & slides",
                "men",
            }:
                return cand
        return ""

    def _json_ld_product(self, response) -> dict[str, Any]:
        for raw in response.css(
            'script[type="application/ld+json"]::text'
        ).getall():
            try:
                data = json.loads(raw)
            except Exception:
                continue
            nodes = data if isinstance(data, list) else [data]
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                t = node.get("@type")
                types = t if isinstance(t, list) else [t]
                if any(x in ("Product", "ProductGroup") for x in types):
                    return node
        return {}

    @staticmethod
    def _price_from_ld(ld: dict) -> str:
        for variant in ld.get("hasVariant") or []:
            if not isinstance(variant, dict):
                continue
            offers = variant.get("offers") or {}
            for spec in offers.get("priceSpecification") or []:
                if isinstance(spec, dict) and spec.get("price") is not None:
                    return clean_price(str(spec.get("price")))
            if offers.get("price") is not None:
                return clean_price(str(offers.get("price")))
        return ""

    def _farfetch_images(
        self,
        product: dict,
        response,
        ld: dict | None = None,
        pid: str = "",
    ) -> list[str]:
        """Gallery from JSON-LD + live DOM xpaths; filter by product id; upsize."""
        ld = ld or {}
        pid = pid or self._product_id(response.url)
        seen: set[str] = set()
        urls: list[str] = []

        def _add(src: str) -> None:
            if not src or src.lower().endswith(".svg"):
                return
            # Handle srcset: take largest
            if "," in src and ("w" in src or " " in src):
                parts = [p.strip().split(" ")[0] for p in src.split(",") if p.strip()]
                if parts:
                    src = parts[-1]
            if not is_product_image(src, _CDN):
                return
            if not self._pid_in_image_url(src, pid):
                return
            add_unique(urls, clean_url(response.urljoin(src)), seen)

        # 1) JSON-LD ProductGroup.image (already _1000 on live pages)
        for img in ld.get("image") or []:
            if isinstance(img, dict):
                _add(img.get("contentUrl") or img.get("url") or "")
            elif isinstance(img, str):
                _add(img)

        # 2) Proven gallery xpaths / CSS from Chrome probe
        for src in response.xpath(_GALLERY_XPATH).getall():
            _add(src)
        for src in response.css(
            "#selected-image img::attr(src), "
            "#selected-image img::attr(srcset), "
            "#selected-image img::attr(data-src), "
            '[aria-label="Product images"] img::attr(src), '
            '[aria-label="Product images"] img::attr(srcset), '
            '[aria-label="Product images"] img::attr(data-src), '
            "button[data-carousel-item-button] img::attr(src), "
            "button[data-carousel-item-button] img::attr(srcset), "
            "button[data-carousel-item-button] img::attr(data-src)"
        ).getall():
            _add(src)

        # 3) __NEXT_DATA__ / product dict (often missing on Farfetch)
        for img in product.get("images") or product.get("media") or []:
            src = (
                img.get("url") or img.get("src") or img.get("link") or ""
                if isinstance(img, dict)
                else str(img)
            )
            _add(src)

        # 4) Last resort: CDN regex but still filter by pid
        if len(urls) < 3:
            for m in _CDN_IMG_RE.finditer(response.text or ""):
                _add(m.group(0).rstrip("\\"))

        out: list[str] = []
        out_seen: set[str] = set()
        for u in urls:
            add_unique(out, self._upsize_ff(u), out_seen)
        return out[:16]

    def _farfetch_color_images(self, variant: dict, response) -> list[str]:
        seen: set[str] = set()
        urls: list[str] = []
        pid = self._product_id(response.url)
        for img in variant.get("images") or []:
            src = (
                img.get("url") or img.get("src") or ""
                if isinstance(img, dict)
                else str(img)
            )
            if (
                src
                and is_product_image(src, _CDN)
                and self._pid_in_image_url(src, pid)
            ):
                add_unique(
                    urls, self._upsize_ff(clean_url(response.urljoin(src))), seen
                )
        return urls
