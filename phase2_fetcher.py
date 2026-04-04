"""
Phase 2: Fast HTML Fetcher using curl_cffi (Chrome TLS impersonation).
Reads listing URLs from data/urls/listing_urls_{category}.jsonl,
downloads HTML, saves to disk.
Output: data/html/{category}/*.html — raw HTML files, resumable.
"""
import asyncio
import json
import os
from pathlib import Path

from curl_cffi import requests as curl

import config
from utils import ensure_dirs, safe_filename


async def fetch_url_async(url):
    try:
        return await asyncio.to_thread(curl.get, url, impersonate="chrome", timeout=20.0)
    except Exception as e:
        return None


async def fetch_with_retry(url, max_retries=5):
    """Fetch with exponential backoff, detect Cloudflare blocks."""
    for attempt in range(max_retries):
        resp = await fetch_url_async(url)
        if resp is None:
            await asyncio.sleep(1.5 ** attempt)
            continue
        # Detect Cloudflare block (short response with challenge)
        if resp.status_code == 403 or (resp.status_code == 200 and len(resp.content) < 10000 and b'cloudflare' in resp.content.lower()):
            await asyncio.sleep(5 * (2 ** attempt))  # much longer backoff for Cloudflare
            continue
        if resp.status_code in (403, 429, 503):
            await asyncio.sleep(2.5 ** attempt)
            continue
        return resp
    return None


async def run_phase2(category, concurrency=30, start=None, end=None):
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
        # Skip URLs with clearly malformed subcategory slugs (truncated at hyphen boundary)
        # e.g. "discrete-" (should be "discrete-semiconductors")
        try:
            subcat_slug = url.split(f"/category/{category}/")[1].split("/")[0].split("?")[0]
            if subcat_slug.endswith('-'):
                skipped_malformed += 1
                continue
        except (IndexError, ValueError):
            skipped_malformed += 1
            continue
        if safe_filename(url) not in existing:
            valid_items.append(it)

    if skipped_malformed:
        print(f"[phase2] SKIPPED {skipped_malformed} malformed URLs")

    range_label = f"[{start or 1}-{end or len(items)}]"
    print(f"[phase2] [{category}] {range_label} Total items: {len(items)} | Already on disk: {len(existing)} | To fetch: {len(valid_items)}")

    if not valid_items:
        print(f"[phase2] [{category}] Nothing to fetch.")
        return

    # Group URLs by subcategory — subcategory is the first path segment after {category}/
    # e.g. https://www.datasheets.com/category/semiconductors/discrete-semiconductors
    # subcategory = "discrete-semiconductors"
    subcats: dict[str, list[dict]] = {}
    for it in valid_items:
        url = it["url"]
        parts = url.split(f"/category/{category}/")[1].split("/")[0].split("?")[0]
        subcats.setdefault(parts, []).append(it)

    print(f"[phase2] [{category}] {len(subcats)} subcategories to probe")

    # Circuit breaker: probe each subcategory. Skip entire subcategory if probe fails.
    # Probe uses the first URL in the subcategory.
    semaphore = asyncio.Semaphore(concurrency)
    skipped_subcats: set[str] = set()

    async def probe_subcat(subcat, subcat_items):
        async with semaphore:
            probe_url = subcat_items[0]["url"]
            resp = await fetch_with_retry(probe_url)
            if resp is None or resp.status_code >= 500:
                return subcat, False
            return subcat, True

    # Run probes in waves to avoid triggering rate limits
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
            await asyncio.sleep(1.0)  # brief pause between probe waves

    # Build final todo: only URLs from working subcategories
    todo = []
    for subcat, subcat_items in subcats.items():
        if subcat not in skipped_subcats:
            todo.extend(subcat_items)

    print(f"[phase2] [{category}] Probing done. {len(todo)} URLs from {len(probed_ok)} subcategories | {len(skipped_subcats)} subcategories blocked")

    if not todo:
        print(f"[phase2] [{category}] All subcategories blocked. Nothing to fetch.")
        return

    async def fetch_one(item):
        async with semaphore:
            resp = await fetch_with_retry(item["url"])
            if resp is None:
                return item["url"], None, "failed_after_retries"
            if resp.status_code == 200 and len(resp.content) > 5000:
                return item["url"], resp.text, None
            return item["url"], None, f"status={resp.status_code} size={len(resp.content)}"

    saved = errors = 0
    batch_size = concurrency * 2

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
            await asyncio.sleep(2.0)

    total = len(list(Path(output_dir).glob("*.html")))
    print(f"[phase2] [{category}] Done — saved:{saved} err:{errors}, {total} files on disk")


async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--category", required=True)
    parser.add_argument("--concurrency", default=10, type=int)
    parser.add_argument("--start", type=int, default=None, help="Start line number (1-indexed)")
    parser.add_argument("--end", type=int, default=None, help="End line number (1-indexed, inclusive)")
    args = parser.parse_args()
    await run_phase2(args.category, args.concurrency, args.start, args.end)


if __name__ == "__main__":
    asyncio.run(main())
