"""
pipelines.py
~~~~~~~~~~~~
Image pipeline for the multi-site sandal scraper framework.

``DatasetImagesPipeline``
    Main pipeline used by all new spiders.  Saves images under::

        dataset/<Brand>/<Product Name>/<Color>/<index:03d>.<ext>

    Additional features vs. the original:
      - Writes ``metadata.json`` alongside every colour variant folder
      - Skips images that already exist on disk (resume support)
      - Enforces minimum image dimensions (configured via settings)
      - Retries failed downloads (handled by Scrapy's built-in retry)

``CustomImagesPipeline``
    Original pipeline — preserved unchanged for backward compatibility with
    the ``selle-sandals`` spider.  It uses the ``images/`` store and the
    original filename convention ``full/<Title>/<Color>/<index>.<ext>``.

``ManualScraperExtPipeline``
    Pass-through pipeline (kept for future extension).
"""

import re
import os
import sys
import time
from datetime import datetime, timedelta

from tqdm import tqdm
from scrapy.pipelines.images import ImagesPipeline
from scrapy.http import Request

from manual_scraper_ext.base.metadata import (
    safe_name,
    dataset_folder,
    write_metadata_json,
)


_ILLEGAL = re.compile(r'[\\/*?:"<>|\r\n\t]')


def _safe(text: str, maxlen: int = 80) -> str:
    """Strip filesystem-illegal characters and truncate."""
    return _ILLEGAL.sub("", (text or "").strip())[:maxlen]


# ─────────────────────────────────────────────────────────────────────────────
# Pass-through pipeline (backward compat)
# ─────────────────────────────────────────────────────────────────────────────

class ManualScraperExtPipeline:
    """Pass-through pipeline (kept for future extension)."""

    def process_item(self, item, spider):
        return item


# ─────────────────────────────────────────────────────────────────────────────
# Original pipeline — unchanged, backward-compatible with selle-sandals
# ─────────────────────────────────────────────────────────────────────────────

class CustomImagesPipeline(ImagesPipeline):
    """
    Original image pipeline for the ``selle-sandals`` spider.

    Saves images as::

        full/<Title>/<Color>/<index:03d>.<ext>

    Kept exactly as it was so the original spider needs no changes.
    """

    def get_media_requests(self, item, info):
        meta = item.get("meta", {})
        for index, url in enumerate(item.get("image_urls", [])):
            yield Request(
                url,
                meta={
                    "title":      meta.get("title", "Unknown"),
                    "color":      meta.get("color", "Default"),
                    "variant_id": meta.get("variant_id", ""),
                    "img_index":  index,
                },
            )

    def file_path(self, request, response=None, info=None, *, item=None):
        title = _safe(request.meta.get("title", "Unknown"), 80)
        color = _safe(request.meta.get("color", "Default"), 40)
        index = int(request.meta.get("img_index") or 0)

        url_path = request.url.split("?")[0]
        ext = url_path.rsplit(".", 1)[-1].lower()
        if ext not in {"jpg", "jpeg", "png", "webp", "gif"}:
            ext = "jpg"

        return f"full/{title}/{color}/{index:03d}.{ext}"


# ─────────────────────────────────────────────────────────────────────────────
# New pipeline — used by all new spiders
# ─────────────────────────────────────────────────────────────────────────────

class DatasetImagesPipeline(ImagesPipeline):
    """
    Dataset image pipeline for all spiders except ``selle-sandals``.

    Saves images as::

        dataset/<Brand>/<Product Name>/<Color>/<index:03d>.<ext>

    After downloading, writes ``metadata.json`` next to the images.

    Features
    --------
    - Resume support: Scrapy's built-in fingerprinting skips already-cached
      images; we additionally skip files already present on disk.
    - Minimum size filtering: configured via ``IMAGES_MIN_HEIGHT`` /
      ``IMAGES_MIN_WIDTH`` in settings (defaults: 100×100 px).
    - Human-readable folder structure.
    - Automatic ``metadata.json`` generation.
    - Progress tracking with ETA.
    """

    def __init__(self, store_uri, crawler, **kwargs):
        super().__init__(store_uri, crawler=crawler, **kwargs)
        self.stats = crawler.stats
        self.logger = crawler.spider.logger if hasattr(crawler, 'spider') else None
        self.total_images = 0
        self.downloaded_images = 0
        self.failed_images = 0
        self.start_time = time.time()
        self.skip_image_download = crawler.settings.getbool("SKIP_IMAGE_DOWNLOAD", False)
        self.progress_bar = None
        self.progress_initialized = False

    @classmethod
    def from_crawler(cls, crawler):
        obj = super().from_crawler(crawler)
        obj.stats = crawler.stats
        obj.logger = crawler.spider.logger if hasattr(crawler, 'spider') else None
        obj.total_images = 0
        obj.downloaded_images = 0
        obj.failed_images = 0
        obj.start_time = time.time()
        obj.skip_image_download = crawler.settings.getbool("SKIP_IMAGE_DOWNLOAD", False)
        obj.progress_bar = None
        obj.progress_initialized = False
        return obj

    def _update_progress(self, spider):
        """Update progress bar with ETA information."""
        if self.total_images == 0 or self.progress_bar is None:
            return
        
        processed = self.downloaded_images + self.failed_images
        self.progress_bar.total = self.total_images
        self.progress_bar.n = processed
        
        elapsed = time.time() - self.start_time
        if elapsed > 0:
            rate = processed / elapsed
            remaining = self.total_images - processed
            if rate > 0:
                eta_seconds = remaining / rate
                eta_time = datetime.now() + timedelta(seconds=eta_seconds)
                eta_str = f"ETA: {eta_time.strftime('%H:%M:%S')} ({eta_seconds/60:.1f}m)"
            else:
                eta_str = "ETA: calculating..."
        else:
            eta_str = "ETA: calculating..."
        
        # Update progress bar description with download/failed counts
        desc = f"[Images] Downloaded: {self.downloaded_images} | Failed: {self.failed_images} | {eta_str}"
        self.progress_bar.set_description(desc)
        self.progress_bar.refresh()

    def get_media_requests(self, item, info):
        if self.skip_image_download:
            # Skip image downloading for testing purposes
            info.spider.logger.debug("[ImagePipeline] Skipping image download (SKIP_IMAGE_DOWNLOAD=True)")
            return
        
        meta = item.get("meta", {})
        brand        = safe_name(meta.get("brand", "Unknown"), 60)
        product_name = safe_name(meta.get("product_name", "Unknown"), 80)
        color        = safe_name(meta.get("color", "Default"), 40)

        image_count = len(item.get("image_urls", []))
        self.total_images += image_count
        self.stats.set_value("image_pipeline/total_images", self.total_images)
        
        # Initialize progress bar when we first know the total
        if not self.progress_initialized and self.total_images > 0:
            self.progress_initialized = True
            self.progress_bar = tqdm(
                total=self.total_images,
                desc="[Images] Downloading...",
                unit="img",
                file=sys.stdout,
                ncols=100,
                leave=True,
            )
            self.start_time = time.time()

        for index, url in enumerate(item.get("image_urls", [])):
            yield Request(
                url,
                meta={
                    "brand":         brand,
                    "product_name":  product_name,
                    "color":         color,
                    "img_index":     index,
                    "item_meta":     meta,   # full metadata for JSON writing
                },
            )

    def file_path(self, request, response=None, info=None, *, item=None):
        brand        = request.meta.get("brand", "Unknown")
        product_name = request.meta.get("product_name", "Unknown")
        color        = request.meta.get("color", "Default")
        index        = int(request.meta.get("img_index") or 0)

        url_path = request.url.split("?")[0]
        ext = url_path.rsplit(".", 1)[-1].lower()
        if ext not in {"jpg", "jpeg", "png", "webp", "gif"}:
            ext = "jpg"

        # <Brand>/<Product Name>/<Color>/<index:03d>.<ext>
        return f"{brand}/{product_name}/{color}/{index:03d}.{ext}"

    def item_completed(self, results, item, info):
        """
        Called after all images for an item have been processed.

        Writes ``metadata.json`` into the product/colour folder and
        updates the ``images`` field with the downloaded file paths.
        Updates progress bar with download status.
        
        When SKIP_IMAGE_DOWNLOAD is True, images are not downloaded
        but metadata is still tracked.
        """
        if self.skip_image_download:
            # When skipping downloads, just track stats and return
            info.spider.logger.debug("[ImagePipeline] Skipping metadata write (SKIP_IMAGE_DOWNLOAD=True)")
            item["images"] = item.get("images") or []
            return item

        # Spider may have pre-downloaded images (e.g. Farfetch Chrome cookies)
        if not results and item.get("images"):
            return item

        downloaded = [
            x["path"] for ok, x in results if ok and isinstance(x, dict)
        ]
        
        # Track success/failure counts
        for ok, x in results:
            if ok and isinstance(x, dict):
                self.downloaded_images += 1
            else:
                self.failed_images += 1
        
        self.stats.inc_value("image_pipeline/downloaded_images", len(downloaded))
        self.stats.inc_value("image_pipeline/failed_images", len(results) - len(downloaded))

        meta = dict(item.get("meta", {}))
        meta["images"] = downloaded

        # Write metadata.json
        if downloaded:
            # Derive folder from the first downloaded path.
            # Scrapy stores images at: <IMAGES_STORE>/full/<file_path()> for
            # ImagesPipeline.  DatasetImagesPipeline's file_path returns
            # Brand/Product/Color/NNN.ext so the actual path on disk is:
            #   <IMAGES_STORE>/full/<Brand>/<Product>/<Color>/NNN.ext
            store = info.spider.settings.get("IMAGES_STORE", "dataset")
            first_path = os.path.join(store, "full", downloaded[0])
            folder = os.path.dirname(first_path)
        else:
            # No images downloaded — still write metadata next to where they
            # would have been saved
            store = info.spider.settings.get("IMAGES_STORE", "dataset")
            brand        = safe_name(meta.get("brand", "Unknown"), 60)
            product_name = safe_name(meta.get("product_name", "Unknown"), 80)
            color        = safe_name(meta.get("color", "Default"), 40)
            folder = os.path.join(store, "full", brand, product_name, color)

        write_metadata_json(folder, meta)
        
        # Update progress bar
        self._update_progress(info.spider)
        
        # Close progress bar when done
        processed = self.downloaded_images + self.failed_images
        if self.progress_bar and processed >= self.total_images:
            self.progress_bar.close()

        item["images"] = downloaded
        return item
