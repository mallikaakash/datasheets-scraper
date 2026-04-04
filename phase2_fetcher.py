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
            existing.add(fpath.name)  # use full filename with extension

    todo = []
    for it in items:
        url = it["url"]
        # Skip malformed/relative URLs
        if not url.startswith('https://'):
            print(f"[phase2] SKIP bad URL: {url[:80]}")
            continue
        if safe_filename(url) not in existing:
            todo.append(it)

    range_label = f"[{start or 1}-{end or len(items)}]"
    print(f"[phase2] [{category}] {range_label} Total: {len(items)} | Already on disk: {len(existing)} | To fetch: {len(todo)}")
    if not todo:
        return

    semaphore = asyncio.Semaphore(concurrency)

    async def fetch_one(item):
        async with semaphore:
            resp = await fetch_with_retry(item["url"])
            if resp is None:
                return item["url"], None, "failed_after_retries"
            if resp.status_code == 200 and len(resp.content) > 5000:
                return item["url"], resp.text, None
            return item["url"], None, f"status={resp.status_code} size={len(resp.content)}"

    saved = errors = 0
    batch_size = concurrency * 2  # smaller batches to avoid triggering rate limits

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
            await asyncio.sleep(2.0)  # pause between batches to avoid rate limits

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
