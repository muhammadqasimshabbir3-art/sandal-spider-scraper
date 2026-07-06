# Sandal Spider Scraper Framework

A production-quality, multi-site e-commerce scraping framework designed to build a high-quality dataset for DINOv2 image retrieval fine-tuning.

This project crawls men's sandals, slides, and flip-flops from multiple premium e-commerce websites, extracts canonical product variant metadata, and downloads high-resolution gallery images organized into structured, human-readable directories.

---

## What this repository contains

- `manual_scraper_ext/` — Scrapy project package
  - `base/` — Shared component architecture:
    - `base_spider.py` — Core abstract base class `BaseSandalSpider`
    - `ecommerce_spider.py` — Intermediate `EcommerceSpider` with JSON-LD, Next.js, and Shopify handlers
    - `image_utils.py` — Reusable image URL normalization, filtering, and CDN matching
    - `metadata.py` — Canonical metadata schema & filesystem utilities
    - `parser_utils.py` — Shared HTML and JSON parsing helpers
  - `spiders/` — Individual site spiders:
    - `selle_sandals.py` — Baseline Shopify spider (fully backwards-compatible)
    - `skechers.py` — Skechers.com spider (JSON-LD & custom gallery parser)
    - `underarmour.py` — Underarmour.com Next.js hydration spider
    - `columbia.py` — Columbia.com state & swatch spider
    - `asos.py` — Asos.com public catalog API spider
    - `nordstrom.py` — Nordstrom.com Next.js hydration spider
    - `zappos.py` — Zappos.com Redux initial state spider
    - `crocs.py` — Crocs.com Salesforce Commerce Cloud / Next.js spider
    - `adidas.py` — Adidas.com Next.js article details spider
    - `alexandermcqueen.py` — Alexander McQueen SFCC & Akamai CDN spider
    - `farfetch.py` — Farfetch.com Next.js marketplace spider
    - `crawl_all.py` — Meta-spider to run all spiders sequentially
  - `items.py` — Canonical `SandalItem` definition (with backwards-compatible `SelleSandalsItem` alias)
  - `pipelines.py` — Image pipelines:
    - `DatasetImagesPipeline` — Main dataset pipeline generating `metadata.json` and nested folder structure
    - `CustomImagesPipeline` — Legacy pipeline for `selle-sandals`
  - `settings.py` — Global Scrapy settings, AutoThrottle, and defaults
- `requirements.txt` — Python dependencies
- `scrapy.cfg` — Scrapy configuration

---

## Project Layout

```
.
├── README.md
├── requirements.txt
├── scrapy.cfg
└── manual_scraper_ext/
    ├── __init__.py
    ├── items.py
    ├── middlewares.py
    ├── pipelines.py
    ├── settings.py
    ├── base/
    │   ├── __init__.py
    │   ├── base_spider.py
    │   ├── ecommerce_spider.py
    │   ├── image_utils.py
    │   ├── metadata.py
    │   └── parser_utils.py
    └── spiders/
        ├── __init__.py
        ├── crawl_all.py
        ├── selle_sandals.py
        ├── zappos.py
        └── __init__.py
```

---

## Quick Start

1. Clone the repository and open the project root.
2. Create and activate a Python virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

3. Install dependencies:

```bash
pip install -r requirements.txt
```

4. Run a single spider:

```bash
scrapy crawl zappos
scrapy crawl selle-sandals
```

5. Run all active spiders sequentially:

```bash
scrapy crawl all
```

---

## Dataset Format

Scraped images and metadata are saved under the `dataset/` root directory. Each unique product class and colour variant gets its own directory, conforming to the layout required for fine-tuning image retrieval models.

### Output Structure

```
dataset/
└── <Brand>/
    └── <Product Name>/
        └── <Color>/
            ├── metadata.json
            ├── 000.jpg
            ├── 001.jpg
            └── 002.jpg
```

### `metadata.json` Format

Each folder contains a `metadata.json` file populated according to the canonical schema:

```json
{
  "brand": "Zappos",
  "product_name": "Men's Sandal",
  "gender": "Men",
  "category": "Sandals",
  "sku": "123456",
  "variant": "Default",
  "color": "Black",
  "price": "69.95",
  "availability": "In Stock",
  "product_url": "https://www.zappos.com/p/men-sandal/product/123456",
  "images": [
    "Zappos/Men's Sandal/Black/000.jpg",
    "Zappos/Men's Sandal/Black/001.jpg"
  ]
}
```

---

Tunable options are set in `manual_scraper_ext/settings.py` and can be overridden per-spider.

| Setting | Default | Purpose |
|---|---|---|
| `CONCURRENT_REQUESTS` | `2` | Maximum concurrent requests |
| `DOWNLOAD_DELAY` | `1.0` | Base delay between successive requests |
| `AUTOTHROTTLE_ENABLED` | `True` | Automatically scale request rate based on target website load |
| `IMAGES_STORE` | `dataset` | Root directory for the scraped image dataset |
| `IMAGES_MIN_HEIGHT` | `100` | Skip images shorter than 100 pixels |
| `IMAGES_MIN_WIDTH` | `100` | Skip images narrower than 100 pixels |
| `HTTPCACHE_ENABLED` | `False` | Toggle HTTP caching (recommended `True` only in development) |

---

## CLI Usage

List all available spiders:
```bash
scrapy list
```

Run a specific site spider:
```bash
scrapy crawl zappos
scrapy crawl selle-sandals
```

Run the sequential crawl:
```bash
scrapy crawl all
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `No module named 'PIL'` | Run `pip install Pillow` to install image processing support. |
| Images not downloading | Ensure `dataset/` is writable and not blocked by filesystem permissions. |
| 0 products discovered | Site layout may have changed. Check selectors and page hydration variables. |
| Requests getting blocked (403/429) | Increase `DOWNLOAD_DELAY` or check your local proxy configuration. |

---

---

## Disabled / Manual Sites

- **Adidas**: automated image extraction is currently unreliable for `adidas`. The spider is excluded by default. You can still find the category entry points here:

  - https://www.adidas.com/us/men-sandals
  - https://www.adidas.com/us/men-slides

- **Columbia**: automated image extraction can be unreliable for `columbia`. The spider is excluded by default. You can still find the category entry points here:

  - https://www.columbia.com/c/mens-sandals/
  - https://www.columbia.com/c/mens-slides/

- **Crocs**: Crocs listing pages are actively blocking automated requests (403/429). The spider is excluded by default. Category entry point:

  - https://www.crocs.com/c/men/footwear/sandals

- **Farfetch**: Farfetch product and listing pages are not reliably accessible for automated image scraping. The spider is excluded by default; use manual browsing if needed. Example product page:

  - https://www.farfetch.com/pk/shopping/men/christian-louboutin-chambelimuly-leather-sandals-item-34890600.aspx

- **ASOS**: ASOS category and product APIs are currently blocked for automated requests and return 403/challenge responses. The spider is excluded by default for now.

- **Alexander McQueen**: automated image scraping for `alexandermcqueen` is not reliable enough. The spider is excluded by default; product pages should be scraped manually if needed.

- **Under Armour**: Under Armour enforces strict bot checks on category and product pages (HTTP 418 / challenge). Automated scraping with current middleware is unreliable; the spider is excluded by default and should be scraped manually if needed. Example category page:

  - https://www.underarmour.com/en-us/c/mens/shoes/sandals-slides/

- **Nordstrom**: Nordstrom's site requires careful Playwright rendering and the current automated flow did not yield downloadable product images in testing. Marked as manual-only for now; scrape manually if needed.

## How to Add a New Website

Adding a new website is straightforward using the shared component architecture:

1. **Create the Spider File**:
   Create a new file in `manual_scraper_ext/spiders/yoursite.py`.
2. **Inherit from `EcommerceSpider`**:
   Subclass `EcommerceSpider` (or `BaseSandalSpider` if the site is not e-commerce specific).
3. **Configure Settings**:
   Define `name`, `brand`, `cdn_patterns`, and target category page list in `start_urls`.
4. **Implement Page Handlers**:
   - `parse(self, response)`: Locate product URLs and yield them with `response.follow(url, callback=self.parse_product)`. Implement pagination using `self.standard_pagination`.
   - `parse_product(self, response)`: Extract product metadata using `self._jsonld_product_meta` or parsing methods. Gather image URLs and call `self.make_item` to output the canonical `SandalItem`.
5. **Register Spider**:
   Add the new spider class import to `manual_scraper_ext/spiders/crawl_all.py` so it participates in `scrapy crawl all`.
