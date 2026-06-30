"""URL-driven product/merchant scraping orchestrator.

A single ProductScraper drives all three platforms behind two semaphores:
- http_concurrency for the cheap httpx path (Shopify .js)
- browser_concurrency for the expensive Playwright path (Amazon, Meesho)

The Playwright browser is launched lazily and shared across all browser tasks;
each scrape gets its own browser context for isolation.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from platforms import (
    detect_platform,
    probe_shopify_custom_domain,
    scrape_amazon_product,
    scrape_meesho_product,
    scrape_shopify_product,
)

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


@dataclass
class ProductReport:
    url: str
    platform: str = "unknown"
    status: str = "pending"
    product_name: Optional[str] = None
    price: Optional[str] = None
    merchant_name: Optional[str] = None
    merchant_address: Optional[str] = None
    merchant_rating: Optional[str] = None
    merchant_location: Optional[str] = None
    contact_info: dict[str, Optional[str]] = field(
        default_factory=lambda: {"email": None, "phone": None}
    )
    error: Optional[str] = None
    elapsed_seconds: float = 0.0

    @property
    def email(self) -> Optional[str]:
        return self.contact_info.get("email") if self.contact_info else None

    @property
    def phone(self) -> Optional[str]:
        return self.contact_info.get("phone") if self.contact_info else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "platform": self.platform,
            "status": self.status,
            "product_name": self.product_name,
            "price": self.price,
            "merchant_name": self.merchant_name,
            "merchant_address": self.merchant_address,
            "merchant_rating": self.merchant_rating,
            "merchant_location": self.merchant_location,
            "contact_info": self.contact_info,
            "error": self.error,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
        }

    def to_csv_row(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "platform": self.platform,
            "status": self.status,
            "product_name": self.product_name or "",
            "price": self.price or "",
            "merchant_name": self.merchant_name or "",
            "merchant_address": self.merchant_address or "",
            "merchant_rating": self.merchant_rating or "",
            "merchant_location": self.merchant_location or "",
            "email": self.email or "",
            "phone": self.phone or "",
            "error": self.error or "",
            "elapsed_seconds": round(self.elapsed_seconds, 2),
        }


CSV_FIELDS = (
    "url", "platform", "status",
    "product_name", "price",
    "merchant_name", "merchant_address",
    "merchant_rating", "merchant_location",
    "email", "phone",
    "error", "elapsed_seconds",
)


class ProductScraper:
    """Routes each URL to its platform handler under two concurrency budgets."""

    def __init__(
        self,
        http_concurrency: int = 10,
        browser_concurrency: int = 3,
        request_timeout: float = 20.0,
        enable_playwright: bool = True,
    ) -> None:
        self._http_sem = asyncio.Semaphore(http_concurrency)
        self._browser_sem = asyncio.Semaphore(browser_concurrency)
        self._timeout = httpx.Timeout(request_timeout, connect=10.0)
        self._enable_playwright = enable_playwright

        self._client: Optional[httpx.AsyncClient] = None
        self._pw = None
        self._pw_browser = None
        self._pw_lock = asyncio.Lock()

    async def __aenter__(self) -> "ProductScraper":
        self._client = httpx.AsyncClient(
            headers=DEFAULT_HEADERS,
            timeout=self._timeout,
            follow_redirects=True,
            http2=False,
        )
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._pw_browser is not None:
            try:
                await self._pw_browser.close()
            except Exception:
                pass
            self._pw_browser = None
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None

    async def _ensure_browser(self):
        if self._pw_browser is not None:
            return self._pw_browser
        async with self._pw_lock:
            if self._pw_browser is None:
                from playwright.async_api import async_playwright
                self._pw = await async_playwright().start()
                self._pw_browser = await self._pw.chromium.launch(headless=True)
        return self._pw_browser

    async def scrape(self, url: str) -> ProductReport:
        started = time.monotonic()
        report = ProductReport(url=url)
        try:
            assert self._client is not None
            platform = detect_platform(url)
            if platform == "unknown":
                if await probe_shopify_custom_domain(self._client, url):
                    platform = "shopify"
            report.platform = platform

            if platform == "shopify":
                async with self._http_sem:
                    await scrape_shopify_product(self._client, url, report)
            elif platform == "amazon":
                if not self._enable_playwright:
                    report.status = "Skipped"
                    report.error = "Playwright disabled but required for Amazon"
                else:
                    browser = await self._ensure_browser()
                    async with self._browser_sem:
                        await scrape_amazon_product(browser, url, report)
            elif platform == "meesho":
                if not self._enable_playwright:
                    report.status = "Skipped"
                    report.error = "Playwright disabled but required for Meesho"
                else:
                    browser = await self._ensure_browser()
                    async with self._browser_sem:
                        await scrape_meesho_product(browser, url, report)
            else:
                report.status = "Unsupported"
                report.error = "URL did not match a supported platform"
        except Exception as exc:
            logger.exception("Unhandled error scraping %s", url)
            report.status = "Error"
            report.error = f"{type(exc).__name__}: {exc}"
        finally:
            report.elapsed_seconds = time.monotonic() - started
        return report
