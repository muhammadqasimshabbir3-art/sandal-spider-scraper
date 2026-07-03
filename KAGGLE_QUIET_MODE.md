# Running Spiders in Quiet Mode (Kaggle/Limited Output)

When running on Kaggle or other environments with output limits, use these commands to minimize logging and show only the progress bar.

## Quick Start: Quiet Mode

### Run with minimal logs (only progress bar + errors):
```bash
scrapy crawl all -s LOG_LEVEL=WARNING
```

### Run specific spider quietly:
```bash
scrapy crawl skechers -s LOG_LEVEL=WARNING
```

### Run multiple spiders, skip some, quiet mode:
```bash
scrapy crawl all -a skip_spiders="selle_sandals,asos" -s LOG_LEVEL=WARNING
```

### Run without saving images (testing only):
```bash
scrapy crawl all -s LOG_LEVEL=WARNING -s SKIP_IMAGE_DOWNLOAD=True
```

## Log Levels

- **WARNING** (recommended for Kaggle): Only warnings and errors
- **ERROR**: Only errors, nothing else
- **INFO** (default): Informational messages + warnings + errors
- **DEBUG**: Verbose debug output (don't use on Kaggle)

## What You'll See

### Normal Mode (INFO level):
```
2026-07-03 15:24:13 [scrapy.utils.log] INFO: Scrapy 2.16.0 started...
2026-07-03 15:24:13 [all] INFO: [BrowserRendering] Middleware active...
2026-07-03 15:24:13 [all] INFO: Queueing start URLs for spider: skechers
...lots more logs...
[Images] Downloaded: 45 | Failed: 2 | ETA: 14:35:22 (0.5m) | 50%|████████░░| 100/200
```

### Quiet Mode (WARNING level):
```
[Images] Downloaded: 45 | Failed: 2 | ETA: 14:35:22 (0.5m) | 50%|████████░░| 100/200
```

## Stop the Crawler

Press **Ctrl+C** once to gracefully shut down:
```bash
^C  # This closes the spider cleanly
```

Press **Ctrl+C** again to force-stop immediately:
```bash
^C^C  # This forces immediate shutdown
```

## Example Kaggle Usage

```python
import subprocess
import os

os.chdir('/path/to/sandal-spider-scraper')

# Run quietly, skip selle_sandals, no image storage
cmd = [
    'scrapy', 'crawl', 'all',
    '-a', 'skip_spiders=selle_sandals',
    '-s', 'LOG_LEVEL=WARNING',
    '-s', 'SKIP_IMAGE_DOWNLOAD=True'
]

subprocess.run(cmd)
```

## Settings File Configuration

Alternatively, edit `manual_scraper_ext/settings.py`:

```python
# For quiet mode:
LOG_LEVEL = 'WARNING'
LOGSTATS_INTERVAL = 0  # Disable periodic stats logging

# For no image downloads:
SKIP_IMAGE_DOWNLOAD = True

# To skip spiders:
EXCLUDED_SPIDERS = ['selle_sandals', 'asos']
```

Then run:
```bash
scrapy crawl all
```

## Output Estimate

On Kaggle with 100+ products and images:
- **Normal mode (INFO)**: ~200-500 lines of logs
- **Quiet mode (WARNING)**: ~1-5 lines of logs (just the progress bar)

This saves **95%+ of output quota**.
