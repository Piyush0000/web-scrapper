"""Platform-specific product/merchant scrapers and a URL router.

Three platforms are supported:
- Shopify (custom domain or *.myshopify.com): /products/<handle>.js endpoint
- Amazon (amazon.<tld>): Playwright-rendered page + seller-profile click-through
- Meesho (meesho.com): Playwright-rendered page, prefers __NEXT_DATA__ JSON

Each scraper takes a partly-populated ProductReport and mutates it in place.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional, TYPE_CHECKING
from urllib.parse import urlparse

import httpx

from playwright_stealth import Stealth
from utils import collect_contacts, detect_shopify

if TYPE_CHECKING:
    from playwright.async_api import Browser
    from scraper import ProductReport

logger = logging.getLogger(__name__)

PLAYWRIGHT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def detect_platform(url: str) -> str:
    """Return one of: shopify | amazon | meesho | unknown."""
    host = (urlparse(url).netloc or "").lower()
    if not host:
        return "unknown"
    if host.endswith(".myshopify.com") or host == "myshopify.com":
        return "shopify"
    if ".amazon." in f".{host}." or host.startswith("amazon."):
        return "amazon"
    if host == "meesho.com" or host.endswith(".meesho.com"):
        return "meesho"
    return "unknown"


async def probe_shopify_custom_domain(
    client: httpx.AsyncClient, url: str
) -> bool:
    """Custom-domain Shopify stores need a fingerprint probe to identify."""
    parsed = urlparse(url)
    # Even if it's not a product path, we can still probe the root for 
    # Shopify fingerprints to enable contact extraction.
    is_shop, _ = await detect_shopify(client, parsed.netloc)
    return is_shop


# ---------------------------------------------------------------------------
# Shopify
# ---------------------------------------------------------------------------

def _format_cents(cents: Optional[int]) -> Optional[str]:
    if cents is None:
        return None
    try:
        return f"{int(cents) / 100:.2f}"
    except (TypeError, ValueError):
        return None


async def scrape_shopify_product(
    client: httpx.AsyncClient, url: str, report: "ProductReport"
) -> None:
    parsed = urlparse(url)
    domain = parsed.netloc
    base = f"{parsed.scheme or 'https'}://{domain}"

    handle_match = re.search(r"/products/([^/?#]+)", parsed.path or "")
    if not handle_match:
        report.status = "Not a product URL"
        report.error = "URL did not match /products/<handle>"
        # Still try contacts on the root domain.
        email, phone = await collect_contacts(client, domain)
        report.contact_info = {"email": email, "phone": phone}
        return

    handle = handle_match.group(1)
    js_url = f"{base}/products/{handle}.js"

    try:
        r = await client.get(js_url, timeout=15)
    except httpx.HTTPError as exc:
        report.status = "Error"
        report.error = f"products.js fetch failed: {exc}"
        return

    if r.status_code == 404:
        report.status = "Not Found"
        report.error = f"products.js returned 404 for handle {handle}"
    elif r.status_code in (401, 403, 429):
        report.status = "Blocked"
        report.error = f"products.js returned HTTP {r.status_code}"
    elif r.status_code != 200:
        report.status = "Error"
        report.error = f"products.js returned HTTP {r.status_code}"
    else:
        try:
            data = r.json()
        except (json.JSONDecodeError, ValueError):
            report.status = "Error"
            report.error = "products.js was not JSON"
            data = None

        if data:
            report.product_name = (data.get("title") or "").strip() or None
            report.merchant_name = (data.get("vendor") or "").strip() or None
            price = _format_cents(data.get("price"))
            if price is None:
                # Some themes use price_min/max instead of price.
                price = _format_cents(data.get("price_min"))
            report.price = price
            report.status = "OK"

    # Contact fallback always runs — the shop's email/phone is the merchant signal.
    email, phone = await collect_contacts(client, domain)
    report.contact_info = {"email": email, "phone": phone}


# ---------------------------------------------------------------------------
# Amazon
# ---------------------------------------------------------------------------

async def _amazon_safe_text(locator) -> Optional[str]:
    try:
        if await locator.count() == 0:
            return None
        text = await locator.first.inner_text(timeout=3000)
        return text.strip() or None
    except Exception:
        return None


async def scrape_amazon_product(
    browser: "Browser", url: str, report: "ProductReport"
) -> None:
    context = await browser.new_context(
        user_agent=PLAYWRIGHT_UA,
        locale="en-US",
        viewport={"width": 1366, "height": 900},
    )
    await Stealth().apply_stealth_async(context)
    try:
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:
            report.status = "Error"
            report.error = f"Amazon page load failed: {exc}"
            return

        title_text = await page.title()
        body_text = (await _amazon_safe_text(page.locator("body"))) or ""
        if "Enter the characters" in body_text or "captcha" in title_text.lower():
            report.status = "Captcha"
            report.error = "Amazon served a captcha interstitial"
            return

        report.product_name = await _amazon_safe_text(page.locator("#productTitle"))
        report.price = await _amazon_safe_text(
            page.locator(".a-price .a-offscreen")
        )

        # "Sold by" can live in several places depending on layout.
        merchant_selectors = (
            "#sellerProfileTriggerId",
            "#merchant-info a",
            "[data-csa-c-content-id='offer-display-feature'] a",
            "#tabular-buybox a[href*='seller']",
        )
        merchant_locator = None
        for sel in merchant_selectors:
            loc = page.locator(sel)
            if await loc.count() > 0:
                merchant_locator = loc.first
                break

        seller_href: Optional[str] = None
        if merchant_locator is not None:
            report.merchant_name = await _amazon_safe_text(merchant_locator)
            try:
                seller_href = await merchant_locator.get_attribute("href")
            except Exception:
                seller_href = None
        else:
            # Last resort: plain text near "Sold by".
            report.merchant_name = await _amazon_safe_text(page.locator("#merchant-info"))

        if not report.product_name and not report.price and not report.merchant_name:
            report.status = "Blocked"
            report.error = "No product fields extracted (page likely blocked)"
            return

        if seller_href:
            seller_url = seller_href if seller_href.startswith("http") else (
                f"{urlparse(url).scheme}://{urlparse(url).netloc}{seller_href}"
            )
            try:
                await page.goto(seller_url, wait_until="domcontentloaded",
                                timeout=20_000)
                # Detailed Seller Information block — Amazon renders pairs of rows.
                rows = page.locator(
                    "#page-section-detail-seller-info .a-row, "
                    "#seller-info-section .a-row"
                )
                count = await rows.count()
                seller_block: dict[str, str] = {}
                for i in range(min(count, 30)):
                    row_text = await _amazon_safe_text(rows.nth(i))
                    if not row_text or ":" not in row_text:
                        continue
                    key, _, value = row_text.partition(":")
                    seller_block[key.strip()] = value.strip()
                report.merchant_address = (
                    seller_block.get("Business Address")
                    or seller_block.get("Business address")
                )
                # If we found a legal/business name, prefer it over the display name.
                legal = (
                    seller_block.get("Business Name")
                    or seller_block.get("Business name")
                )
                if legal:
                    report.merchant_name = legal
            except Exception as exc:
                logger.debug("Amazon seller profile fetch failed: %s", exc)

        report.status = "OK"
    finally:
        await context.close()


# ---------------------------------------------------------------------------
# Meesho
# ---------------------------------------------------------------------------

def _walk_for(node, predicate):
    """Yield values in a nested JSON structure that satisfy `predicate(key, value)`."""
    if isinstance(node, dict):
        for k, v in node.items():
            if predicate(k, v):
                yield v
            yield from _walk_for(v, predicate)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_for(item, predicate)


def _extract_from_next_data(data: dict) -> dict:
    """Best-effort harvest of product + supplier fields from Meesho __NEXT_DATA__."""
    out: dict[str, Optional[str]] = {
        "product_name": None,
        "price": None,
        "merchant_name": None,
        "merchant_rating": None,
        "merchant_location": None,
    }

    # Product name lives under various keys; pick the first plausible one.
    for value in _walk_for(
        data,
        lambda k, v: k in ("name", "productName", "product_name") and isinstance(v, str) and v.strip(),
    ):
        out["product_name"] = value.strip()
        break

    # Price — Meesho exposes various keys: transient_price, price, productPrice.
    for value in _walk_for(
        data,
        lambda k, v: k in ("transient_price", "min_product_price", "price", "productPrice")
        and isinstance(v, (int, float, str)),
    ):
        out["price"] = str(value)
        break

    # Supplier name
    for value in _walk_for(
        data,
        lambda k, v: k in ("supplier_name", "shop_name", "supplierName", "shopName")
        and isinstance(v, str) and v.strip(),
    ):
        out["merchant_name"] = value.strip()
        break

    # Supplier rating
    for value in _walk_for(
        data,
        lambda k, v: k in ("supplier_rating", "supplierRating", "shop_rating", "rating")
        and isinstance(v, (int, float, str)),
    ):
        try:
            out["merchant_rating"] = f"{float(value):.2f}"
        except (TypeError, ValueError):
            out["merchant_rating"] = str(value)
        break

    # Location
    for value in _walk_for(
        data,
        lambda k, v: k in ("supplier_location", "shop_location", "city", "supplierCity")
        and isinstance(v, str) and v.strip(),
    ):
        out["merchant_location"] = value.strip()
        break

    return out


async def scrape_meesho_product(
    browser: "Browser", url: str, report: "ProductReport"
) -> None:
    context = await browser.new_context(
        user_agent=PLAYWRIGHT_UA,
        locale="en-IN",
        viewport={"width": 1366, "height": 900},
    )
    await Stealth().apply_stealth_async(context)
    try:
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:
            report.status = "Error"
            report.error = f"Meesho page load failed: {exc}"
            return

        # Prefer __NEXT_DATA__ — Meesho is a Next.js app and embeds full state.
        next_text = await page.evaluate(
            "() => document.getElementById('__NEXT_DATA__')?.textContent || null"
        )
        harvested: dict = {}
        if next_text:
            try:
                harvested = _extract_from_next_data(json.loads(next_text))
            except (json.JSONDecodeError, ValueError):
                harvested = {}

        # DOM fallbacks for anything __NEXT_DATA__ didn't yield.
        if not harvested.get("product_name"):
            harvested["product_name"] = await _amazon_safe_text(page.locator("h1"))

        if not harvested.get("merchant_name"):
            # Common Meesho pattern: a "Sold By" / "Shop Name" label
            for txt in ("Sold By", "Shop Name", "Supplier"):
                loc = page.locator(f"text=/{txt}/i")
                if await loc.count() > 0:
                    parent_text = await _amazon_safe_text(loc.first.locator(".."))
                    if parent_text:
                        harvested["merchant_name"] = parent_text
                        break

        if not harvested.get("price"):
            price_loc = page.locator(
                "[class*='price' i], [class*='Price']"
            )
            harvested["price"] = await _amazon_safe_text(price_loc)

        report.product_name = harvested.get("product_name")
        report.price = harvested.get("price")
        report.merchant_name = harvested.get("merchant_name")
        report.merchant_rating = harvested.get("merchant_rating")
        report.merchant_location = harvested.get("merchant_location")

        if not (report.product_name or report.merchant_name):
            report.status = "Blocked"
            report.error = "No product or supplier fields extracted"
        else:
            report.status = "OK"
    finally:
        await context.close()
