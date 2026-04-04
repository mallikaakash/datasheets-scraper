"""
Phase 1: DFS Category + Listing URL Generator using curl_cffi.
curl_cffi handles the JS-rendered HTML from datasheets.com fine.
Output: data/urls/listing_urls_{category}.jsonl
"""
import argparse
import asyncio
import json
import os
import re

from bs4 import BeautifulSoup
from curl_cffi import requests as curl

import config
from utils import ensure_dirs


def extract_total_products(html):
    m = re.search(r'text-2xl font-bold text-brand-500.*?children.*?([\d,]+)', html)
    return int(m.group(1).replace(',', '')) if m else 0


async def fetch_html(url):
    try:
        return await asyncio.to_thread(curl.get, url, impersonate='chrome', timeout=20.0)
    except Exception:
        return None


async def discover_subcats(url, current_slug):
    """Discover subcategories one level deeper than current_slug using curl_cffi."""
    resp = await fetch_html(url)
    if not resp or resp.status_code != 200 or len(resp.content) < 5000:
        return []

    soup = BeautifulSoup(resp.text, 'lxml')
    subcats = []
    seen = set()

    for link in soup.select('a[href*="/category/"]'):
        href = link.get('href', '')
        if not href.startswith('/') or '?' in href:
            continue

        segs = href.strip('/').split('/')

        try:
            idx = segs.index(current_slug)
        except ValueError:
            continue

        path_below = segs[idx + 1:]
        if len(path_below) != 1:
            continue

        slug = path_below[0]
        if slug in seen:
            continue
        seen.add(slug)
        subcats.append(slug)

    return subcats


async def run_phase1(category):
    output_file = f"data/urls/listing_urls_{category}.jsonl"
    ensure_dirs(os.path.dirname(output_file))

    listing_urls = []
    visited = set()
    queue = [(f"{config.BASE_URL}/category/{category}", [])]
    visited.add(f"{config.BASE_URL}/category/{category}")

    pages_visited = 0

    while queue:
        url, breadcrumb = queue.pop()
        pages_visited += 1
        category_path = '/'.join([category] + breadcrumb)
        indent = '  ' * len(breadcrumb)

        current_slug = breadcrumb[-1] if breadcrumb else category
        subcats = await discover_subcats(url, current_slug)

        if subcats:
            print(f"[phase1] {indent}{category_path} -> {subcats}")
            for sc in subcats:
                next_bc = breadcrumb + [sc]
                next_url = f"{config.BASE_URL}/category/{category}/{'/'.join(next_bc)}"
                if next_url not in visited:
                    visited.add(next_url)
                    queue.append((next_url, next_bc))

        resp = await fetch_html(url)
        if resp and resp.status_code == 200 and len(resp.content) > 5000:
            total = extract_total_products(resp.text)
            if total > 0:
                if breadcrumb:  # skip root category, only nested subcategory URLs
                    pages = (total + config.PRODUCTS_PER_PAGE - 1) // config.PRODUCTS_PER_PAGE
                    print(f"[phase1] {indent}{category_path} | {total} products | {pages} pages")
                    for pnum in range(1, pages + 1):
                        if pnum == 1:
                            listing_url = f"{config.BASE_URL}/category/{category_path}"
                        else:
                            listing_url = f"{config.BASE_URL}/category/{category_path}?page={pnum}"
                        listing_urls.append({
                            "url": listing_url,
                            "breadcrumb": breadcrumb,
                            "total_products": total,
                            "category_path": category_path,
                        })
                else:
                    print(f"[phase1] {indent}{category_path} | {total} products | SKIPPED (root)")
            else:
                print(f"[phase1] {indent}{category_path} | no products found")
        else:
            print(f"[phase1] {indent}{category_path} | fetch failed")

        if pages_visited % 20 == 0:
            print(f"[phase1] progress: {pages_visited} pages visited, {len(listing_urls)} listing URLs")

    with open(output_file, 'w') as f:
        for item in listing_urls:
            f.write(json.dumps(item) + '\n')

    print(f"[phase1] [{category}] Done -- {pages_visited} pages, {len(listing_urls)} listing URLs -> {output_file}")
    return len(listing_urls)


async def main():
    parser = argparse.ArgumentParser(description="Phase 1: Generate listing URLs for a category")
    parser.add_argument('-c', '--category', required=True, help="Category slug")
    parser.add_argument('--merge', action='store_true', help="Merge into listing_urls_all.jsonl")
    args = parser.parse_args()

    count = await run_phase1(args.category)
    print(f"[phase1] [{args.category}] {count} URLs generated")


if __name__ == '__main__':
    asyncio.run(main())
