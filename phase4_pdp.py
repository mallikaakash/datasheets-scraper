"""
Phase 4: Product Detail Page (PDP) scraper + parser.

Reads product URLs from data/output/{category}_products.json (Phase 3 output).
Fetches each PDP with curl_cffi (handles Next.js SSR), extracts all Must-Have
fields per the data model spec.

Output: data/output/{category}_final.jsonl  (one JSON object per line)
"""
import argparse
import asyncio
import hashlib
import io
import json
import os
import random
import re
from datetime import datetime, timezone
from urllib.parse import urlencode

import pypdf
from bs4 import BeautifulSoup
import config
from http_client import get as http_get
from utils import ensure_dirs


# ---------------------------------------------------------------------------
# HTTP fetch
# ---------------------------------------------------------------------------

SEMAPHORE: asyncio.Semaphore = asyncio.Semaphore(1)


async def fetch_html(url: str, retries: int = 5) -> str | None:
    for attempt in range(retries):
        try:
            resp = await asyncio.to_thread(http_get, url, config.REQUEST_TIMEOUT)
            if resp.status_code == 200 and len(resp.content) > 2000:
                return resp.text
            if resp.status_code in (404, 410):
                return None
            if resp.status_code == 429:
                wait = (2 ** attempt) * 5 + random.uniform(2, 5)
                await asyncio.sleep(wait)
                continue
            if resp.status_code == 403:
                wait = (2 ** attempt) * 10 + random.uniform(5, 10)
                await asyncio.sleep(wait)
                continue
        except Exception:
            pass
        if attempt < retries - 1:
            await asyncio.sleep(2 * (attempt + 1))
    return None


# ---------------------------------------------------------------------------
# PDF download + processing
# ---------------------------------------------------------------------------

async def fetch_pdf_bytes(pdf_url: str) -> bytes | None:
    """Download PDF and return raw bytes, or None on failure."""
    for attempt in range(3):
        try:
            resp = await asyncio.to_thread(http_get, pdf_url, 30.0)
            if resp.status_code == 200 and len(resp.content) > 100:
                return resp.content
        except Exception:
            pass
        if attempt < 2:
            await asyncio.sleep(2 * (attempt + 1))
    return None


def process_pdf(pdf_bytes: bytes) -> tuple[str, dict]:
    """
    Returns (sha256_hex, pdfMetadata dict).
    pdfMetadata contains page count, document info, and short text preview.
    """
    pdf_hash = hashlib.sha256(pdf_bytes).hexdigest()

    metadata: dict = {}
    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        num_pages = len(reader.pages)

        doc_info = reader.metadata or {}
        text_parts: list[str] = []
        for page in reader.pages[:5]:
            try:
                t = page.extract_text() or ''
                if t.strip():
                    text_parts.append(t.strip())
            except Exception:
                pass
        text_preview = ' '.join(text_parts)[:1000]

        metadata = {
            'pageCount': num_pages,
            'title': str(doc_info.get('/Title') or ''),
            'author': str(doc_info.get('/Author') or ''),
            'subject': str(doc_info.get('/Subject') or ''),
            'creator': str(doc_info.get('/Creator') or ''),
            'textPreview': text_preview,
        }
    except Exception:
        pass

    return pdf_hash, metadata


# ---------------------------------------------------------------------------
# Next.js data extraction
# datasheets.com uses App Router (Next.js 13+) — data is in RSC flight scripts
# (self.__next_f.push), NOT in __NEXT_DATA__.
# ---------------------------------------------------------------------------

def extract_next_data(html: str) -> dict:
    """Legacy __NEXT_DATA__ extraction — returns {} for App Router sites."""
    m = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', html, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except Exception:
        return {}


def extract_rsc_part_data(html: str) -> dict:
    """
    Extract the 'part' product object from Next.js RSC flight data.
    datasheets.com App Router embeds the full product JSON inside
    self.__next_f.push([1, "..."]) script tags.
    Returns the 'part' dict or {} if not found.
    """
    decoder = json.JSONDecoder()

    for script_m in re.finditer(
        r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', html, re.S
    ):
        raw = script_m.group(1)
        try:
            content = json.loads('"' + raw + '"')
        except Exception:
            content = (
                raw.replace('\\"', '"')
                   .replace('\\\\', '\\')
                   .replace('\\n', '\n')
                   .replace('\\t', '\t')
                   .replace('\\r', '\r')
            )

        idx = content.find('"part":{')
        if idx == -1:
            continue

        part_start = idx + len('"part":')
        try:
            part_obj, _ = decoder.raw_decode(content, part_start)
            if isinstance(part_obj, dict) and (
                part_obj.get('mpn') or part_obj.get('partNumber')
            ):
                return part_obj
        except Exception:
            pass

    return {}


def parse_rsc_specs(specs_list: list) -> tuple[dict, dict]:
    """
    Convert RSC specs format to (general, compliance) dicts.
    RSC format: [{"attribute": {"name": "X", "group": "General"}, "displayValue": "Y"}]
    Values stored as raw strings — no unit parsing per spec.

    NOTE: datasheets.com puts compliance attrs in the General group (not a Compliance group),
    so we scan every item's name for compliance keywords regardless of group.
    """
    general: dict = {}
    rohs = reach = lead_free = None
    approvals: list = []
    flammability = None

    for item in specs_list:
        if not isinstance(item, dict):
            continue
        attr = item.get('attribute') or {}
        name = attr.get('name', '').strip()
        group = attr.get('group', '').strip()
        value = str(item.get('displayValue') or '').strip()

        if not name or not value:
            continue

        nl = name.lower()
        vl = value.lower()

        # Detect compliance fields by name regardless of group
        is_compliance = group in ('Compliance', 'Regulatory')

        if 'rohs' in nl:
            is_compliance = True
            if 'non' not in vl and vl not in ('unknown', 'n/a', ''):
                rohs = vl in ('compliant', 'yes', 'true')
            elif 'unknown' not in vl and 'n/a' not in vl:
                rohs = False
        elif 'reach' in nl:
            is_compliance = True
            if 'unknown' in vl or 'n/a' in vl or not vl:
                reach = None
            else:
                reach = 'non' not in vl and vl in ('compliant', 'yes', 'true')
        elif 'lead' in nl and ('free' in nl or 'pb' in nl):
            is_compliance = True
            lead_free = 'non' not in vl and vl in ('compliant', 'yes', 'true', 'lead free')
        elif 'flammab' in nl or 'ul94' in vl or (group in ('Compliance', 'Regulatory') and 'ul' in vl):
            is_compliance = True
            flammability = value
        elif is_compliance:
            approvals.append(name)

        if not is_compliance:
            general[name] = value

    compliance = {
        'rohs': rohs,
        'reach': reach,
        'leadFree': lead_free,
        'approvals': approvals,
        'flammabilityRating': flammability,
    }
    return general, compliance


def parse_rsc_images(images_list: list) -> list:
    """Extract absolute image URLs from RSC images array: [{"url": "..."}]"""
    urls = []
    seen: set = set()
    for item in images_list:
        if isinstance(item, dict):
            url = item.get('url') or item.get('src') or ''
        elif isinstance(item, str):
            url = item
        else:
            continue
        url = url.strip()
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def parse_rsc_breadcrumb(part: dict) -> list:
    """Extract ordered category breadcrumb from RSC part data."""
    # Try category.path: "Hardware, Enclosures & Fasteners/Hardware/Shielding/EMI Gaskets"
    cat_obj = part.get('category') or {}
    if isinstance(cat_obj, dict):
        path = cat_obj.get('path') or ''
        if path:
            parts = [c.strip() for c in path.split('/') if c.strip()]
            # Deduplicate consecutive identical entries (site repeats leaf for shallow cats)
            deduped = [parts[0]] if parts else []
            for p in parts[1:]:
                if p != deduped[-1]:
                    deduped.append(p)
            return deduped

    # Try category1..category4 flat fields
    crumbs = []
    for i in range(1, 6):
        v = part.get(f'category{i}') or ''
        if v:
            crumbs.append(v.strip())
    if crumbs:
        return crumbs

    return []


def _dig(obj, *keys):
    """Safely navigate nested dicts/lists."""
    for k in keys:
        if obj is None:
            return None
        if isinstance(obj, dict):
            obj = obj.get(k)
        elif isinstance(obj, list) and isinstance(k, int):
            obj = obj[k] if k < len(obj) else None
        else:
            return None
    return obj


# ---------------------------------------------------------------------------
# HTML (BeautifulSoup) parsers — used when Next.js data is incomplete
# ---------------------------------------------------------------------------

def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def parse_product_header(soup: BeautifulSoup) -> str:
    """Plain-text product name (same as the page subtitle / title)."""
    return parse_title(soup)


def parse_part_number(soup: BeautifulSoup) -> str:
    # Common patterns on datasheets.com
    for sel in (
        "[class*='part-number']",
        "[class*='partNumber']",
        "[class*='mpn']",
        "h1",
    ):
        el = soup.select_one(sel)
        if el:
            text = el.get_text(strip=True)
            if text:
                return text
    return ""


def parse_manufacturer(soup: BeautifulSoup) -> str:
    for sel in (
        "[class*='manufacturer']",
        "[class*='brand']",
        "a[href*='/manufacturer/']",
        "a[href*='/manufacturers/']",
    ):
        el = soup.select_one(sel)
        if el:
            text = el.get_text(strip=True)
            if text and len(text) < 80:
                return text.strip()
    return ""


def parse_availability(soup: BeautifulSoup) -> dict:
    lifecycle = ""
    overall_stock = False

    # Lifecycle badge / chip
    for sel in (
        "[class*='lifecycle']",
        "[class*='status-badge']",
        "[class*='availability']",
    ):
        el = soup.select_one(sel)
        if el:
            text = el.get_text(strip=True)
            if text:
                lifecycle = text
                break

    # If any distributor row shows "In Stock" → overall available
    for el in soup.find_all(string=re.compile(r"in stock", re.I)):
        overall_stock = True
        break

    return {"lifecycleStatus": lifecycle, "overallStockAvailable": overall_stock}


def parse_title(soup: BeautifulSoup) -> str:
    """
    Extract the product title/subtitle — the short human-readable product name
    displayed near the part number in the page header (e.g. '0603, Thin Film, SMD, 1MOhm').
    Different from the long technicalSpecification.description.
    """
    # Try <meta> tags first (og:title or description)
    for meta in soup.find_all("meta"):
        prop = meta.get("property", "") or meta.get("name", "")
        if prop in ("og:title", "og:description", "description"):
            content = (meta.get("content") or "").strip()
            if content and len(content) > 5:
                return content

    # Try subtitle / tagline element near the part-number header
    for sel in (
        "[class*='subtitle']",
        "[class*='tagline']",
        "[class*='product-title']",
        "[class*='part-title']",
        "[class*='header'] p",
        "header p",
        "header span",
    ):
        el = soup.select_one(sel)
        if el:
            text = el.get_text(strip=True)
            if text and 5 < len(text) < 300:
                return text

    # Fall back to <title> tag (strip site suffix)
    title_tag = soup.find("title")
    if title_tag:
        text = title_tag.get_text(strip=True)
        # Remove " | datasheets.com" or " - DATASHEETS" suffix
        text = re.sub(r"\s*[\|\-]\s*(datasheets\.com|DATASHEETS).*$", "", text, flags=re.I).strip()
        if text:
            return text

    return ""


def parse_description(soup: BeautifulSoup) -> str:
    for sel in (
        "[class*='description']",
        "[class*='product-desc']",
        "[class*='overview']",
        "p.description",
    ):
        el = soup.select_one(sel)
        if el:
            text = el.get_text(strip=True)
            if text and len(text) > 10:
                return text
    return ""


def parse_general_specs(soup: BeautifulSoup) -> dict:
    """Parse the Technical Specifications / General table into a flat dict."""
    specs: dict = {}

    # Look for a section labelled "Technical Specifications" or "General"
    for section_el in soup.find_all(["section", "div", "table"]):
        heading = section_el.find(["h2", "h3", "h4", "th"])
        if not heading:
            continue
        heading_text = heading.get_text(strip=True).lower()
        if "technical" not in heading_text and "general" not in heading_text and "specification" not in heading_text:
            continue

        # Parse rows
        for row in section_el.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) >= 2:
                key = cells[0].get_text(strip=True)
                val = cells[1].get_text(strip=True)
                if key and val and key.lower() not in ("parameter", "value", "attribute"):
                    specs[key] = val

        if specs:
            break

    # Fallback: scan all tables for 2-column key-value patterns
    if not specs:
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 3:
                continue
            for row in rows:
                cells = row.find_all(["td", "th"])
                if len(cells) == 2:
                    k = cells[0].get_text(strip=True)
                    v = cells[1].get_text(strip=True)
                    if k and v and len(k) < 60:
                        specs[k] = v
            if len(specs) > 5:
                break

    return specs


def parse_compliance(soup: BeautifulSoup) -> dict:
    """Parse compliance section (RoHS, REACH, lead-free, approvals)."""
    compliance = {
        "rohs": None,
        "reach": None,
        "leadFree": None,
        "approvals": [],
        "flammabilityRating": None,
    }

    text = soup.get_text(" ")

    # RoHS
    m = re.search(r'RoHS\s*[:\-]?\s*(Compliant|Non-Compliant|Yes|No|Exempt)', text, re.I)
    if m:
        val = m.group(1).lower()
        compliance["rohs"] = val in ("compliant", "yes")

    # REACH
    m = re.search(r'REACH\s*[:\-]?\s*(Compliant|Non-Compliant|Yes|No)', text, re.I)
    if m:
        val = m.group(1).lower()
        compliance["reach"] = val in ("compliant", "yes")

    # Lead-Free
    m = re.search(r'Lead[\s-]?Free\s*[:\-]?\s*(Yes|No|Compliant|Non-Compliant)', text, re.I)
    if m:
        val = m.group(1).lower()
        compliance["leadFree"] = val in ("yes", "compliant")

    # Flammability
    m = re.search(r'(UL\s*94[\s\-]?[VHB][\-\s]?\w*)', text, re.I)
    if m:
        compliance["flammabilityRating"] = m.group(1).strip()

    # Approvals (UL, CSA, CE, etc.)
    approvals = set()
    for pat in (r'\bUL\b', r'\bCSA\b', r'\bCE\b', r'\bTUV\b', r'\bVDE\b', r'\bCCC\b'):
        m2 = re.search(pat, text)
        if m2:
            approvals.add(m2.group(0))
    compliance["approvals"] = sorted(approvals)

    # Also parse compliance table if present
    for section_el in soup.find_all(["section", "div"]):
        heading = section_el.find(["h2", "h3", "h4"])
        if not heading or "compliance" not in heading.get_text(strip=True).lower():
            continue
        for row in section_el.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) >= 2:
                k = cells[0].get_text(strip=True).lower()
                v = cells[1].get_text(strip=True)
                if "rohs" in k:
                    compliance["rohs"] = "compliant" in v.lower() or v.lower() == "yes"
                elif "reach" in k:
                    compliance["reach"] = "compliant" in v.lower() or v.lower() == "yes"
                elif "lead" in k:
                    compliance["leadFree"] = "lead free" in v.lower() or v.lower() == "yes"
        break

    return compliance


def parse_images(soup: BeautifulSoup, base_url: str = "https://www.datasheets.com") -> list:
    """Return de-duplicated list of absolute image URLs from Product Images section."""
    seen = set()
    urls = []

    # Prefer images inside a "Product Images" section
    for section_el in soup.find_all(["section", "div"]):
        heading = section_el.find(["h2", "h3", "h4"])
        if not heading or "image" not in heading.get_text(strip=True).lower():
            continue
        for img in section_el.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            src = src.strip()
            if not src or src.startswith("data:"):
                continue
            if not src.startswith("http"):
                src = base_url.rstrip("/") + "/" + src.lstrip("/")
            if src not in seen:
                seen.add(src)
                urls.append(src)

    # Fallback: all product images on page
    if not urls:
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            src = src.strip()
            if not src or src.startswith("data:"):
                continue
            # Skip icons / logos / tiny images
            alt = (img.get("alt") or "").lower()
            if any(x in alt for x in ("logo", "icon", "banner")):
                continue
            if not src.startswith("http"):
                src = base_url.rstrip("/") + "/" + src.lstrip("/")
            if src not in seen:
                seen.add(src)
                urls.append(src)

    return urls


def parse_applications(soup: BeautifulSoup) -> list:
    """Return list of application use-cases from the page."""
    apps = []
    for section_el in soup.find_all(["section", "div", "ul"]):
        heading = section_el.find(["h2", "h3", "h4"])
        if not heading or "application" not in heading.get_text(strip=True).lower():
            continue
        for li in section_el.find_all("li"):
            text = li.get_text(strip=True)
            if text:
                apps.append(text)
        if not apps:
            text = section_el.get_text(" ", strip=True)
            # Split on comma/semicolon
            for part in re.split(r"[,;]", text):
                p = part.strip()
                if p and len(p) < 100:
                    apps.append(p)
    return apps


_CURRENCY_MARKERS = (
    ("Rs.", "INR"),
    ("Rs", "INR"),
    ("INR", "INR"),
    ("₹", "INR"),
    ("S$", "SGD"),
    ("C$", "CAD"),
    ("A$", "AUD"),
    ("HK$", "HKD"),
    ("NZ$", "NZD"),
    ("US$", "USD"),
    ("£", "GBP"),
    ("€", "EUR"),
    ("¥", "JPY"),
    ("$", "USD"),
)


def _parse_money(text: str) -> tuple[float | None, str | None]:
    """Parse '$0.374', '£0.244', 'S$0.497', '—' → (amount, currency)."""
    if not text:
        return None, None
    raw = text.strip().replace("\xa0", " ").replace(",", "")
    if raw in ("", "—", "–", "-", "n/a", "N/A"):
        return None, None
    currency = None
    for marker, code in _CURRENCY_MARKERS:
        if marker in raw:
            currency = code
            raw = raw.replace(marker, "")
            break
    m = re.search(r"(\d+(?:\.\d+)?)", raw)
    if not m:
        return None, None
    try:
        amount = float(m.group(1))
    except ValueError:
        return None, None
    if amount <= 0:
        return None, currency
    return amount, currency


def _header_tier_qty(header: str) -> int | None:
    m = re.search(r"(\d[\d,]*)\s*\+", header.replace(",", ""))
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _clean_price_breaks(breaks: list) -> list:
    """Drop dummy API rows (qty 0 / $0.00) and keep one price per quantity."""
    by_qty: dict[int, dict] = {}
    for pb in breaks or []:
        if not isinstance(pb, dict):
            continue
        try:
            qty = int(pb.get("quantity"))
            price = float(pb.get("unitPrice"))
        except (TypeError, ValueError):
            continue
        if qty < 1 or price <= 0:
            continue
        cur = pb.get("currency") or "USD"
        prev = by_qty.get(qty)
        if prev is None:
            by_qty[qty] = {"quantity": qty, "unitPrice": price, "currency": cur}
    return [by_qty[q] for q in sorted(by_qty)]


def parse_pricing(soup: BeautifulSoup) -> list:
    """
    Parse the Pricing & Availability table.

    datasheets.com uses quantity-tier columns (e.g. '100 +', '250 +', '3000 +')
    with currency in the cell ($ / £ / S$), not a single Price column.
    """
    pricing = []
    seen_tables = 0

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        header_cells = rows[0].find_all(["th", "td"])
        headers = [th.get_text(" ", strip=True) for th in header_cells]
        headers_l = [h.lower() for h in headers]
        if not any("distributor" in h or "stock" in h for h in headers_l):
            continue
        if not any(_header_tier_qty(h) for h in headers) and not any(
            "price" in h for h in headers_l
        ):
            continue
        seen_tables += 1

        def cell_text(row, idx):
            cells = row.find_all(["td", "th"])
            if idx < 0 or idx >= len(cells):
                return ""
            return cells[idx].get_text(" ", strip=True)

        def col_idx(*frags):
            for i, h in enumerate(headers_l):
                if any(f in h for f in frags):
                    return i
            return -1

        dist_i = col_idx("distributor", "supplier")
        stock_i = col_idx("stock")
        moq_i = col_idx("min")
        pkg_i = col_idx("package", "pkg")
        action_i = col_idx("action", "buy")
        tier_cols = [(i, _header_tier_qty(h)) for i, h in enumerate(headers)]
        tier_cols = [(i, q) for i, q in tier_cols if q]

        for row in rows[1:]:
            cells = row.find_all("td")
            if not cells:
                continue

            distributor_name = cell_text(row, dist_i) if dist_i >= 0 else ""
            stock_text = cell_text(row, stock_i) if stock_i >= 0 else ""
            moq_text = cell_text(row, moq_i) if moq_i >= 0 else ""
            pkg_text = cell_text(row, pkg_i) if pkg_i >= 0 else ""

            dist_url = ""
            link_i = dist_i if dist_i >= 0 else 0
            for idx in (link_i, action_i, 0):
                if idx < 0:
                    continue
                cells_row = row.find_all(["td", "th"])
                if idx >= len(cells_row):
                    continue
                a = cells_row[idx].find("a", href=True)
                if a and a.get("href") and not a["href"].startswith("#"):
                    dist_url = a["href"]
                    if not dist_url.startswith("http"):
                        dist_url = "https://www.datasheets.com" + dist_url
                    break

            stock_qty = None
            m = re.search(r"([\d,]+)", stock_text.replace(",", ""))
            if m and "—" not in stock_text:
                try:
                    stock_qty = int(m.group(1).replace(",", ""))
                except ValueError:
                    stock_qty = None

            sl = stock_text.lower()
            if "in stock" in sl or (stock_qty is not None and stock_qty > 0):
                stock_status = "In Stock"
            elif "limited" in sl:
                stock_status = "Limited Stock"
            elif "back" in sl:
                stock_status = "Backorder"
            else:
                stock_status = "Out of Stock"

            moq = 1
            m = re.search(r"([\d,]+)", moq_text.replace(",", ""))
            if m:
                try:
                    moq = int(m.group(1).replace(",", ""))
                except ValueError:
                    moq = 1

            breaks = []
            for idx, qty in tier_cols:
                amount, currency = _parse_money(cell_text(row, idx))
                if amount is None:
                    continue
                breaks.append({
                    "quantity": qty,
                    "unitPrice": amount,
                    "currency": currency or "USD",
                })

            # Legacy single Price column
            if not breaks:
                price_i = col_idx("price")
                if price_i >= 0:
                    amount, currency = _parse_money(cell_text(row, price_i))
                    if amount is not None:
                        breaks.append({
                            "quantity": moq,
                            "unitPrice": amount,
                            "currency": currency or "USD",
                        })

            if not distributor_name and not breaks and stock_qty is None:
                continue

            pricing.append({
                "distributorName": distributor_name,
                "distributorPartUrl": dist_url,
                "stock": {"quantity": stock_qty, "status": stock_status},
                "minimumOrderQuantity": moq,
                "packageType": pkg_text if pkg_text and pkg_text not in ("—", "-", "–") else None,
                "priceBreaks": _clean_price_breaks(breaks),
            })

        if seen_tables:
            break

    return pricing


def parse_datasheet(soup: BeautifulSoup, product_url: str) -> dict:
    """Parse datasheet section for PDF URL and availability."""
    pdf_url = None
    is_available = False

    def _abs(href: str) -> str:
        if not href.startswith("http"):
            return "https://www.datasheets.com" + href
        return href

    def _valid(href: str) -> bool:
        """Reject fragment-only or empty hrefs."""
        return bool(href) and not href.startswith("#") and "?" not in href.split("#")[0].replace(".pdf", "")

    # Priority 1: explicit .pdf link
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.endswith(".pdf") and _valid(href):
            pdf_url = _abs(href)
            is_available = True
            break

    # Priority 2: "Download PDF" or "Open in New Tab" button text
    if not pdf_url:
        for a in soup.find_all("a"):
            text = a.get_text(strip=True).lower()
            href = a.get("href", "").strip()
            if ("download" in text or "open in new tab" in text) and href.endswith(".pdf") and _valid(href):
                pdf_url = _abs(href)
                is_available = True
                break

    # Per spec: pdfUrl must be null if not found on page — do NOT guess.

    return {
        "isDatasheetAvailable": is_available,
        "hasStoredDatasheet": False,
        "pdfUrl": pdf_url,
        "pdfToDocumentMappingId": None,
        "storage": {
            "documentId": None,
            "blobId": None,
            "blobUrl": None,
            "version": 1,
        },
    }


def parse_category_breadcrumb(soup: BeautifulSoup) -> list[str]:
    """Extract ordered category breadcrumb from page."""
    # 1. Structured data ld+json — most reliable
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            # Handle Product with nested breadcrumb
            if isinstance(data, dict) and data.get("@type") == "Product":
                bc = data.get("breadcrumb") or {}
                items = bc.get("itemListElement", []) if isinstance(bc, dict) else []
                crumbs = [i.get("name", "") for i in items if i.get("name")]
                if crumbs:
                    return crumbs
            # Handle standalone BreadcrumbList
            if isinstance(data, dict) and data.get("@type") == "BreadcrumbList":
                items = data.get("itemListElement", [])
                crumbs = [i.get("name", "") for i in items if i.get("name")]
                if crumbs:
                    return crumbs
        except Exception:
            pass

    # 2. Nav breadcrumb — deduplicate consecutive same-text entries
    skip = {"›", "/", ">", "»", "Home", "Categories"}
    for nav in soup.find_all(["nav", "ol", "ul"]):
        aria = (nav.get("aria-label") or "").lower()
        cls = " ".join(nav.get("class") or []).lower()
        if "breadcrumb" not in aria and "breadcrumb" not in cls:
            continue
        seen_texts: set = set()
        crumbs = []
        for item in nav.find_all(["li", "a", "span"]):
            text = item.get_text(strip=True)
            if text and text not in skip and text not in seen_texts:
                seen_texts.add(text)
                crumbs.append(text)
        if crumbs:
            return crumbs

    return []


# ---------------------------------------------------------------------------
# Next.js data path — map page props to our schema
# ---------------------------------------------------------------------------

def build_from_next_data(nd: dict, product_url: str, fallback_html: str) -> dict | None:
    """
    Try to extract a full product record from __NEXT_DATA__.
    Returns None if the data isn't structured enough.
    """
    # Common Next.js page prop paths
    props = _dig(nd, "props", "pageProps")
    if not props:
        return None

    # Try to find product data — field names vary by site version
    product = (
        props.get("product")
        or props.get("part")
        or props.get("partData")
        or props.get("componentData")
        or props.get("data")
    )
    if not product or not isinstance(product, dict):
        return None

    part_number = (
        product.get("partNumber")
        or product.get("mpn")
        or product.get("part_number")
        or ""
    )
    if not part_number:
        return None  # not enough data

    manufacturer = (
        product.get("manufacturer")
        or _dig(product, "manufacturerInfo", "name")
        or _dig(product, "brand", "name")
        or ""
    )
    if isinstance(manufacturer, dict):
        manufacturer = manufacturer.get("name", "")

    # title = short product name displayed near part number in page header
    title = product.get("title") or product.get("name") or product.get("shortDescription") or ""
    category = product.get("category") or product.get("breadcrumb") or []
    if isinstance(category, str):
        category = [c.strip() for c in category.split(">")]

    availability = product.get("availability") or {}
    lifecycle = availability.get("lifecycleStatus") or product.get("lifecycleStatus") or ""
    in_stock = availability.get("overallStockAvailable") or product.get("inStock") or False

    specs = product.get("technicalSpecification") or product.get("specs") or product.get("specifications") or {}
    description = ""
    general = {}
    compliance = {"rohs": None, "reach": None, "leadFree": None, "approvals": [], "flammabilityRating": None}
    images = []
    applications = []

    if isinstance(specs, dict):
        description = specs.get("description") or title
        general = specs.get("general") or specs.get("attributes") or {}
        compliance = specs.get("compliance") or compliance
        images = specs.get("images") or []
        applications = specs.get("applications") or []
    elif isinstance(specs, list):
        general = {item["name"]: item["value"] for item in specs if isinstance(item, dict) and item.get("name")}

    pricing_raw = product.get("pricing") or product.get("prices") or product.get("distributors") or []
    pricing = []
    for p in pricing_raw:
        if not isinstance(p, dict):
            continue
        pricing.append({
            "distributorName": p.get("distributorName") or p.get("name") or "",
            "distributorPartUrl": p.get("distributorPartUrl") or p.get("url") or "",
            "stock": {
                "quantity": p.get("stock", {}).get("quantity") or p.get("quantity") or 0,
                "status": p.get("stock", {}).get("status") or p.get("stockStatus") or "Out of Stock",
            },
            "minimumOrderQuantity": p.get("minimumOrderQuantity") or p.get("moq") or 1,
            "packageType": p.get("packageType") or p.get("package") or None,
            "priceBreaks": p.get("priceBreaks") or [],
        })

    datasheet_raw = product.get("datasheet") or {}
    pdf_url = datasheet_raw.get("pdfUrl") or product.get("pdfUrl") or product.get("datasheetUrl")
    is_ds_available = bool(pdf_url) or datasheet_raw.get("isDatasheetAvailable", False)

    soup = _soup(fallback_html)
    if not pdf_url:
        ds = parse_datasheet(soup, product_url)
        pdf_url = ds["pdfUrl"]
        is_ds_available = is_ds_available or ds["isDatasheetAvailable"]
    if not images:
        images = parse_images(soup)
    if not applications:
        applications = parse_applications(soup)
    if not description:
        description = parse_description(soup)
    if not title:
        title = parse_title(soup)

    return _assemble(
        part_number=part_number,
        product_url=product_url,
        product_header=title,
        manufacturer=manufacturer,
        manufacturer_metadata={},
        category=category,
        lifecycle=lifecycle,
        in_stock=in_stock,
        description=description,
        general=general,
        compliance=compliance,
        images=images,
        applications=applications,
        pricing=pricing,
        pdf_url=pdf_url,
        is_ds_available=is_ds_available,
    )


# ---------------------------------------------------------------------------
# RSC path — primary for datasheets.com App Router
# ---------------------------------------------------------------------------

def build_from_rsc(part: dict, html: str, product_url: str) -> dict:
    """Build record from RSC flight data ('part' object extracted from Next.js RSC payload)."""
    soup = _soup(html)

    part_number = part.get('mpn') or part.get('partNumber') or ''

    mfr = part.get('manufacturer') or {}
    if mfr:
        raw_mfr = mfr.get('name') if isinstance(mfr, dict) else str(mfr)
        manufacturer = (raw_mfr or '').strip()
    else:
        manufacturer = ''

    # manufacturerMetadata — all fields from RSC manufacturer object
    manufacturer_metadata: dict = {}
    if isinstance(mfr, dict):
        manufacturer_metadata = {k: v for k, v in mfr.items() if v and v != '$undefined'}

    title = part.get('title') or part.get('name') or ''

    # Use richer descriptions[0].text if available, fall back to description field
    descriptions = part.get('descriptions') or []
    description = (descriptions[0].get('text', '') if descriptions and isinstance(descriptions[0], dict) else '') \
                  or part.get('description') or ''
    lifecycle = part.get('lifecycleStatus') or ''
    in_stock = bool(part.get('inStock'))

    # Technical specs
    specs_list = part.get('specs') or part.get('specifications') or []
    if isinstance(specs_list, list):
        general, compliance = parse_rsc_specs(specs_list)
    else:
        general = {}
        compliance = {"rohs": None, "reach": None, "leadFree": None, "approvals": [], "flammabilityRating": None}

    # Images — RSC array then primaryImage fallback
    images = parse_rsc_images(part.get('images') or [])
    if not images:
        primary = part.get('primaryImage') or ''
        if primary:
            images = [primary]

    # Category breadcrumb — RSC category.path then HTML nav
    category = parse_rsc_breadcrumb(part)
    if not category:
        category = parse_category_breadcrumb(soup)

    # Datasheet — bestDatasheet.url from RSC then HTML scan
    best_ds = part.get('bestDatasheet') or {}
    pdf_url = (best_ds.get('url') if isinstance(best_ds, dict) else None) or None
    is_ds_available = bool(pdf_url)
    if not pdf_url:
        ds = parse_datasheet(soup, product_url)
        pdf_url = ds['pdfUrl']
        is_ds_available = ds['isDatasheetAvailable']

    # Pricing is AJAX-loaded; HTML table is a fallback until the API runs.
    pricing = parse_pricing(soup)

    applications = parse_applications(soup)

    if not title:
        title = parse_title(soup)
    if not description:
        description = parse_description(soup)

    return _assemble(
        part_number=part_number,
        product_url=product_url,
        product_header=title,
        manufacturer=manufacturer,
        manufacturer_metadata=manufacturer_metadata,
        category=category,
        lifecycle=lifecycle,
        in_stock=in_stock,
        description=description,
        general=general,
        compliance=compliance,
        images=images,
        applications=applications,
        pricing=pricing,
        pdf_url=pdf_url,
        is_ds_available=is_ds_available,
    )


# ---------------------------------------------------------------------------
# HTML-only path
# ---------------------------------------------------------------------------

def build_from_html(html: str, product_url: str) -> dict:
    soup = _soup(html)

    part_number = parse_part_number(soup)
    manufacturer = parse_manufacturer(soup)
    availability = parse_availability(soup)
    title = parse_title(soup)
    description = parse_description(soup)
    general = parse_general_specs(soup)
    compliance = parse_compliance(soup)
    images = parse_images(soup)
    applications = parse_applications(soup)
    pricing = parse_pricing(soup)
    datasheet = parse_datasheet(soup, product_url)
    category = parse_category_breadcrumb(soup)

    return _assemble(
        part_number=part_number,
        product_url=product_url,
        product_header=title,
        manufacturer=manufacturer,
        manufacturer_metadata={},
        category=category,
        lifecycle=availability["lifecycleStatus"],
        in_stock=availability["overallStockAvailable"],
        description=description,
        general=general,
        compliance=compliance,
        images=images,
        applications=applications,
        pricing=pricing,
        pdf_url=datasheet["pdfUrl"],
        is_ds_available=datasheet["isDatasheetAvailable"],
    )


# ---------------------------------------------------------------------------
# Schema assembler
# ---------------------------------------------------------------------------

def _assemble(
    *,
    part_number,
    product_url,
    product_header,
    manufacturer,
    manufacturer_metadata,
    category,
    lifecycle,
    in_stock,
    description,
    general,
    compliance,
    images,
    applications,
    pricing,
    pdf_url,
    is_ds_available,
) -> dict:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # Deduplicate images preserving order
    seen: set = set()
    deduped_images = []
    for img in images:
        if img and img not in seen:
            seen.add(img)
            deduped_images.append(img)

    # Schema matches codexscape-final.pdf exactly (field names, nesting, order).
    return {
        "partNumber": part_number,
        "productUrl": product_url,
        "productHeader": product_header or "",
        "manufacturer": manufacturer,
        "category": category if isinstance(category, list) else [category],
        "availability": {
            "lifecycleStatus": lifecycle,
            "overallStockAvailable": in_stock,
        },
        "technicalSpecification": {
            "description": description,           # raw — no rewriting
            "general": general if isinstance(general, dict) else {},
            "compliance": compliance if isinstance(compliance, dict) else {
                "rohs": None,
                "reach": None,
                "leadFree": None,
                "approvals": [],
                "flammabilityRating": None,
            },
            "images": deduped_images,             # deduped absolute URLs
            "applications": applications if isinstance(applications, list) else [],
            "pdfMetadata": {},                    # populated by PDF processor
            "countryOfOrigin": None,
        },
        "pricing": pricing if isinstance(pricing, list) else [],
        "leadTime": None,
        "isObsolete": None,
        "obsoleteAt": None,
        "audit": {
            "fetchedAt": now,
            "source": "datasheets.com",
            "lastUpdatedAt": None,
        },
        "datasheet": {
            "isDatasheetAvailable": is_ds_available,
            "hasStoredDatasheet": False,
            "pdfUrl": pdf_url,                    # null if not found on page
            "pdfToDocumentMappingId": None,       # set by downstream PDF processor
            "storage": {
                "documentId": None,
                "blobId": None,
                "blobUrl": None,
                "version": None,
            },
        },
    }


# ---------------------------------------------------------------------------
# Pricing API response normalizer
# ---------------------------------------------------------------------------

def _abs_datasheets_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url.startswith("/"):
        return "https://www.datasheets.com" + url
    return url


def _normalize_price_breaks(offer: dict, moq: int, currency: str) -> list:
    """API offers nest breaks as pricing: [{break, price}], not priceBreaks."""
    out = []
    raw = (
        offer.get("pricing")
        if isinstance(offer.get("pricing"), list)
        else None
    ) or offer.get("priceBreaks") or offer.get("prices") or []
    if isinstance(raw, dict):
        raw = [raw]
    for pb in raw:
        if not isinstance(pb, dict):
            continue
        qty = pb.get("break") or pb.get("quantity") or pb.get("qty") or moq
        price = pb.get("price") or pb.get("unitPrice")
        if price is None:
            continue
        try:
            qty_i = int(str(qty).replace(",", "").split()[0])
            price_f = float(str(price).replace(",", "").replace("$", "").strip())
        except (ValueError, TypeError):
            continue
        if qty_i < 1 or price_f <= 0:
            continue
        out.append({
            "quantity": qty_i,
            "unitPrice": price_f,
            "currency": pb.get("currency") or currency or "USD",
        })
    return _clean_price_breaks(out)


def _normalize_api_pricing(pricing_list: list) -> list:
    """
    Normalize /api/part-pricing offers into the spec pricing array.
    Live API shape (per offer): source, tracked_url, inventory, moq, packaging,
    currency, pricing: [{break, price}].
    """
    out = []
    for p in pricing_list:
        if not isinstance(p, dict):
            continue

        dist_name = (
            p.get("source")
            or p.get("distributorName")
            or p.get("name")
            or p.get("distributor")
            or ""
        )
        if not dist_name:
            continue
        dist_name = str(dist_name).replace("_", " ").strip()
        if dist_name.islower() or dist_name.isupper():
            dist_name = dist_name.title()

        stock_qty = p.get("inventory")
        if stock_qty is None:
            stock_qty = p.get("stock") or p.get("quantity") or p.get("stockQuantity")
        if isinstance(stock_qty, dict):
            stock_qty = stock_qty.get("quantity")
        try:
            stock_qty = int(stock_qty) if stock_qty is not None else None
        except (ValueError, TypeError):
            stock_qty = None

        stock_status_raw = p.get("stockStatus") or p.get("availability") or ""
        if isinstance(stock_status_raw, dict):
            stock_status_raw = stock_status_raw.get("status") or ""
        sl = str(stock_status_raw).lower()
        if "in stock" in sl or (stock_qty is not None and stock_qty > 0):
            stock_status = "In Stock"
        elif "limited" in sl:
            stock_status = "Limited Stock"
        elif "back" in sl:
            stock_status = "Backorder"
        elif stock_qty == 0:
            stock_status = "Out of Stock"
        else:
            stock_status = "Out of Stock"

        moq = p.get("moq") or p.get("minimumOrderQuantity") or 1
        try:
            moq = int(str(moq).replace(",", "").split()[0])
        except (ValueError, TypeError):
            moq = 1

        pkg = p.get("packaging") or p.get("packageType") or p.get("package") or None
        if pkg == "—":
            pkg = None
        currency = p.get("currency") or "USD"
        dist_url = _abs_datasheets_url(
            p.get("tracked_url") or p.get("distributorPartUrl") or p.get("buy_url") or p.get("url") or ""
        )

        out.append({
            "distributorName": dist_name,
            "distributorPartUrl": dist_url,
            "stock": {"quantity": stock_qty, "status": stock_status},
            "minimumOrderQuantity": moq,
            "packageType": pkg,
            "priceBreaks": _normalize_price_breaks(p, moq, currency),
        })
    return out


def _dist_key(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _merge_pricing(api_offers: list, html_offers: list) -> list:
    """
    API has extra qty tiers the on-page table hides; HTML has currency symbols
    ($ / £ / S$) and the visible 100+/250+/500+/3000+ cells. Merge both:
    keep every real API break, overlay HTML prices/currency for the same qty,
    add HTML-only cells, drop $0 dummy rows.
    """
    merged: dict[str, dict] = {}
    unnamed_html = []

    def upsert(offer: dict, prefer_html_prices: bool):
        name = offer.get("distributorName") or ""
        key = _dist_key(name)
        if not key:
            unnamed_html.append(offer)
            return
        cur = merged.get(key)
        if cur is None:
            cloned = dict(offer)
            cloned["priceBreaks"] = _clean_price_breaks(offer.get("priceBreaks") or [])
            merged[key] = cloned
            return
        if prefer_html_prices:
            if offer.get("distributorPartUrl") and not cur.get("distributorPartUrl"):
                cur["distributorPartUrl"] = offer["distributorPartUrl"]
            if offer.get("packageType") and not cur.get("packageType"):
                cur["packageType"] = offer["packageType"]
        by_qty = {pb["quantity"]: pb for pb in cur.get("priceBreaks") or []}
        for pb in _clean_price_breaks(offer.get("priceBreaks") or []):
            existing = by_qty.get(pb["quantity"])
            if existing is None:
                by_qty[pb["quantity"]] = pb
            elif prefer_html_prices:
                # HTML cell currency/amount is what the PDP table shows.
                by_qty[pb["quantity"]] = pb
        cur["priceBreaks"] = [by_qty[q] for q in sorted(by_qty)]

    for offer in api_offers or []:
        upsert(offer, prefer_html_prices=False)
    for offer in html_offers or []:
        upsert(offer, prefer_html_prices=True)

    # First table row often has a blank distributor name (logo-only).
    for html_row in unnamed_html:
        html_breaks = _clean_price_breaks(html_row.get("priceBreaks") or [])
        html_prices = {round(pb["unitPrice"], 5) for pb in html_breaks}
        html_stock = (html_row.get("stock") or {}).get("quantity")
        html_moq = html_row.get("minimumOrderQuantity")
        match_key = None
        best = (-1, -1, None)  # overlap, stock_match, key
        for key, offer in merged.items():
            api_prices = {round(pb["unitPrice"], 5) for pb in offer.get("priceBreaks") or []}
            stock = (offer.get("stock") or {}).get("quantity")
            overlap = len(html_prices & api_prices) if html_prices else 0
            stock_match = int(
                html_stock is not None
                and stock == html_stock
                and html_moq == offer.get("minimumOrderQuantity")
            )
            cand = (overlap, stock_match, key)
            if cand[:2] > best[:2]:
                best = cand
        if best[0] >= 1:
            match_key = best[2]
        elif best[0] == 0 and best[1] == 1:
            # Stock+MOQ only if a single distributor matches.
            stock_hits = [
                k for k, offer in merged.items()
                if (offer.get("stock") or {}).get("quantity") == html_stock
                and offer.get("minimumOrderQuantity") == html_moq
            ]
            if len(stock_hits) == 1:
                match_key = stock_hits[0]
        if match_key:
            upsert({**html_row, "distributorName": merged[match_key]["distributorName"]}, True)
        elif html_breaks or html_stock:
            merged[f"__unnamed_{len(merged)}"] = {
                **html_row,
                "distributorName": html_row.get("distributorName") or "",
                "priceBreaks": html_breaks,
            }

    return list(merged.values())


# ---------------------------------------------------------------------------
# Per-URL worker
# ---------------------------------------------------------------------------

async def process_url(product_url: str, phase3_record: dict) -> dict | None:
    global SEMAPHORE
    async with SEMAPHORE:
        html = await fetch_html(product_url)
        await asyncio.sleep(config.PHASE3_DELAY_MS / 1000)

    if not html:
        return None

    # Try RSC flight data first (datasheets.com uses Next.js App Router)
    record = None
    part = extract_rsc_part_data(html)
    if part:
        try:
            record = build_from_rsc(part, html, product_url)
        except Exception:
            record = None

    # Fall back to legacy __NEXT_DATA__ (classic Next.js pages)
    if not record:
        nd = extract_next_data(html)
        if nd:
            try:
                record = build_from_next_data(nd, product_url, html)
            except Exception:
                record = None

    # Fall back to pure HTML parsing
    if not record:
        try:
            record = build_from_html(html, product_url)
        except Exception:
            return None

    # Pricing: merge live API tiers with the on-page qty-column table ($/£/S$).
    if record:
        html_pricing = list(record.get("pricing") or [])
        if html and not html_pricing:
            try:
                html_pricing = parse_pricing(_soup(html))
            except Exception:
                html_pricing = []
        api_pricing = []
        mpn = record.get("partNumber", "") or phase3_record.get("partNumber", "")
        mfr = record.get("manufacturer", "") or phase3_record.get("manufacturer", "")
        if mpn and mfr:
            try:
                qs = urlencode({"pn": mpn, "manufacturer": mfr})
                pricing_resp = await asyncio.to_thread(
                    http_get,
                    f"https://www.datasheets.com/api/part-pricing?{qs}",
                    15.0,
                )
                if pricing_resp is not None and pricing_resp.status_code == 200:
                    payload = pricing_resp.json()
                    pricing_data = payload.get("pricing") if isinstance(payload, dict) else None
                    if isinstance(pricing_data, list) and pricing_data:
                        api_pricing = _normalize_api_pricing(pricing_data)
            except Exception:
                pass
        # Keep prices in the distributors' native currencies (USD/GBP/SGD),
        # exactly as the site's pricing API returns them. No FX conversion.
        record["pricing"] = _merge_pricing(api_pricing, html_pricing)

    # Back-fill fields from Phase 3 when HTML parse couldn't find them
    if not record.get("partNumber") and phase3_record.get("partNumber"):
        record["partNumber"] = phase3_record["partNumber"]
    if not record.get("manufacturer") and phase3_record.get("manufacturer"):
        record["manufacturer"] = phase3_record["manufacturer"]
    if not record.get("category") and phase3_record.get("category"):
        record["category"] = phase3_record["category"]
    if not record.get("productHeader") and phase3_record.get("title"):
        record["productHeader"] = phase3_record["title"]
    if not record["technicalSpecification"].get("description") and phase3_record.get("title"):
        record["technicalSpecification"]["description"] = phase3_record["title"]

    # PDF processing — download, extract metadata into technicalSpecification.pdfMetadata
    pdf_url = record.get("datasheet", {}).get("pdfUrl")
    if pdf_url and not record["datasheet"].get("hasStoredDatasheet"):
        pdf_bytes = await fetch_pdf_bytes(pdf_url)
        if pdf_bytes:
            _, pdf_meta = process_pdf(pdf_bytes)
            record["technicalSpecification"]["pdfMetadata"] = pdf_meta
            record["datasheet"]["hasStoredDatasheet"] = True

    return record


# ---------------------------------------------------------------------------
# Phase 4 runner
# ---------------------------------------------------------------------------

async def run_phase4(category: str, concurrency: int | None = None, limit: int | None = None):
    global SEMAPHORE
    concurrency = concurrency or config.PHASE3_CONCURRENCY
    SEMAPHORE = asyncio.Semaphore(concurrency)

    input_file = f"data/output/{category}_products.json"
    output_file = f"data/output/{category}_final.jsonl"
    ensure_dirs("data/output")

    if not os.path.exists(input_file):
        print(f"[phase4] Input not found: {input_file}  (run phase3 first)")
        return

    with open(input_file) as f:
        phase3_records: list[dict] = json.load(f)

    if limit:
        phase3_records = phase3_records[:limit]

    # Skip already-written URLs
    done_urls: set[str] = set()
    if os.path.exists(output_file):
        with open(output_file) as f:
            for line in f:
                try:
                    obj = json.loads(line)
                    if obj.get("productUrl"):
                        done_urls.add(obj["productUrl"])
                except Exception:
                    pass

    todo = [r for r in phase3_records if r.get("productUrl") not in done_urls]
    print(f"[phase4] [{category}] {len(phase3_records)} products | {len(todo)} to fetch | {len(done_urls)} already done")

    if not todo:
        print(f"[phase4] [{category}] Nothing to do.")
        return

    out_fh = open(output_file, "a", encoding="utf-8")

    done = 0
    errors = 0
    batch_size = concurrency * 4

    try:
        for i in range(0, len(todo), batch_size):
            batch = todo[i : i + batch_size]

            async def _worker(rec):
                url = rec.get("productUrl", "")
                if not url:
                    return None
                return await process_url(url, rec)

            results = await asyncio.gather(*[_worker(r) for r in batch])

            for record in results:
                if record:
                    out_fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                    done += 1
                else:
                    errors += 1

            out_fh.flush()
            nb = (len(todo) - 1) // batch_size + 1
            print(
                f"[phase4] [{category}] Batch {i // batch_size + 1}/{nb} "
                f"— done:{done} err:{errors} ({i + len(batch)}/{len(todo)})"
            )
            await asyncio.sleep(0.3)
    finally:
        out_fh.close()

    print(f"[phase4] [{category}] Complete — {done} records written to {output_file} ({errors} errors)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 4: Scrape product detail pages")
    parser.add_argument("-c", "--category", required=True, help="Category slug (must match phase3 output)")
    parser.add_argument("--concurrency", type=int, default=None, help="Concurrent requests (default: PHASE3_CONCURRENCY)")
    parser.add_argument("--limit", type=int, default=None, help="Max products to process (for testing)")
    args = parser.parse_args()
    asyncio.run(run_phase4(args.category.lower().strip(), args.concurrency, args.limit))
