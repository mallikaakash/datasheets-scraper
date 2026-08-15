BASE_URL = "https://www.datasheets.com"
START_CATEGORY = "sensors-transducers"

# Smallest root catalog on the site (~3k parts); good for pipeline smoke tests.
# (Verified by comparing /category/<slug> product counts.)
SMALL_TEST_CATEGORY_SLUG = "uncategorized"

CATEGORIES_TO_SCRAPE = [
    "sensors-transducers",
    "circuit-protection",
    "uncategorized",
    "semiconductors",
    "wire-cable-assemblies",
    "tools-production-supplies",
    "computing-embedded-systems",
    "connectors-interconnects",
    "industrial-automation-control",
    "hardware-enclosures-fasteners",
    "test-measurement",
    "passive-components",
    "optoelectronics-displays",
    "electromechanical",
    "rf-wireless",
    "power-products",
    "development-prototyping",
    "thermal-management",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

PHASE1_CONCURRENCY = 10
PHASE1_DELAY_MS = 300

PHASE2_CONCURRENCY = 5
PHASE2_DELAY_MS = 500

PHASE3_CONCURRENCY = 30
PHASE3_DELAY_MS = 400

REQUEST_TIMEOUT = 30.0
MAX_RETRIES = 4
BACKOFF_BASE_MS = 2000

PRODUCTS_PER_PAGE = 20

DATA_DIR = "data"
URLS_DIR = f"{DATA_DIR}/urls"
HTML_DIR = f"{DATA_DIR}/html"
PDF_DIR = f"{DATA_DIR}/pdfs"
OUTPUT_FILE = f"{DATA_DIR}/output/results.jsonl"

# Prices are stored in the distributors' native currencies (USD/GBP/SGD),
# exactly as datasheets.com's pricing API returns them. No FX conversion is
# performed — see PRICING.md.
