"""
Quick test: render one listing page with Playwright and extract product data.
"""
import asyncio
import json
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

URL = "https://www.datasheets.com/category/test-measurement"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        print(f"Navigating to {URL}...")
        await page.goto(URL, wait_until="networkidle", timeout=30000)

        # Wait for product rows to appear
        try:
            await page.wait_for_selector("table tbody tr", timeout=15000)
            print("Products table found!")
        except Exception as e:
            print(f"Table not found: {e}")

        # Get the rendered HTML
        html = await page.content()
        print(f"HTML length: {len(html):,} chars")

        soup = BeautifulSoup(html, "lxml")

        # Try different selectors
        print("\n--- Trying selectors ---")

        # Table rows
        rows = soup.select("table tbody tr")
        print(f"Table rows: {len(rows)}")

        # Product links
        prod_links = soup.select("a[href*='/datasheet/']")
        print(f"Product datasheet links: {len(prod_links)}")

        # Any part numbers
        parts = soup.select(".part-number, [class*='part'], [class*='mpn']")
        print(f"Part number elements: {len(parts)}")

        # Look for text patterns
        text = soup.get_text()
        import re
        # Look for MPN patterns
        mpn_matches = re.findall(r'/([a-z0-9\-]+/[a-z0-9\-]+-[a-z0-9\-]+)', text)
        if mpn_matches:
            print(f"URL-like patterns found: {len(set(mpn_matches))} unique")

        # Check for any structured data
        scripts = soup.find_all("script", type="application/json")
        print(f"JSON scripts: {len(scripts)}")

        await browser.close()

        # Save HTML for inspection
        Path("data/test_rendered.html").write_text(html)
        print("\nSaved to data/test_rendered.html")


if __name__ == "__main__":
    asyncio.run(main())
