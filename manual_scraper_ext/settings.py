# ─────────────────────────────────────────────────────────────────────────────
#  Scrapy settings for the sandal-spider-scraper framework
#
#  This file configures the PROJECT-LEVEL defaults.  Each spider may override
#  any setting via its ``custom_settings`` class attribute.
# ─────────────────────────────────────────────────────────────────────────────

BOT_NAME = "sandal_spider_scraper"

SPIDER_MODULES = ["manual_scraper_ext.spiders"]
NEWSPIDER_MODULE = "manual_scraper_ext.spiders"

# ── Politeness ────────────────────────────────────────────────────────────────
ROBOTSTXT_OBEY = False
CONCURRENT_REQUESTS = 2
CONCURRENT_REQUESTS_PER_DOMAIN = 2
DOWNLOAD_DELAY = 1.0            # seconds between successive requests
RANDOMIZE_DOWNLOAD_DELAY = True

# ── AutoThrottle ──────────────────────────────────────────────────────────────
AUTOTHROTTLE_ENABLED = True
AUTOTHROTTLE_START_DELAY = 1.0
AUTOTHROTTLE_MAX_DELAY = 30.0
AUTOTHROTTLE_TARGET_CONCURRENCY = 1.5

# ── User-Agent ────────────────────────────────────────────────────────────────
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_REQUEST_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}

# ── Retry ─────────────────────────────────────────────────────────────────────
RETRY_ENABLED = True
RETRY_TIMES = 3
RETRY_HTTP_CODES = [500, 502, 503, 504, 403, 408, 429]

# ── HTTP Cache (disabled during active scraping) ──────────────────────────────
HTTPCACHE_ENABLED = False
# Uncomment to enable caching during development:
# HTTPCACHE_ENABLED          = True
# HTTPCACHE_EXPIRATION_SECS  = 0
# HTTPCACHE_DIR              = ".scrapy/httpcache"
# HTTPCACHE_STORAGE          = "scrapy.extensions.httpcache.FilesystemCacheStorage"

# ── Item Pipelines ────────────────────────────────────────────────────────────
# The DatasetImagesPipeline is the default for all new spiders.
# The original selle-sandals spider overrides this via custom_settings.
ITEM_PIPELINES = {
    "manual_scraper_ext.pipelines.DatasetImagesPipeline": 1,
}

# ── Dataset image pipeline (new spiders) ─────────────────────────────────────
IMAGES_STORE = "dataset"           # root directory for downloaded images
IMAGES_URLS_FIELD = "image_urls"   # item field containing URL list
IMAGES_RESULT_FIELD = "images"     # item field Scrapy writes results into
IMAGES_MIN_HEIGHT = 0              # minimum image height in pixels
IMAGES_MIN_WIDTH = 0               # minimum image width in pixels

# ── Feed export (JSON Lines) ──────────────────────────────────────────────────
# Uncomment to also save scraped items to a file:
# FEEDS = {
#     "output/items.jl": {"format": "jsonlines", "overwrite": True},
# }

# ── Downloader middlewares ────────────────────────────────────────────────────
# DOWNLOADER_MIDDLEWARES = {
#     "manual_scraper_ext.middlewares.ManualScraperExtDownloaderMiddleware": 543,
# }

# ── Misc ──────────────────────────────────────────────────────────────────────
COOKIES_ENABLED = True
REQUEST_FINGERPRINTER_IMPLEMENTATION = "2.7"
TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"
FEED_EXPORT_ENCODING = "utf-8"

# ── Browser Rendering for Challenges ─────────────────────────────────────────
# Use Playwright browser rendering to bypass bot challenges.
# Most e-commerce sites don't have real CAPTCHA, just browser checks.
BROWSER_RENDERING_ENABLED = True

# Use Selenium to open a real browser for interactive CAPTCHA solving.
# When True, the Selenium middleware will launch a visible Chrome window
# for pages that look like bot challenges. This is intended for manual,
# one-off runs where the operator can solve the CAPTCHA interactively.
SELENIUM_CHALLENGE_ENABLED = True

# ── Skip Image Download (for testing CAPTCHA/challenges without storage) ──────
# Set to True to run spiders without saving images to dataset folder.
# Useful for testing challenge handling and validating spider logic.
SKIP_IMAGE_DOWNLOAD = False

# ── Skip Specific Spiders ─────────────────────────────────────────────────────
# List of spider names to skip during execution (e.g., ['selle_sandals', 'asos']).
# Useful when running crawl_all to exclude specific sites.
# Usage: scrapy crawl crawl_all -a skip_spiders="selle_sandals,asos"
# No excluded spiders are configured; only the active spiders are kept in the project.
EXCLUDED_SPIDERS = []


# Enable the challenge middleware (placed after the standard retry middleware at 550)
# Uses browser rendering (Playwright) to bypass bot challenges.
DOWNLOADER_MIDDLEWARES = {
    # Selenium-based interactive challenge handler (runs before Playwright)
    "manual_scraper_ext.selenium_captcha_middleware.SeleniumChallengeMiddleware": 585,
    "manual_scraper_ext.qwen_captcha_middleware.BrowserRenderingChallengeMiddleware": 590,
}

# ── HTTP error handling ────────────────────────────────────────────────────────
# Allow 418 and 403 to reach the spider's parse methods (so they can log/skip)
# Note: individual spiders may override this list.
HTTPERROR_ALLOWED_CODES = [418, 403]

# ── Logging Configuration ─────────────────────────────────────────────────────
# For Kaggle/limited output environments:
#   LOG_LEVEL = 'WARNING'  — Show only warnings and errors (quiet mode)
#   LOG_LEVEL = 'INFO'     — Show info messages (default)
#   LOG_LEVEL = 'DEBUG'    — Show all debug messages (verbose)
LOG_LEVEL = 'INFO'

# Disable specific verbose loggers
LOGSTATS_INTERVAL = 0  # Disable periodic log stats (0 = never print stats)

# Suppress Python warnings in quiet mode
# Uncomment for Kaggle to hide deprecation warnings:
# import warnings
# warnings.filterwarnings('ignore')
