#!/usr/bin/env python3
"""
Extract primary and alt barcodes from the Minfos catalogue for a list of MNPNs.
Outputs results to a timestamped CSV file.

Usage:
    python extract_barcodes.py

Credentials are read from a .env file (MINFOS_USERNAME, MINFOS_PASSWORD)
or prompted interactively at runtime.
"""

import asyncio
import csv
import json
import os
import re
import sys
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ── MNPNs to extract ──────────────────────────────────────────────────────────

MNPNS = [
    "887814", "731282", "893785", "880650", "880647", "776974",
    "852558", "893784", "708741", "893791", "831357", "896753",
    "816906", "881479", "878526", "892601", "795804", "632754",
    "632601", "632762", "632775", "878147", "887487", "894873",
    "844139", "895175", "878528", "887489", "875588",
]

BASE_URL = "https://catalogue.minfos.com.au"

# ── Helpers ───────────────────────────────────────────────────────────────────

async def idle(page, timeout=12000):
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout)
    except PlaywrightTimeout:
        pass


def digits_only(text: str) -> str:
    """Return only the numeric portion of a string."""
    return re.sub(r"\D", "", text or "")


def looks_like_barcode(value: str) -> bool:
    """Return True if value looks like a real barcode (8–14 digits)."""
    v = digits_only(value)
    return 8 <= len(v) <= 14

# ── Login ─────────────────────────────────────────────────────────────────────

async def login(page, access_code: str) -> bool:
    print(f"  Navigating to {BASE_URL} …")
    await page.goto(BASE_URL, wait_until="domcontentloaded")
    await idle(page)

    # Locate the access code field
    for sel in [
        'input[placeholder*="access code" i]',
        'input[placeholder*="accesscode" i]',
        'input[placeholder*="code" i]',
        'input[name*="code" i]',
        'input[id*="code" i]',
        'input[formcontrolname*="code" i]',
        'input[type="text"]',
        'input[type="number"]',
        'input[type="password"]',
    ]:
        loc = page.locator(sel).first
        if await loc.count():
            await loc.fill(access_code)
            break
    else:
        print("  ERROR: Could not find access code field on login page.")
        return False

    # Submit
    for sel in [
        'button[type="submit"]', 'input[type="submit"]',
        'button:has-text("Login")', 'button:has-text("Log In")',
        'button:has-text("Sign In")', 'button:has-text("Continue")',
        'button:has-text("Access")', 'button:has-text("Enter")',
    ]:
        btn = page.locator(sel).first
        if await btn.count():
            await btn.click()
            break

    await idle(page, timeout=20000)

    # Confirm we're past the login page
    if "login" in page.url.lower() or "signin" in page.url.lower():
        print("  ERROR: Still on login page — check access code.")
        return False

    print("  Logged in successfully.")
    return True

# ── Product lookup ────────────────────────────────────────────────────────────

async def fetch_product(page, mnpn: str) -> dict:
    """
    Navigate to the product page for the given MNPN and return barcode data.

    Strategy:
      1. Try direct URL patterns (Angular hash routes common to Minfos).
      2. Fall back to the search box.
      3. Intercept JSON API responses to get structured barcode data.
      4. Fall back to DOM scraping.
    """
    result = {
        "mnpn": mnpn,
        "description": "",
        "primary_barcode": "",
        "alt_barcodes": "",
        "error": "",
    }

    # Collect any JSON responses that mention barcodes
    api_data: list[dict] = []

    async def handle_response(response):
        ct = response.headers.get("content-type", "")
        if "json" not in ct:
            return
        url = response.url
        # Only care about responses that look like product/catalogue API calls
        if not any(k in url for k in ["product", "item", "catalogue", "barcode", mnpn]):
            return
        try:
            body = await response.json()
            api_data.append({"url": url, "body": body})
        except Exception:
            pass

    page.on("response", handle_response)

    try:
        navigated = False

        # 1. Try common direct-URL patterns
        candidate_paths = [
            f"/#/products/{mnpn}",
            f"/#/product/{mnpn}",
            f"/#/product/details/{mnpn}",
            f"/#/catalogue/product/{mnpn}",
            f"/#/items/{mnpn}",
            f"/#/search?q={mnpn}",
            f"/#/search?mnpn={mnpn}",
        ]
        for path in candidate_paths:
            url = BASE_URL + path
            await page.goto(url, wait_until="domcontentloaded")
            await idle(page)
            # If a different URL loaded (redirect away = not found), skip
            if page.url.startswith(url.split("?")[0]):
                navigated = True
                break

        # 2. Fall back to search box
        if not navigated:
            await page.goto(BASE_URL, wait_until="domcontentloaded")
            await idle(page)

            for sel in [
                'input[type="search"]',
                'input[placeholder*="search" i]',
                'input[placeholder*="MNPN" i]',
                'input[placeholder*="product" i]',
                'input[formcontrolname="search"]',
                'input[formcontrolname="query"]',
                '#search', '.search-input',
            ]:
                field = page.locator(sel).first
                if await field.count():
                    await field.clear()
                    await field.fill(mnpn)
                    await field.press("Enter")
                    await idle(page)
                    break

            # Click on the first result that matches our MNPN
            for sel in [
                f'[data-mnpn="{mnpn}"]',
                f'a:has-text("{mnpn}")',
                f'td:has-text("{mnpn}")',
                'table tbody tr:first-child td:first-child',
                '.product-result:first-child',
                '.search-result:first-child a',
                'mat-row:first-child',
            ]:
                loc = page.locator(sel).first
                if await loc.count():
                    await loc.click()
                    await idle(page)
                    break

        # ── Extract from intercepted API responses ──────────────────────────

        for entry in api_data:
            body = entry["body"]
            found = _parse_api_body(body, mnpn, result)
            if found:
                break

        # ── Fall back to DOM scraping ───────────────────────────────────────

        if not result["primary_barcode"]:
            await _scrape_dom(page, result)

    except Exception as exc:
        result["error"] = str(exc)

    finally:
        page.remove_listener("response", handle_response)

    return result


def _parse_api_body(body, mnpn: str, result: dict) -> bool:
    """
    Recursively search a JSON structure for barcode fields.
    Returns True if primary barcode was found.
    """
    if isinstance(body, list):
        for item in body:
            if _parse_api_body(item, mnpn, result):
                return True
        return False

    if not isinstance(body, dict):
        return False

    # Check if this object is for our MNPN
    our_item = any(
        str(body.get(k, "")).strip() == mnpn
        for k in ["mnpn", "MNPN", "minfosNationalProductNumber", "productNumber", "id"]
    )

    if not our_item:
        # Recurse into nested objects
        for v in body.values():
            if isinstance(v, (dict, list)):
                if _parse_api_body(v, mnpn, result):
                    return True
        return False

    # Found our product — extract description
    for k in ["description", "productDescription", "name", "productName"]:
        if body.get(k):
            result["description"] = str(body[k]).strip()
            break

    # Extract primary barcode
    primary_keys = [
        "barcode", "primaryBarcode", "ean", "ean13", "upc",
        "gtin", "scanCode", "scancode",
    ]
    for k in primary_keys:
        v = str(body.get(k, "")).strip()
        if looks_like_barcode(v):
            result["primary_barcode"] = digits_only(v)
            break

    # Extract alt barcodes — may be a list or a separate field
    alt_values: list[str] = []
    alt_keys = [
        "altBarcodes", "alternativeBarcodes", "altBarcode",
        "alternativeBarcode", "secondaryBarcodes", "secondaryBarcode",
        "additionalBarcodes",
    ]
    for k in alt_keys:
        v = body.get(k)
        if isinstance(v, list):
            for item in v:
                bc = str(item.get("barcode", item) if isinstance(item, dict) else item).strip()
                if looks_like_barcode(bc):
                    alt_values.append(digits_only(bc))
        elif isinstance(v, str) and looks_like_barcode(v):
            alt_values.append(digits_only(v))

    result["alt_barcodes"] = "; ".join(alt_values)
    return bool(result["primary_barcode"])


async def _scrape_dom(page, result: dict):
    """Last-resort: scrape visible text from the page to find barcodes."""
    try:
        content = await page.content()

        # Description
        if not result["description"]:
            for sel in ["h1", "h2", ".product-name", ".product-title", "[class*='description']"]:
                loc = page.locator(sel).first
                if await loc.count():
                    t = (await loc.text_content() or "").strip()
                    if t:
                        result["description"] = t
                        break

        # Walk all table rows and labelled fields looking for "barcode"
        primary_found = False
        alt_values: list[str] = []

        rows = page.locator("tr, .field-row, .detail-row, .info-row")
        n = await rows.count()
        for i in range(n):
            row_text = (await rows.nth(i).text_content() or "").strip()
            if "barcode" not in row_text.lower():
                continue
            numbers = [x for x in re.findall(r"\b\d{8,14}\b", row_text)]
            if not numbers:
                continue
            is_alt = bool(re.search(r"alt|alternate|alternative|secondary", row_text, re.I))
            if is_alt:
                alt_values.extend(numbers)
            elif not primary_found:
                result["primary_barcode"] = numbers[0]
                primary_found = True
                if len(numbers) > 1:
                    alt_values.extend(numbers[1:])

        result["alt_barcodes"] = "; ".join(dict.fromkeys(alt_values))  # dedup, preserve order

    except Exception as exc:
        if not result["error"]:
            result["error"] = f"DOM scrape failed: {exc}"

# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    access_code = os.getenv("MINFOS_ACCESS_CODE") or input("Minfos catalogue access code: ").strip()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = f"barcodes_{timestamp}.csv"

    print(f"\nProcessing {len(MNPNS)} MNPNs  →  {output_file}\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,      # Change to True once you've verified it works
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(viewport={"width": 1400, "height": 900})
        page = await context.new_page()

        print("Step 1/2 — Logging in …")
        if not await login(page, access_code):
            await browser.close()
            sys.exit(1)

        print(f"\nStep 2/2 — Extracting barcodes …\n")

        results: list[dict] = []
        for idx, mnpn in enumerate(MNPNS, 1):
            print(f"  [{idx:2}/{len(MNPNS)}] MNPN {mnpn} … ", end="", flush=True)
            data = await fetch_product(page, mnpn)
            results.append(data)

            if data["primary_barcode"]:
                alts = f"  alts: {data['alt_barcodes']}" if data["alt_barcodes"] else ""
                print(f"barcode={data['primary_barcode']}{alts}")
            elif data["error"]:
                print(f"ERROR — {data['error']}")
            else:
                print("(no barcode found)")

        await browser.close()

    # Write CSV
    fieldnames = ["mnpn", "description", "primary_barcode", "alt_barcodes", "error"]
    with open(output_file, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    found = sum(1 for r in results if r["primary_barcode"])
    print(f"\nDone. {found}/{len(MNPNS)} barcodes found.")
    print(f"CSV saved to: {output_file}")


if __name__ == "__main__":
    asyncio.run(main())
