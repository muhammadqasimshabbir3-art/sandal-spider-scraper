import scrapy
from scrapy.crawler import CrawlerProcess

class SaveNordstrom(scrapy.Spider):
    name = 'save_nordstrom'
    start_urls = [
        'https://www.nordstrom.com/browse/men/shoes/sandals?breadcrumb=Home%2FMen%2FShoes%2FSandals%20%26%20Flip-Flops&origin=topnav'
    ]
    custom_settings = {
        'DOWNLOAD_HANDLERS': {
            'http': 'scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler',
            'https': 'scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler',
        },
        'PLAYWRIGHT_LAUNCH_OPTIONS': {'headless': True},
        'PLAYWRIGHT_DEFAULT_NAVIGATION_TIMEOUT': 30000,
        'LOG_LEVEL': 'INFO',
    }

    def parse(self, response):
        path = '/tmp/nordstrom_cat.html'
        with open(path, 'w', encoding='utf-8') as f:
            f.write(response.text)
        self.log(f'Wrote rendered page to {path}')

if __name__ == '__main__':
    process = CrawlerProcess()
    process.crawl(SaveNordstrom)
    process.start()
