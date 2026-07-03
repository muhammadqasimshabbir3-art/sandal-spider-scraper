import logging
import scrapy
from scrapy.crawler import CrawlerProcess
from scrapy.utils.project import get_project_settings
from manual_scraper_ext.spiders.crawl_all import AllSandalsSpider

# Set up logging to print everything to console
logging.basicConfig(level=logging.DEBUG)

settings = get_project_settings()
# Disable AutoThrottle and other delays for quick test
settings.set('AUTOTHROTTLE_ENABLED', False)
settings.set('DOWNLOAD_DELAY', 0)
settings.set('CONCURRENT_REQUESTS', 1)

process = CrawlerProcess(settings)
process.crawl(AllSandalsSpider)
process.start()
