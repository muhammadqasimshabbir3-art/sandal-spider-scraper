"""
nordstrom.py
~~~~~~~~~~~~
Spider for https://www.nordstrom.com/ — Men's sandals and slides.

Uses **undetected-chromedriver** + ``chrome_profile/`` (same path as Farfetch).

Nordstrom Akamai blocks true ``--headless`` (invitation.html). Default is
**offscreen headless**: a real Chrome window parked off-screen — fast like
headless, but the catalog loads. Set ``NORDSTROM_HEADLESS_STYLE=real`` only
to force true headless (will usually get blocked).
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

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
from manual_scraper_ext.base.metadata import safe_name
from manual_scraper_ext.chrome_cookies import profile_dir
from manual_scraper_ext.uc_chrome import build_undetected_chrome


_CDN = (
    "n.nordstrommedia.com",
    "nordstromimage.com",
    "nordstrommedia.com",
)

_IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

_START_URLS = [
    "https://www.nordstrom.com/browse/men/shoes/sandals"
    "?breadcrumb=Home%2FMen%2FShoes%2FSandals%20%26%20Flip-Flops&origin=topnav",
]

_PRODUCT_HREF_RE = re.compile(
    r"(?:https?://(?:www\.)?nordstrom\.com)?(/s/[^?\s\"'<>]+/\d+)",
    re.I,
)

# Prefer real product media; drop tiny icons / sprites
_MEDIA_RE = re.compile(
    r"https://n\.nordstrommedia\.com/it/[^\"'\s\\]+?\.(?:jpg|jpeg|png|webp)",
    re.I,
)


class NordstromSpider(EcommerceSpider):
    """
    Crawls Nordstrom via one persistent Chrome profile session.

    1. Listing + PDP HTML come from Selenium (reused driver).
    2. Paginate only when the current listing had products.
    3. Download gallery images through DatasetImagesPipeline.
    """

    name = "nordstrom"
    brand = "Nordstrom"
    category = "Sandals"
    gender = "Men"
    cdn_patterns = _CDN

    start_urls = _START_URLS

    custom_settings = {
        **EcommerceSpider.custom_settings,
        "DOWNLOAD_DELAY": 0.15,
        "RANDOMIZE_DOWNLOAD_DELAY": False,
        "CONCURRENT_REQUESTS": 1,
        "AUTOTHROTTLE_ENABLED": False,
        "DOWNLOAD_HANDLERS": {},
        "SELENIUM_CHALLENGE_ENABLED": False,
        "BROWSER_RENDERING_ENABLED": False,
        # Offscreen "headless" (true --headless is blocked by Nordstrom Akamai)
        "NORDSTROM_SELENIUM_HEADLESS": True,
        "NORDSTROM_HEADLESS_STYLE": "offscreen",  # or "real"
        "NORDSTROM_PDP_WAIT": 5,
        "NORDSTROM_RESUME_MIN_IMAGES": 4,
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._driver = None
        self._seen_product_ids: set[str] = set()
        self._done_product_ids: set[str] = set()
        self._skipped_resume = 0
        self._forced_headed = False

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        crawler.signals.connect(spider._spider_closed, signal=signals.spider_closed)
        return spider

    def _spider_closed(self, spider=None, reason=None):
        if self._skipped_resume:
            self.logger.info(
                "[Nordstrom] Resume skipped %d already-downloaded product(s)",
                self._skipped_resume,
            )
        self._quit_driver()

    async def start(self):
        """Scrapy 2.13+ entrypoint (``start_requests`` is ignored on 2.13+)."""
        self._done_product_ids = self._load_done_product_ids()
        if self._done_product_ids:
            self.logger.info(
                "[Nordstrom] Resume: %d product(s) already on disk — will skip them",
                len(self._done_product_ids),
            )
        self.logger.info(
            "[Nordstrom] Bootstrap: launching Chrome for listing "
            "(headless=%s, style=%s, no Scrapy HTTP to nordstrom.com)",
            self.settings.getbool("NORDSTROM_SELENIUM_HEADLESS", True),
            self.settings.get("NORDSTROM_HEADLESS_STYLE", "offscreen"),
        )
        url = self.start_urls[0]
        rendered = self._open_listing(url)
        if rendered is None:
            self.logger.error("[Nordstrom] Selenium failed for %s", url)
            return
        rendered.meta["nordstrom_page"] = 1
        rendered.meta["nordstrom_rendered"] = True
        for result in self.parse(rendered):
            yield result

    def start_requests(self) -> Iterator[scrapy.Request]:
        # Scrapy 2.13+ ignores this when ``start()`` exists; kept for older Scrapy.
        yield scrapy.Request(
            "data:text/html,<html><body>nordstrom-bootstrap</body></html>",
            callback=self._bootstrap,
            dont_filter=True,
        )

    def _bootstrap(self, _response) -> Iterator:
        self._done_product_ids = self._load_done_product_ids()
        if self._done_product_ids:
            self.logger.info(
                "[Nordstrom] Resume: %d product(s) already on disk — will skip them",
                len(self._done_product_ids),
            )
        url = self.start_urls[0]
        rendered = self._open_listing(url)
        if rendered is None:
            self.logger.error("[Nordstrom] Selenium failed for %s", url)
            return
        rendered.meta["nordstrom_page"] = 1
        rendered.meta["nordstrom_rendered"] = True
        yield from self.parse(rendered)

    def _open_listing(self, url: str) -> HtmlResponse | None:
        """Fetch listing; if blocked, retry windowed (last resort)."""
        rendered = self._selenium_fetch(
            url, wait_seconds=12, wait_for_products=True
        )
        if rendered is not None and not self._is_blocked(rendered):
            return rendered

        style = str(self.settings.get("NORDSTROM_HEADLESS_STYLE", "offscreen") or "offscreen")
        if style.lower() == "real" and not self._forced_headed:
            # True headless failed — fall back to offscreen (still "headless-like")
            self.logger.warning(
                "[Nordstrom] True headless hit invitation — retrying offscreen mode"
            )
            self._quit_driver()
            self._forced_headed = True  # reuse flag → offscreen via style override
            self.settings.set("NORDSTROM_HEADLESS_STYLE", "offscreen", priority="spider")
            rendered = self._selenium_fetch(
                url, wait_seconds=14, wait_for_products=True
            )
            if rendered is not None and not self._is_blocked(rendered):
                return rendered

        if rendered is not None and self._is_blocked(rendered):
            self.logger.error(
                "[Nordstrom] Blocked at %s\n"
                "  Tip: scrapy crawl nordstrom "
                "-s NORDSTROM_HEADLESS_STYLE=offscreen\n"
                "  Or visible: -s NORDSTROM_SELENIUM_HEADLESS=False",
                rendered.url,
            )
            return None
        return rendered

    def parse_errback(self, failure):
        request = failure.request
        page = int(request.meta.get("nordstrom_page") or 1)
        rendered = self._selenium_fetch(
            request.url, wait_seconds=10, wait_for_products=True
        )
        if rendered is None:
            self.logger.error("[Nordstrom] Selenium failed for %s", request.url)
            return
        rendered.meta["nordstrom_page"] = page
        rendered.meta["nordstrom_rendered"] = True
        yield from self.parse(rendered)

    # ── Listing ──────────────────────────────────────────────────────────────

    def parse(self, response) -> Iterator:
        page_num = int(response.meta.get("nordstrom_page") or 1)

        if not response.meta.get("nordstrom_rendered"):
            rendered = self._selenium_fetch(
                response.url, wait_seconds=10, wait_for_products=True
            )
            if rendered is not None:
                response = rendered

        if self._is_blocked(response):
            self.logger.error(
                "[Nordstrom] Blocked at %s — try "
                "-s NORDSTROM_HEADLESS_STYLE=offscreen or "
                "-s NORDSTROM_SELENIUM_HEADLESS=False",
                response.url,
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
            "[Nordstrom] Listing page %d: %d product URL(s) (%d new) at %s",
            page_num,
            len(product_urls),
            len(fresh),
            response.url,
        )

        if not product_urls:
            self.logger.warning(
                "[Nordstrom] No products on page %d — stopping. "
                "(len=%d, title=%r)",
                page_num,
                len(response.text or ""),
                (response.css("title::text").get() or "").strip(),
            )
            return

        queued = 0
        page_skipped = 0
        for url in fresh:
            pid = self._product_id(url)
            if pid and pid in self._done_product_ids:
                self._skipped_resume += 1
                page_skipped += 1
                continue
            queued += 1
            yield scrapy.Request(
                f"data:text/html,nordstrom-pdp-{pid}",
                callback=self.parse_product,
                meta={"nordstrom_pdp_url": url, "dont_cache": True},
                dont_filter=True,
            )
        if page_skipped:
            self.logger.info(
                "[Nordstrom] Page %d: queued %d PDP(s), resume-skipped %d",
                page_num,
                queued,
                page_skipped,
            )

        next_url = self._next_page_url(response, page_num)
        if next_url and len(product_urls) >= 15 and page_num < 15:
            yield scrapy.Request(
                f"data:text/html,nordstrom-page-{page_num + 1}",
                callback=self._bootstrap_page,
                meta={
                    "nordstrom_page": page_num + 1,
                    "nordstrom_list_url": next_url,
                },
                dont_filter=True,
            )
        elif page_num >= 1:
            self.logger.info(
                "[Nordstrom] Stopping pagination after page %d "
                "(products=%d, next=%s, seen=%d)",
                page_num,
                len(product_urls),
                bool(next_url),
                len(self._seen_product_ids),
            )

    def _bootstrap_page(self, response) -> Iterator:
        url = response.meta.get("nordstrom_list_url") or self.start_urls[0]
        page = int(response.meta.get("nordstrom_page") or 1)
        rendered = self._selenium_fetch(
            url, wait_seconds=10, wait_for_products=True
        )
        if rendered is None:
            self.logger.error("[Nordstrom] Selenium failed for page %s", page)
            return
        rendered.meta["nordstrom_page"] = page
        rendered.meta["nordstrom_rendered"] = True
        yield from self.parse(rendered)

    def product_errback(self, failure):
        request = failure.request
        url = request.meta.get("nordstrom_pdp_url") or request.url
        rendered = self._selenium_fetch(
            url, wait_seconds=5, wait_for_gallery=True
        )
        if rendered is None:
            self.logger.warning("[Nordstrom] PDP Selenium failed: %s", url)
            return
        rendered.meta["nordstrom_pdp_url"] = url
        yield from self.parse_product(rendered)

    def parse_product(self, response) -> Iterator:
        """Parse a Nordstrom product detail page via Selenium HTML."""
        pdp_url = response.meta.get("nordstrom_pdp_url") or response.url
        pid_early = self._product_id(pdp_url)
        if pid_early and pid_early in self._done_product_ids:
            self._skipped_resume += 1
            return

        pdp_wait = int(self.settings.getint("NORDSTROM_PDP_WAIT", 5) or 5)
        rendered = self._selenium_fetch(
            pdp_url, wait_seconds=pdp_wait, wait_for_gallery=True
        )
        if rendered is not None:
            response = rendered

        if self._is_blocked(response):
            self.logger.warning("[Nordstrom] Blocked PDP: %s", response.url)
            return

        next_data = extract_next_data(response)
        product = (
            self._next_data_deep(next_data, "props", "pageProps", "product")
            or self._next_data_deep(next_data, "props", "pageProps", "productDetail")
            or self._next_data_deep(
                next_data, "props", "pageProps", "initialState", "product"
            )
            or {}
        )

        title = first_text(
            product.get("name", ""),
            product.get("displayName", ""),
            response.css("h1[class*='product-name']::text").get(),
            response.css("h1::text").get(),
            response.css('meta[property="og:title"]::attr(content)').get(),
        )
        if not title:
            self.logger.warning("No title on %s", response.url)
            return

        sku = str(product.get("styleNumber") or product.get("id") or pid_early or "")
        price_data = product.get("priceRange") or product.get("price") or {}
        if isinstance(price_data, dict):
            raw_price = (
                (price_data.get("regular") or {}).get("high", "")
                or (price_data.get("sale") or {}).get("high", "")
                or str(price_data)
            )
        else:
            raw_price = str(price_data)
        price = clean_price(raw_price)
        availability = (
            "In Stock" if product.get("isAvailable", True) else "Out of Stock"
        )

        radio_colors = self._parse_color_radios(response.text or "")
        # Only click colour radios when multiple colours (expensive)
        swatch_colors: list[tuple[str, str, list[str]]] = []
        if len(radio_colors) > 1:
            swatch_colors = self._selenium_color_galleries(radio_colors)

        if swatch_colors:
            colors = [(c, cid, {"_urls": imgs}) for c, cid, imgs in swatch_colors]
        elif radio_colors:
            colors = [(c, cid, {}) for c, cid in radio_colors]
        else:
            colors = self._nordstrom_colors(product) or [("Default", "", {})]

        all_images = self._nordstrom_images(product, response)
        live = self._collect_gallery_from_driver()
        if len(live) > len(all_images):
            all_images = live

        self.logger.info(
            "[Nordstrom] %s — %d base images, %d colours on %s",
            title,
            len(all_images),
            len(colors),
            response.url,
        )

        min_done = int(self.settings.getint("NORDSTROM_RESUME_MIN_IMAGES", 4) or 4)
        yielded_any = False
        for color, color_id, variant_dict in colors:
            color_urls: list[str] = []
            color_seen: set[str] = set()
            for img in variant_dict.get("_urls") or []:
                add_unique(color_urls, img, color_seen)
            for img in self._nordstrom_color_images(variant_dict, response):
                add_unique(color_urls, img, color_seen)
            if not color_urls and len(colors) == 1:
                for img in all_images:
                    add_unique(color_urls, img, color_seen)
            color_urls = color_urls[:16]
            if not color_urls:
                continue

            existing_n = self._folder_image_count(title, color or "Default")
            if existing_n >= max(len(color_urls), min_done):
                self.logger.info(
                    "[Nordstrom] Skip colour (already have %d imgs) %s / %s",
                    existing_n,
                    title,
                    color,
                )
                yielded_any = True
                continue

            yielded_any = True
            yield self.make_item(
                product_name=title,
                product_url=response.url,
                image_urls=color_urls,
                color=color,
                sku=sku,
                variant=color_id,
                price=price,
                availability=availability,
            )

        if not yielded_any and all_images:
            yield self.make_item(
                product_name=title,
                product_url=response.url,
                image_urls=all_images[:16],
                color="Default",
                sku=sku,
                price=price,
                availability=availability,
            )

        if sku or pid_early:
            self._done_product_ids.add(sku or pid_early)

    # ── Selenium (single reused driver) ──────────────────────────────────────

    def _get_driver(self):
        if self._driver is not None:
            try:
                _ = self._driver.current_url
                return self._driver
            except Exception:
                self._quit_driver()

        profile = profile_dir(self.settings)
        headless = bool(self.settings.getbool("NORDSTROM_SELENIUM_HEADLESS", True))
        style = str(
            self.settings.get("NORDSTROM_HEADLESS_STYLE", "offscreen") or "offscreen"
        )
        self._driver = build_undetected_chrome(
            profile,
            headless=headless,
            headless_style=style,
            block_heavy=True,
            log=self.logger,
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

    def _selenium_fetch(
        self,
        url: str,
        wait_seconds: int = 6,
        wait_for_products: bool = False,
        wait_for_gallery: bool = False,
    ) -> HtmlResponse | None:
        """Navigate reused Chrome to url; return HtmlResponse."""
        try:
            driver = self._get_driver()
            try:
                driver.get(url)
            except TimeoutException:
                self.logger.debug(
                    "[Nordstrom] page load timed out (continuing) %s", url
                )

            if wait_for_products:
                try:
                    WebDriverWait(driver, max(wait_seconds, 8)).until(
                        EC.presence_of_element_located(
                            (By.CSS_SELECTOR, 'a[href*="/s/"]')
                        )
                    )
                except Exception:
                    self.logger.warning(
                        "[Nordstrom] Timed out waiting for product links on %s", url
                    )
                time.sleep(0.8)
                for y in (900, 1800, 2700):
                    try:
                        driver.execute_script(f"window.scrollTo(0, {y});")
                        time.sleep(0.35)
                    except Exception:
                        break
                try:
                    driver.execute_script("window.scrollTo(0, 0);")
                    time.sleep(0.2)
                except Exception:
                    pass
            elif wait_for_gallery:
                try:
                    WebDriverWait(driver, wait_seconds).until(
                        EC.any_of(
                            EC.presence_of_element_located(
                                (
                                    By.CSS_SELECTOR,
                                    'img[src*="nordstrommedia.com/it/"]',
                                )
                            ),
                            EC.presence_of_element_located(
                                (By.CSS_SELECTOR, "h1")
                            ),
                        )
                    )
                except Exception:
                    self.logger.debug(
                        "[Nordstrom] Gallery wait timed out on %s", url
                    )
                time.sleep(0.3)
                try:
                    driver.execute_script(
                        "window.scrollTo(0, Math.min(document.body.scrollHeight*0.3, 1000));"
                    )
                    time.sleep(0.2)
                except Exception:
                    pass
            else:
                time.sleep(min(wait_seconds, 3))
                try:
                    driver.execute_script(
                        "window.scrollTo(0, Math.min(document.body.scrollHeight*0.5, 2000));"
                    )
                    time.sleep(0.4)
                except Exception:
                    pass

            html = driver.page_source or ""
            current = driver.current_url or url
            return HtmlResponse(
                url=current,
                body=html.encode("utf-8"),
                encoding="utf-8",
                request=scrapy.Request(url),
            )
        except Exception as exc:
            self.logger.warning("[Nordstrom] Selenium fetch failed: %s", exc)
            self._quit_driver()
            return None

    # ── Resume helpers ───────────────────────────────────────────────────────

    def _load_done_product_ids(self) -> set[str]:
        store = Path(self.settings.get("IMAGES_STORE", "dataset"))
        root = store / "full"
        if not root.is_dir():
            return set()
        min_imgs = int(self.settings.getint("NORDSTROM_RESUME_MIN_IMAGES", 4) or 4)
        done: set[str] = set()
        for meta_path in root.rglob("metadata.json"):
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            source = (data.get("source") or "").lower()
            url = (data.get("product_url") or "").lower()
            if source != "nordstrom" and "nordstrom.com" not in url:
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

    def _folder_image_count(self, product_name: str, color: str) -> int:
        store = Path(self.settings.get("IMAGES_STORE", "dataset"))
        # Pipeline file_path: Brand/Product/Color — brand often overridden to
        # product brand; check both Nordstrom and any brand folders via glob
        candidates = [
            store / "full" / safe_name(self.brand, 60) / safe_name(product_name, 80) / safe_name(color, 40),
            store / "full" / safe_name(product_name, 80) / safe_name(color, 40),
        ]
        # Also match Brand/*/Color when brand varies (Nordstrom sells many brands)
        brand_glob = list(
            (store / "full").glob(
                f"*/{safe_name(product_name, 80)}/{safe_name(color, 40)}"
            )
        )
        folders = [p for p in candidates + brand_glob if p.is_dir()]
        if not folders:
            return 0
        folder = folders[0]
        return sum(
            1
            for p in folder.iterdir()
            if p.is_file()
            and p.suffix.lower() in _IMG_EXTS
            and p.stat().st_size > 1000
        )

    # ── Listing helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _product_id(url: str) -> str:
        m = re.search(r"/s/[^/]+/(\d+)", url or "", re.I)
        return m.group(1) if m else ""

    def _extract_product_urls(self, response) -> list[str]:
        seen: set[str] = set()
        urls: list[str] = []

        def _add(raw: str) -> None:
            if not raw:
                return
            m = _PRODUCT_HREF_RE.search(raw)
            path = m.group(1) if m else ""
            if not path and "/s/" in raw:
                path = raw.split("?")[0]
                if path.startswith("http"):
                    path = urlparse(path).path
            if not path or "/s/" not in path:
                return
            if any(x in path.lower() for x in ("/browse/", "/c/", "/invitation")):
                return
            full = response.urljoin(raw.split("#")[0])
            pid = self._product_id(full)
            key = pid or full.split("?")[0].rstrip("/").lower()
            if key not in seen:
                seen.add(key)
                urls.append(full)

        next_data = extract_next_data(response)
        products = (
            self._next_data_deep(next_data, "props", "pageProps", "products")
            or self._next_data_deep(
                next_data, "props", "pageProps", "searchResults", "products"
            )
            or self._next_data_deep(
                next_data, "props", "pageProps", "initialState", "products"
            )
            or []
        )
        if isinstance(products, dict):
            products = products.get("items") or products.get("results") or []
        for product in products or []:
            if isinstance(product, dict):
                _add(
                    product.get("productPageUrl")
                    or product.get("url")
                    or product.get("productUrl")
                    or ""
                )

        for href in response.css(
            '[data-container-type="product-grid"] article a[href*="/s/"]::attr(href), '
            "article.IZSr3 a[href*='/s/']::attr(href), "
            "h3 a[href*='/s/']::attr(href)"
        ).getall():
            _add(href)

        if not urls:
            for href in response.css('a[href*="/s/"]::attr(href)').getall():
                _add(href)

        return urls

    def _next_page_url(self, response, page_num: int) -> str | None:
        """Use footer Next only — never invent infinite ?page=N URLs."""
        for a in response.css("footer a.trYAx, footer a[href*='page=']"):
            label = " ".join(a.css("::text").getall()).strip().lower()
            href = (a.attrib.get("href") or "").strip()
            if href and "next" in label:
                return response.urljoin(href)

        want = page_num + 1
        for href in response.css("footer a[href*='page=']::attr(href)").getall():
            m = re.search(r"[?&]page=(\d+)", href)
            if m and int(m.group(1)) == want:
                return response.urljoin(href)

        next_href = (
            response.xpath('//a[@aria-label="Next page"]/@href').get()
            or response.css('a[rel="next"]::attr(href)').get()
        )
        if next_href:
            return response.urljoin(next_href)
        return None

    def _is_blocked(self, response) -> bool:
        url = (response.url or "").lower()
        text = (response.text or "")[:8000].lower()
        if "siteclosed.nordstrom.com" in url or "invitation.html" in url:
            return True
        if "this site is not available" in text or "not available in your region" in text:
            return True
        return False

    # ── Product helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _clean_color_name(raw: str) -> str:
        name = (raw or "").strip()
        if name.lower().startswith("selected "):
            name = name[9:].strip()
        return name

    def _parse_color_radios(self, html: str) -> list[tuple[str, str]]:
        """Parse PDP ``input[name=color]`` radios → [(color_name, color_id), ...]."""
        seen: set[str] = set()
        result: list[tuple[str, str]] = []
        for m in re.finditer(
            r'<input[^>]*name="color"[^>]*value="([^"]+)"[^>]*>'
            r".*?"
            r'<img class="Xn7hh"[^>]*(?:title|alt)="([^"]+)"',
            html,
            re.I | re.S,
        ):
            cid = m.group(1).strip()
            name = self._clean_color_name(m.group(2))
            chunk = m.group(0)
            tm = re.search(r'\btitle="([^"]+)"', chunk, re.I)
            if tm:
                name = self._clean_color_name(tm.group(1))
            key = cid or name.lower()
            if not name or key in seen:
                continue
            seen.add(key)
            result.append((name, cid))

        if result:
            return result

        for m in re.finditer(
            r'<input[^>]*class="[^"]*E2iXd[^"]*"[^>]*'
            r'(?:id="([^"]+)"[^>]*value="([^"]+)"|value="([^"]+)"[^>]*id="([^"]+)")',
            html,
            re.I,
        ):
            cid = (m.group(2) or m.group(3) or m.group(1) or m.group(4) or "").strip()
            if not cid or cid in seen:
                continue
            chunk = html[m.end() : m.end() + 500]
            tm = re.search(r'\btitle="([^"]+)"', chunk, re.I)
            am = re.search(r'\balt="([^"]+)"', chunk, re.I)
            name = self._clean_color_name(
                (tm.group(1) if tm else "") or (am.group(1) if am else "") or cid
            )
            seen.add(cid)
            result.append((name, cid))
        return result

    def _collect_gallery_from_driver(self) -> list[str]:
        """Product gallery URLs from the live PDP (skip 40px colour swatches)."""
        if self._driver is None:
            return []
        driver = self._driver
        imgs: list[str] = []
        seen: set[str] = set()

        def _keep(src: str) -> bool:
            if not src or not is_product_image(src, _CDN):
                return False
            low = src.lower()
            if "swatch=true" in low or "w=40" in low or "h=40" in low or "h=28" in low:
                return False
            if low.endswith(".gif") or ".gif?" in low:
                return False
            return True

        try:
            for el in driver.find_elements(
                By.CSS_SELECTOR,
                'img[src*="nordstrommedia.com/it/"]:not(.Xn7hh)',
            ):
                for attr in ("src", "data-src", "srcset"):
                    src = el.get_attribute(attr) or ""
                    if " " in src and "," in src:
                        parts = [
                            p.strip().split(" ")[0] for p in src.split(",") if p.strip()
                        ]
                        src = parts[-1] if parts else src
                    if _keep(src):
                        add_unique(imgs, clean_url(src), seen)
        except Exception:
            pass

        if len(imgs) < 2:
            try:
                html = driver.page_source or ""
            except Exception:
                html = ""
            for m in _MEDIA_RE.finditer(html):
                src = m.group(0).rstrip("\\")
                if _keep(src):
                    add_unique(imgs, clean_url(src), seen)

        return imgs[:16]

    def _selenium_color_galleries(
        self, radio_colors: list[tuple[str, str]] | None = None
    ) -> list[tuple[str, str, list[str]]]:
        """Click each ``name=color`` radio label and collect that colour's gallery."""
        if self._driver is None:
            return []
        driver = self._driver

        colors = list(radio_colors or [])
        if not colors:
            try:
                html = driver.page_source or ""
            except Exception:
                html = ""
            colors = self._parse_color_radios(html)

        if len(colors) <= 1:
            return []

        results: list[tuple[str, str, list[str]]] = []
        for name, cid in colors:
            clicked = False
            if cid:
                try:
                    label = driver.find_element(By.CSS_SELECTOR, f'label[for="{cid}"]')
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center'});", label
                    )
                    time.sleep(0.1)
                    try:
                        label.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", label)
                    clicked = True
                    time.sleep(0.55)
                except Exception:
                    clicked = False

            if not clicked and cid:
                try:
                    parsed = urlparse(driver.current_url)
                    qs = parse_qs(parsed.query)
                    qs["color"] = [cid]
                    new_url = urlunparse(
                        parsed._replace(query=urlencode(qs, doseq=True))
                    )
                    try:
                        driver.get(new_url)
                    except TimeoutException:
                        pass
                    time.sleep(0.8)
                    clicked = True
                except Exception:
                    continue

            if not clicked:
                continue

            imgs = self._collect_gallery_from_driver()
            if imgs:
                results.append((name, cid, imgs))

        return results

    def _nordstrom_colors(self, product: dict) -> list[tuple[str, str, dict]]:
        seen: set[str] = set()
        result: list[tuple[str, str, dict]] = []
        for key in ("colorOptions", "colors", "variants", "skus"):
            items = product.get(key) or []
            for v in items:
                if not isinstance(v, dict):
                    continue
                name = (
                    v.get("color")
                    or v.get("colorName")
                    or v.get("displayColor")
                    or v.get("name")
                    or ""
                ).strip()
                cid = str(v.get("colorCode") or v.get("id") or "")
                if name and name.lower() not in seen:
                    seen.add(name.lower())
                    result.append((name, cid, v))
        return result

    def _nordstrom_images(self, product: dict, response) -> list[str]:
        seen: set[str] = set()
        urls: list[str] = []
        for key in ("images", "media", "productImages"):
            imgs = product.get(key) or []
            for img in imgs:
                if isinstance(img, dict):
                    src = (
                        img.get("url")
                        or img.get("src")
                        or img.get("squareLargeUrl")
                        or ""
                    )
                elif isinstance(img, str):
                    src = img
                else:
                    continue
                if "swatch=true" in src or "w=40" in src:
                    continue
                if src and is_product_image(src, _CDN):
                    add_unique(urls, clean_url(response.urljoin(src)), seen)

        if len(urls) < 3:
            for m in _MEDIA_RE.finditer(response.text or ""):
                src = m.group(0).rstrip("\\")
                if "swatch=true" in src or "w=40" in src or "h=28" in src:
                    continue
                if is_product_image(src, _CDN):
                    add_unique(urls, clean_url(src), seen)

        if not urls:
            for img in self.gallery_images(response):
                if "swatch=true" not in img and "w=40" not in img:
                    add_unique(urls, img, seen)

        return urls[:16]

    def _nordstrom_color_images(self, variant: dict, response) -> list[str]:
        seen: set[str] = set()
        urls: list[str] = []
        for imgs in (variant.get("images", []), variant.get("media", [])):
            for img in imgs:
                if isinstance(img, dict):
                    src = img.get("url") or img.get("src") or ""
                else:
                    src = str(img)
                if "swatch=true" in src or "w=40" in src:
                    continue
                if src and is_product_image(src, _CDN):
                    add_unique(urls, clean_url(response.urljoin(src)), seen)
        return urls
