"""
Phase 3: Parser — extracts products from saved HTML listing pages, outputs JSON.
NO PDF downloads — pdfUrl is constructed from the pattern.
NO individual product page fetches — listing page data only.

Listing tables only expose Image / Part Number / Manufacturer / Description, so fields
like price, quantity, leadTimeDays, countryOfOrigin, RoHS, etc. stay at schema defaults
unless a future PDP (product detail) fetch is added.

Output: data/output/{category}_products.json (JSON array)
"""
import asyncio
import json
import os
from pathlib import Path

from bs4 import BeautifulSoup

import config
from utils import ensure_dirs, build_pdf_url, build_product_url, now_utc


EXCLUDED_SLUGS = {
    'category', 'account', 'search', 'manufacturers', 'api-docs',
    'terms', 'privacy-policy', 'updated-parts', 'boms', 'bom',
}


def extract_products(html, category_path, breadcrumb):
    """Extract product rows from a listing page HTML."""
    soup = BeautifulSoup(html, 'lxml')
    products = []

    for row in soup.select('table tbody tr'):
        cells = row.find_all('td')
        if len(cells) < 3:
            continue

        # Find product link — skip image thumbnails
        links = row.select('a[href]')
        slug_mfr = slug_pn = None
        for l in links:
            href = l.get('href', '').strip('/')
            parts = href.split('/')
            if len(parts) == 2 and parts[0] not in EXCLUDED_SLUGS and parts[1] not in EXCLUDED_SLUGS:
                text = l.get_text(strip=True)
                # Skip image "No Image" thumbnails
                if text and text != 'No Image' and not text.startswith('http'):
                    slug_mfr, slug_pn = parts
                    break

        if not slug_mfr:
            continue

        # Part number — first cell text
        part_number_cell = cells[0].get_text(strip=True)
        if not part_number_cell or part_number_cell == 'No Image':
            part_number_cell = slug_pn.replace('-', ' ').upper()

        # Manufacturer — third cell text
        manufacturer_cell = cells[2].get_text(strip=True) if len(cells) >= 3 else ''

        # Title/description — last cell text
        description = cells[-1].get_text(strip=True)

        # Image URL
        img = row.select_one('img[src]')
        image_url = img.get('src', '') if img else ''

        # Clean up manufacturer name
        manufacturer = manufacturer_cell.title()

        products.append({
            'manufacturer': manufacturer,
            'partNumber': part_number_cell.replace('-', ' ').upper(),
            'slug_mfr': slug_mfr,
            'slug_pn': slug_pn,
            'title': description,
            'imageUrl': image_url,
            'productUrl': build_product_url(slug_mfr, slug_pn),
            'pdfUrl': build_pdf_url(slug_mfr, slug_pn),
            'category_path': category_path,
            'breadcrumb': breadcrumb,
        })

    return products


def build_record(prod):
    """Build the final JSON record (target shape; commerce fields need PDP scrape)."""
    # category_path = test-measurement/inspection-equipment/calipers-micrometers/general
    # root = test-measurement, breadcrumb = [inspection-equipment, calipers-micrometers, general]
    category = [prod['category_path'].split('/')[0]] + prod['breadcrumb']

    return {
        # Intermediate record — consumed by Phase 4 which overwrites with full schema.
        # Fields here are used only as backfill when PDP HTML parse fails.
        'partNumber': prod['partNumber'],
        'productUrl': prod['productUrl'],
        'title': prod['title'],
        'manufacturer': prod['manufacturer'],
        'category': category,
        'pdfUrl': prod['pdfUrl'],
        'fetchedAt': now_utc(),
    }


async def run_phase3(category, limit=None):
    html_dir = f'data/html/{category}'
    output_file = f'data/output/{category}_products.json'
    urls_file = f'data/urls/listing_urls_{category}.jsonl'

    ensure_dirs(os.path.dirname(output_file))

    # Load ALL URL metadata from listing_urls_all.jsonl (covers all categories)
    all_urls_file = 'data/urls/listing_urls_all.jsonl'
    url_meta = {}
    for metafile in [urls_file, all_urls_file]:
        if os.path.exists(metafile):
            with open(metafile) as f:
                for line in f:
                    obj = json.loads(line)
                    url_meta[obj['url']] = obj

    # Also build a reverse index by category_path for pages without exact URL match
    by_cat_path = {}
    for obj in url_meta.values():
        cp = obj.get('category_path', '')
        if cp and cp not in by_cat_path:
            by_cat_path[cp] = obj

    html_files = sorted(Path(html_dir).glob('*.html'))
    if not html_files:
        print(f'[phase3] No HTML files in {html_dir}')
        return

    print(f'[phase3] [{category}] Processing {len(html_files)} HTML files')

    import re

    def normalize_key(s):
        return re.sub(r'[\s\-]+', '', s.lower().strip())

    def category_depth(fp):
        # Extract category depth from filename for sorting
        # e.g. https_www_datasheets_com_category_test_measurement_test_accessories_probes_leads_clips_page_1
        stem = fp.stem.replace('https_www_datasheets_com_category_', '')
        segs = stem.replace('_page_', '/_page_').split('/')[0].split('_')
        return len(segs)

    # Process deepest categories first so deeper breadcrumbs overwrite shallow ones
    html_files = sorted(html_files, key=category_depth, reverse=True)
    print(f'[phase3] [{category}] Sorted {len(html_files)} files by depth (deepest first)')

    all_records = []
    seen_keys = set()

    for fp in html_files:
        with open(fp, encoding='utf-8') as f:
            html = f.read()

        # Derive listing URL from filename
        # Pattern: https_www_datasheets_com_category_test_measurement_test_accessories_page_3.html
        stem = fp.stem
        if '_page_' in stem:
            parts = stem.split('_page_')
            cat_path = parts[0].replace('https_www_datasheets_com_category_', '').replace('_', '/')
            page = parts[1]
            url = f'https://www.datasheets.com/category/{cat_path}?page={page}'
        else:
            cat_path = stem.replace('https_www_datasheets_com_category_', '').replace('_', '/')
            url = f'https://www.datasheets.com/category/{cat_path}'

        # Try exact URL match first, then fallback to category_path
        meta = url_meta.get(url) or by_cat_path.get(cat_path, {})
        breadcrumb = meta.get('breadcrumb', [])
        category_path = meta.get('category_path', cat_path)

        products = extract_products(html, category_path, breadcrumb)

        for prod in products:
            record = build_record(prod)
            # Deduplicate by (normalized partNumber, manufacturer)
            key = (normalize_key(record['partNumber']), record['manufacturer'].lower().strip())
            if key not in seen_keys:
                seen_keys.add(key)
                all_records.append(record)
                if limit and len(all_records) >= limit:
                    break

        if limit and len(all_records) >= limit:
            print(f'[phase3] [{category}] reached --limit {limit}; stopping parse')
            break

        if len(all_records) % 500 == 0:
            print(f'[phase3] [{category}] {len(all_records)} unique products extracted')

    # Write single JSON array
    with open(output_file, 'w', encoding='utf-8') as out:
        json.dump(all_records, out, ensure_ascii=False, indent=2)

    # Clean up HTML files — no longer needed after parsing
    # DISABLED: keeping HTML files
    # import shutil
    # if os.path.exists(html_dir):
    #     shutil.rmtree(html_dir)
    #     print(f'[phase3] [{category}] Cleaned up {html_dir}')

    print(f'[phase3] [{category}] Done — {len(all_records)} unique products → {output_file}')
    return len(all_records)


async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--category', required=True)
    parser.add_argument('--limit', type=int, default=None,
                        help="Stop after N unique products")
    args = parser.parse_args()
    await run_phase3(args.category.lower().strip(), args.limit)


if __name__ == '__main__':
    asyncio.run(main())
