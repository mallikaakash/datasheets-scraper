"""
Phase 2: HTML Fetcher using curl_cffi (TLS fingerprint; matches phase 1).
Reads listing URLs from data/urls/listing_urls_{category}.jsonl,
downloads HTML, saves to disk.
Output: data/html/{category}/*.html — raw HTML files, resumable.
"""
import asyncio
import json
import os
import random
import time
from pathlib import Path

import config
from http_client import get as http_get
from utils import ensure_dirs, safe_filename


def fetch_sync(url):
    """Fetch a URL. Runs in thread pool. Retries 403s (rate limits) with backoff."""
    last = (0, 0, None)
    for attempt in range(6):
        try:
            resp = http_get(url, timeout=45.0)
            if resp is None:
                time.sleep(1.5 + attempt)
                continue
            last = (resp.status_code, len(resp.content), resp.text)
            if resp.status_code == 200 and len(resp.content) > 5000:
                return last
            if resp.status_code == 403:
                time.sleep(2.5 + attempt * 2 + random.random() * 2)
                continue
            return last
        except Exception:
            time.sleep(1.5 + attempt)
    return last


async def fetch_url_async(url):
    try:
        return await asyncio.to_thread(fetch_sync, url)
    except Exception:
        return 0, 0, None


async def run_phase2(category, concurrency=30, start=None, end=None, no_probe=False, limit=None):
    urls_file = f"data/urls/listing_urls_{category}.jsonl"
    if not os.path.exists(urls_file):
        print(f"[phase2] File not found: {urls_file}")
        return

    with open(urls_file) as f:
        items = [json.loads(line) for line in f]

    # Slice by line number (1-indexed for user friendliness)
    if start is not None:
        items = items[start - 1:]
    if end is not None:
        items = items[:end - start + 1]
    if limit is not None:
        max_pages = (limit + config.PRODUCTS_PER_PAGE - 1) // config.PRODUCTS_PER_PAGE
        items = items[:max_pages]
        print(f"[phase2] [{category}] --limit {limit} → fetching at most {len(items)} listing pages")

    output_dir = f"data/html/{category}"
    ensure_dirs(output_dir)

    existing = set()
    for fpath in Path(output_dir).glob("*.html"):
        if fpath.stat().st_size > 5000:
            existing.add(fpath.name)

    # Filter to only valid category URLs
    valid_items = []
    skipped_malformed = 0
    for it in items:
        url = it["url"]
        if not url.startswith('https://www.datasheets.com/category/'):
            skipped_malformed += 1
            continue
        # Skip URLs with malformed subcategory slugs (truncated at hyphen boundary)
        # e.g. "discrete-" (should be "discrete-semiconductors")
        try:
            # Split on /category/{category}/ — strip query string first to avoid ?page= breaking the split
            url_path = url.split("?")[0]
            after_cat = url_path.split(f"/category/{category}/")[1]
            subcat_slug = after_cat.split("/")[0]
            if subcat_slug.endswith('-'):
                skipped_malformed += 1
                continue
        except IndexError:
            # Root category URL (no subcategory) — valid, allow through
            pass
        if safe_filename(url) not in existing:
            valid_items.append(it)

    if skipped_malformed:
        print(f"[phase2] SKIPPED {skipped_malformed} malformed URLs")

    range_label = f"[{start or 1}-{end or len(items)}]"
    print(f"[phase2] [{category}] {range_label} Total items: {len(items)} | Already on disk: {len(existing)} | To fetch: {len(valid_items)}")

    if not valid_items:
        print(f"[phase2] [{category}] Nothing to fetch.")
        return

    # Group URLs by subcategory (root URLs go under key "")
    subcats: dict[str, list[dict]] = {}
    for it in valid_items:
        url = it["url"]
        try:
            url_path = url.split("?")[0]
            after_cat = url_path.split(f"/category/{category}/")[1]
            parts = after_cat.split("/")[0]
            subcats.setdefault(parts, []).append(it)
        except IndexError:
            # Root category URL — group under empty string key
            subcats.setdefault("", []).append(it)

    print(f"[phase2] [{category}] {len(subcats)} subcategories")

    semaphore = asyncio.Semaphore(concurrency)
    skipped_subcats: set[str] = set()

    if not no_probe:
        async def probe_subcat(subcat, subcat_items):
            async with semaphore:
                probe_url = subcat_items[0]["url"]
                status, size, _ = await fetch_url_async(probe_url)
                if status >= 500 or status == 0:
                    return subcat, False
                return subcat, True

        subcat_list = list(subcats.items())
        probed_ok = set()
        wave_size = min(concurrency * 2, len(subcat_list))

        for wave_start in range(0, len(subcat_list), wave_size):
            wave = subcat_list[wave_start:wave_start + wave_size]
            wave_results = await asyncio.gather(*[probe_subcat(sc, items) for sc, items in wave])
            for subcat, ok in wave_results:
                if ok:
                    probed_ok.add(subcat)
                else:
                    skipped_subcats.add(subcat)
                    skipped_count = len(subcats[subcat])
                    print(f"[phase2] SKIP subcategory (blocked): {subcat} ({skipped_count} URLs)")
            if wave_start + wave_size < len(subcat_list):
                await asyncio.sleep(0.5)

    todo = []
    for subcat, subcat_items in subcats.items():
        if subcat not in skipped_subcats:
            todo.extend(subcat_items)

    probed_count = len([s for s in subcats if s not in skipped_subcats])
    print(f"[phase2] [{category}] Probing done. {len(todo)} URLs from {probed_count} subcategories | {len(skipped_subcats)} subcategories blocked")

    if not todo:
        print(f"[phase2] [{category}] All subcategories blocked. Nothing to fetch.")
        return

    async def fetch_one(item):
        async with semaphore:
            status, size, html = await fetch_url_async(item["url"])
            if html and status == 200 and size > 5000:
                return item["url"], html, None
            return item["url"], None, f"status={status} size={size}"

    saved = errors = 0
    batch_size = concurrency * 4  # larger batches since requests are fast

    for i in range(0, len(todo), batch_size):
        batch = todo[i:i + batch_size]
        nb = (len(todo) - 1) // batch_size + 1

        tasks = [fetch_one(item) for item in batch]
        results = await asyncio.gather(*tasks)

        for (url, html, err) in results:
            filename = safe_filename(url)
            filepath = Path(output_dir) / filename
            if html:
                filepath.write_text(html, encoding="utf-8")
                saved += 1
            else:
                errors += 1
                print(f"[phase2] FAIL {url[:60]}: {err}")

        elapsed = (i + len(batch)) / concurrency
        print(f"[phase2] [{category}] Batch {i // batch_size + 1}/{nb} — "
              f"saved:{saved} err:{errors} ({i + len(batch)}/{len(todo)}, ~{elapsed:.0f}s)")

        if i + batch_size < len(todo):
            await asyncio.sleep(0.3)  # small pause between batches

    total = len(list(Path(output_dir).glob("*.html")))
    print(f"[phase2] [{category}] Done — saved:{saved} err:{errors}, {total} files on disk")


async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--category", required=True)
    parser.add_argument("--concurrency", default=10, type=int)
    parser.add_argument("--start", type=int, default=None, help="Start line number (1-indexed)")
    parser.add_argument("--end", type=int, default=None, help="End line number (1-indexed, inclusive)")
    parser.add_argument("--no-probe", action="store_true", help="Skip subcategory probing")
    parser.add_argument("--limit", type=int, default=None,
                        help="Fetch only enough listing pages to cover N products")
    args = parser.parse_args()
    category = args.category.lower().strip()
    await run_phase2(category, args.concurrency, args.start, args.end, args.no_probe, args.limit)


if __name__ == "__main__":
    asyncio.run(main())
