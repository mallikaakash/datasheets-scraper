# Datasheets.com Scraper

Scrapes electronic-component data from [datasheets.com](https://www.datasheets.com) —
part numbers, specs, distributor pricing, and datasheet links — and writes one
JSON record per product.



It uses `curl_cffi` (Chrome TLS impersonation) + `asyncio` for fast, block-resistant
fetching, and Playwright to clear the initial Cloudflare challenge once per session.

---

## 1. Setup (one time)

**Requires Python 3.10+.**

```bash
# 1. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Install the Chromium build Playwright needs for the Cloudflare step
playwright install chromium
```

> **Note:** Always run the scraper from the same environment where you installed
> the dependencies. If you see `ModuleNotFoundError: No module named 'pypdf'`,
> you're running a different Python (e.g. system/conda `base`) — re-activate the
> venv.

---

## 2. Quick start

Scrape one category, end to end. config.py file contains the parameter format and slug name

```bash
python main.py -c uncategorized
```

Do a small test run first (caps the number of products):

```bash
python main.py -c uncategorized --limit 300
```

**First run only:** a Chrome window opens to pass Cloudflare. Leave it in front
until the datasheets.com homepage appears (~90s). The session cookie is saved to
`data/.cf_session.json` and reused, so later runs don't prompt again.

**Output:** `data/output/<category>_final.jsonl` — one JSON object per line.

---

## 3. What runs

`main.py` runs a 4-phase pipeline. By default **all four phases run**:

| Phase | Script | Does | Writes |
|------:|--------|------|--------|
| 1 | `phase1_generator.py` | Crawl category tree → paginated listing URLs | `data/urls/listing_urls_<cat>.jsonl` |
| 2 | `phase2_fetcher.py` | Download listing-page HTML (resume-safe) | `data/html/<cat>/*.html` |
| 3 | `phase3_parser.py` | Parse listings → product records, dedup | `data/output/<cat>_products.json` |
| 4 | `phase4_pdp.py` | Visit each product page + pricing API → enriched records | `data/output/<cat>_final.jsonl` |

**The `_final.jsonl` from Phase 4 is the deliverable.** Phases 1–3 build the input
Phase 4 needs.

---

## 4. Common commands

```bash
# Full pipeline for one category
python main.py -c sensors-transducers

# Test run (cap products)
python main.py -c sensors-transducers --limit 300

# Listing-level only — skip the slow per-product Phase 4
python main.py -c sensors-transducers --no-pdp

# Re-run Phase 4 only (reuses existing *_products.json)
python main.py -c sensors-transducers --phase 4

# Every category in config.py (long — can take a very long time)
python main.py --all-categories --confirm-full-scrape
```

**Resume behaviour:** if `data/output/<cat>_products.json` already exists, a normal
run skips phases 1–3 and goes straight to Phase 4. To force a fresh listing crawl,
delete that file first. Phase 2 is also resume-safe on its own (already-downloaded
HTML is skipped).

Category slugs live in `config.py` (`CATEGORIES_TO_SCRAPE`), as do concurrency,
delay, retry, and timeout knobs.

---

## 5. Pricing & currency — important

This is the part most likely to surprise you, so read it before comparing output
to the website.

**Where prices come from.** For each product, Phase 4 calls the site's own pricing
endpoint:

```
https://www.datasheets.com/api/part-pricing?pn=<partNumber>&manufacturer=<manufacturer>
```

Each distributor offer comes back in its **native currency** — e.g. GBP (Farnell),
USD (Newark / Avnet), SGD (Element14) — with a list of quantity price-breaks.

**We store prices exactly as that API returns them.** `unitPrice` and `currency`
are the distributor's own numbers. **No currency conversion is performed** — we do
not generate INR (or any other display currency). If you need a single currency,
convert the native prices yourself with a rate and timestamp you control.

> An earlier version converted everything to INR using a live FX API. That was
> removed because (a) the values never matched the site's own INR picker — which
> uses the site's own rate and timing — and (b) they were self-generated numbers,
> not data the site actually gave us.

**Why there can be more price-breaks than the website shows.** The visible price
table on a product page often lists only a few tiers (e.g. `100 / 500 / 1000`). The
pricing API frequently returns **more** tiers than the page renders (e.g.
`250 / 2500 / 5000`, or a full `3000 → 48000` ladder). These extra tiers are
**genuine distributor data** — the site's UI simply hides some of them — so we keep
every real tier the API returns. This means the output may legitimately contain
more price-breaks than you see on the page.

**What we drop.** The API pads offers with dummy rows (quantity `0` and/or price
`0.00000`). Those are filtered out, so an offer whose tiers are all zeros comes
through with an empty `priceBreaks` list.

See [PRICING.md](PRICING.md) for a fully worked example.

---

## 6. Output format

`data/output/<category>_final.jsonl` — one product per line. Each record includes:
identity (`partNumber`, `manufacturer`, `productUrl`, `productHeader`, `category`),
`availability`, `technicalSpecification` (description, general specs, compliance,
images), `pricing` (per-distributor offers with native-currency `priceBreaks`),
`datasheet` metadata, and an `audit` block.

---

## 7. Troubleshooting

| Symptom | Fix |
|---------|-----|
| `ModuleNotFoundError: pypdf` (or others) | Wrong Python — activate the venv where you ran `pip install`. |
| Playwright / "Executable doesn't exist" | Run `playwright install chromium`. |
| Cloudflare window keeps reopening | Let the homepage fully load and stay in front; delete `data/.cf_session.json` to reset the session. |
| Some subcategories fail in Phase 2 | Cloudflare rate-limits a few subtrees; retries with backoff are built in, and resume skips what's already downloaded — just re-run. |
| Want to re-fetch fresh data | Phase 4 is resume-safe (it skips products already in `_final.jsonl`). To regenerate from scratch, delete `data/output/<cat>_final.jsonl` (and `<cat>_products.json` / `data/html/<cat>/` to also redo phases 1–3). |

---

## 8. Notes

- `pdfUrl` is constructed from manufacturer + part number, not scraped.
- Phase 3 deletes each category's HTML after parsing to save disk.
- Data output lives under `data/` and is gitignored.
