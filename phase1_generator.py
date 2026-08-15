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
from urllib.parse import urlparse

from bs4 import BeautifulSoup
import config
from http_client import get as http_get, is_challenge
from utils import ensure_dirs


def extract_total_products(html):
    m = re.search(r'text-2xl font-bold text-brand-500.*?children.*?([\d,]+)', html)
    return int(m.group(1).replace(',', '')) if m else 0


async def fetch_html(url):
    try:
        return await asyncio.to_thread(http_get, url, 25.0)
    except Exception as e:
        print(f"[phase1] fetch exception: {e}")
        return None


_CATEGORY_PATH_RE = re.compile(
    r'/category/[a-z0-9][a-z0-9\-]*(?:/[a-z0-9][a-z0-9\-]*)*',
    re.I,
)


def discover_subcats_from_html(html, category, breadcrumb):
    """
    Immediate child slugs under /category/{category}/{breadcrumb...}/.

    Next.js RSC payloads embed /category/... paths in script strings, not only in
    <a href>. Regex over the full HTML finds those; anchors are merged in too.
    """
    prefix = f"/category/{'/'.join([category] + breadcrumb)}"
    children = set()

    for m in _CATEGORY_PATH_RE.finditer(html):
        path = m.group(0).rstrip('/')
        if path == prefix or not path.startswith(prefix + '/'):
            continue
        rest = path[len(prefix) + 1 :]
        slug = rest.split('/')[0]
        if not slug or slug.startswith('error-'):
            continue
        if slug in breadcrumb or slug == category:
            continue
        children.add(slug)

    soup = BeautifulSoup(html, 'lxml')
    for link in soup.select('a[href*="/category/"]'):
        href = link.get('href', '') or ''
        if '?' in href:
            href = href.split('?', 1)[0]
        if href.startswith('http'):
            try:
                href = urlparse(href).path or ''
            except Exception:
                continue
        if not href.startswith('/'):
            continue
        path = href.rstrip('/')
        if path == prefix or not path.startswith(prefix + '/'):
            continue
        rest = path[len(prefix) + 1 :]
        slug = rest.split('/')[0]
        if slug and not slug.startswith('error-') and slug not in breadcrumb and slug != category:
            children.add(slug)

    return sorted(children)


async def run_phase1(category, limit=None):
    output_file = f"data/urls/listing_urls_{category}.jsonl"
    ensure_dirs(os.path.dirname(output_file))

    listing_urls = []
    visited = set()
    queue = [(f"{config.BASE_URL}/category/{category}", [])]
    visited.add(f"{config.BASE_URL}/category/{category}")

    pages_visited = 0
    max_listing_pages = None
    if limit:
        max_listing_pages = (limit + config.PRODUCTS_PER_PAGE - 1) // config.PRODUCTS_PER_PAGE
        print(f"[phase1] [{category}] --limit {limit} → at most {max_listing_pages} listing pages")

    while queue:
        url, breadcrumb = queue.pop()
        pages_visited += 1
        category_path = '/'.join([category] + breadcrumb)
        indent = '  ' * len(breadcrumb)

        resp = await fetch_html(url)
        if not resp or resp.status_code != 200 or len(resp.content) < 5000:
            reason = "no response"
            if resp is not None:
                challenge = is_challenge(resp.status_code, resp.text)
                reason = f"status={resp.status_code} bytes={len(resp.content)}"
                if challenge:
                    reason += " Cloudflare-challenge"
            print(f"[phase1] {indent}{category_path} | fetch failed ({reason})")
            if pages_visited % 20 == 0:
                print(f"[phase1] progress: {pages_visited} pages visited, {len(listing_urls)} listing URLs")
            continue

        html = resp.text
        subcats = discover_subcats_from_html(html, category, breadcrumb)

        if subcats:
            print(f"[phase1] {indent}{category_path} -> {subcats}")
            for sc in subcats:
                next_bc = breadcrumb + [sc]
                next_url = f"{config.BASE_URL}/category/{category}/{'/'.join(next_bc)}"
                if next_url not in visited:
                    visited.add(next_url)
                    queue.append((next_url, next_bc))

        total = extract_total_products(html)
        if total > 0:
            if subcats:
                # Parent and child listing pages repeat the same parts; only scrape leaves.
                print(
                    f"[phase1] {indent}{category_path} | {total} products | "
                    f"SKIPPED (non-leaf, {len(subcats)} subcats)"
                )
            else:
                pages = (total + config.PRODUCTS_PER_PAGE - 1) // config.PRODUCTS_PER_PAGE
                if max_listing_pages is not None:
                    remaining = max_listing_pages - len(listing_urls)
                    if remaining <= 0:
                        break
                    pages = min(pages, remaining)
                depth = "root leaf" if not breadcrumb else "leaf"
                print(f"[phase1] {indent}{category_path} | {total} products | {pages} pages ({depth})")
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
            print(f"[phase1] {indent}{category_path} | no products found")

        if pages_visited % 20 == 0:
            print(f"[phase1] progress: {pages_visited} pages visited, {len(listing_urls)} listing URLs")

        if max_listing_pages is not None and len(listing_urls) >= max_listing_pages:
            print(f"[phase1] [{category}] reached listing-page cap ({max_listing_pages}); stopping DFS")
            break

    if not listing_urls:
        print(
            f"[phase1] [{category}] 0 listing URLs — not overwriting {output_file} "
            f"(previous file kept if it exists)"
        )
        return 0

    with open(output_file, 'w') as f:
        for item in listing_urls:
            f.write(json.dumps(item) + '\n')

    print(f"[phase1] [{category}] Done -- {pages_visited} pages, {len(listing_urls)} listing URLs -> {output_file}")
    return len(listing_urls)


async def main():
    parser = argparse.ArgumentParser(description="Phase 1: Generate listing URLs for a category")
    parser.add_argument('-c', '--category', required=True, help="Category slug")
    parser.add_argument('--merge', action='store_true', help="Merge into listing_urls_all.jsonl")
    parser.add_argument('--limit', type=int, default=None,
                        help="Stop after enough listing pages to cover N products")
    args = parser.parse_args()

    category = args.category.lower().strip()
    count = await run_phase1(category, args.limit)
    print(f"[phase1] [{category}] {count} URLs generated")
    if count == 0:
        print(
            "[phase1] No listing URLs generated. The first page did not return catalog HTML. "
            "Re-run and wait for the Chrome window to finish the Cloudflare check."
        )
        raise SystemExit(1)


if __name__ == '__main__':
    asyncio.run(main())
