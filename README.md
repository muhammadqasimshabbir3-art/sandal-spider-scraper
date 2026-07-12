# Sandal Spider Scraper Framework

Scrapy crawler for men's sandal product images (DINOv2 / retrieval dataset).

Only **four** sites are active.

---

## Active websites

| Spider command | Brand | Seed URL | Engine |
|----------------|-------|----------|--------|
| `scrapy crawl nordstrom` | Nordstrom | https://www.nordstrom.com/browse/men/shoes/sandals | undetected Chrome + profile |
| `scrapy crawl farfetch` | Farfetch | https://www.farfetch.com/us/ → auto locale sandals | undetected Chrome + profile |
| `scrapy crawl zappos` | Zappos | https://www.zappos.com/men-sandals/CK_XARC51wHAAQLiAgMBAhg.zso | Scrapy |
| `scrapy crawl selle-sandals` | Selle Sandals | https://www.selle-sandals.com/collections/all | Scrapy |
| `scrapy crawl all` | *(all above)* | — | Meta spider |

---

## Quick Start

```bash
cd sandal-spider-scraper
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 1. One-time Chrome login

```bash
python tools/setup_chrome_login.py
```

Log into Google, open Nordstrom / Farfetch (and Zappos if prompted), pass any checks, then press Enter to save `cookies/session.json`.

For Farfetch: do **not** force `/pk/` or `/us/` — those often return Access Denied. The spider opens `farfetch.com`, adopts the allowed locale (e.g. `/jp/`), then crawls sandals. Re-run `setup_chrome_login.py` if needed; close other Chrome windows using `chrome_profile/` first.

### 2. Crawl

```bash
scrapy list

scrapy crawl nordstrom
scrapy crawl farfetch
scrapy crawl zappos
scrapy crawl selle-sandals

scrapy crawl all
```

Smoke test:

```bash
scrapy crawl zappos -s CLOSESPIDER_ITEMCOUNT=3
```

---

## Project layout

```
manual_scraper_ext/
  base/                 # BaseSandalSpider, EcommerceSpider, image helpers
  spiders/
    nordstrom.py
    farfetch.py
    zappos.py
    selle_sandals.py
    crawl_all.py
  chrome_cookies.py
  chrome_cookie_middleware.py
  selenium_captcha_middleware.py
  pipelines.py
  settings.py
tools/setup_chrome_login.py
chrome_profile/         # gitignored
cookies/session.json    # gitignored
dataset/                # downloaded images
```

---

## Dataset format

```
dataset/
└── <Brand>/
    └── <Product Name>/
        └── <Color>/
            ├── metadata.json
            ├── 000.jpg
            └── …
```

---

## Chrome trust flow

```
setup_chrome_login.py  →  chrome_profile/ + cookies/session.json
                                    ↓
              ChromeCookieMiddleware (Scrapy requests)
                                    ↓
         Selenium (Nordstrom / Farfetch) or plain Scrapy (Zappos / Selle)
                                    ↓
                         DatasetImagesPipeline → dataset/
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Chrome profile locked | Close other Chrome windows using `chrome_profile/` |
| 403 / challenge | Re-run `python tools/setup_chrome_login.py` |
| Nordstrom geo-block | US VPN + reopen Nordstrom in the Chrome profile |
| Farfetch Access Denied | Avoid `/pk/` and `/us/` URLs; use undetected Chrome + profile; close locked `chrome_profile/` windows |

---

## License

Respect each website's Terms of Service and robots.txt.
