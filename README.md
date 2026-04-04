# Datasheets.com Scraper

A 3-phase scraper for datasheets.com using `curl_cffi` (Chrome TLS impersonation) with `asyncio` concurrency.

## Setup

```bash
pip install -r requirements.txt
```

## 3-Phase Architecture

### Phase 1 — Generate Listing URLs (DFS category crawler)

```bash
python phase1_generator.py -c <category-slug>
```

Example:
```bash
python phase1_generator.py -c sensors-transducers
python phase1_generator.py -c semiconductors
```

Output: `data/urls/listing_urls_<category>.jsonl`

This crawls the category hierarchy using DFS, discovers all subcategories, and generates paginated listing URLs for every node (root → leaf). Each listing URL entry contains:

- `url`: the full listing page URL
- `breadcrumb`: the DFS path taken to reach this node
- `total_products`: total products in this category
- `category_path`: the full slash-separated path (e.g. `sensors-transducers/pressure-sensors/board-mount`)

### Phase 2 — Download HTML

```bash
python phase2_fetcher.py -c <category> [options]
```

Options:
- `--concurrency N` — concurrent workers (default: 10, safe to run 2-3 instances in parallel)
- `--start N` — start at URL index N (1-indexed)
- `--end N` — end at URL index N (1-indexed)

For parallel execution across multiple terminals:
```bash
# Terminal 1: first half
python phase2_fetcher.py -c sensors-transducers --start 1 --end 44000

# Terminal 2: second half
python phase2_fetcher.py -c sensors-transducers --start 44001 --end 88512
```

Output: `data/html/<category>/*.html`

Resume-safe: already-downloaded files are skipped.

### Phase 3 — Parse and Build JSON

```bash
python phase3_parser.py -c <category>
```

Output: `data/output/<category>_products.json` (single JSON array)

Deduplicates by `(normalized_partNumber, manufacturer)`. Processes deepest category files first so products get assigned the most specific category. Deletes HTML files after parsing.

## Workflow

For a complete scrape:

```bash
# 1. Generate URLs (do once per category)
python phase1_generator.py -c <category>

# 2. Download HTML (run 2-3 terminals in parallel)
python phase2_fetcher.py -c <category> --concurrency 10

# 3. Parse to JSON
python phase3_parser.py -c <category>
```

## Notes

- Phase 2 may hit Cloudflare rate limits on specific subcategories (`envi`, `opti`, `spec`). The script handles retries with exponential backoff but some URLs may fail. Resume will skip already-downloaded files.
- `pdfUrl` is constructed from manufacturer + part number, not scraped.
- `pdfHash` and `manufacturerMetadata` fields are empty placeholders.
- Phase 3 cleans up HTML files automatically after parsing.
