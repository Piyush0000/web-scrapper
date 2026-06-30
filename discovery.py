"""Shopify Discovery Engine — uses Serper.dev API for reliable search results.

Usage:
    python discovery.py [max_age_days] [--limit N] [--api-key YOUR_KEY]

    python discovery.py 60
    python discovery.py 30 --limit 20 --api-key abc123

Get a free API key at https://serper.dev (2,500 free searches)
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import sys
from urllib.parse import urlparse, quote_plus

import httpx
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

load_dotenv()  # Reads .env into os.environ automatically

from scraper import ProductScraper, ProductReport
from utils import get_domain_info

console = Console()
logger = logging.getLogger(__name__)

SERPER_ENDPOINT = "https://google.serper.dev/search"

# India-focused queries — city names + Indian niches for higher precision
QUERIES = [
    "myshopify.com new indian brand Delhi Mumbai Bangalore",
    "myshopify.com new fashion brand India 2025",
    "myshopify.com skincare brand India launched 2025",
    "myshopify.com jewellery brand India new launch",
    "myshopify.com India food snacks brand new",
    "myshopify.com handmade artisan India brand",
    "myshopify.com organic ayurvedic India brand new",
    "myshopify.com Indian clothing ethnic brand launched",
    "myshopify.com India D2C brand startup 2025",
    "myshopify.com India accessories brand new launch",
]

# Indian TLDs (including .shop and .store which Indian brands use too)
INDIA_TLDS = (".in", ".co.in", ".ind.in", ".net.in", ".org.in", ".shop", ".store")

# Keywords that signal a likely Indian brand in the subdomain
INDIA_KEYWORDS = {
    "india", "indian", "bharat", "desi", "delhi", "mumbai", "bangalore",
    "bengaluru", "chennai", "hyderabad", "kolkata", "pune", "jaipur",
    "surat", "lucknow", "ahmedabad", "hindi", "ayur", "kurta", "saree",
    "lehenga", "dupatta", "masala", "chai", "mithai", "halal",
}

# Only keep domains that look like actual Shopify stores (not agencies/blogs)
SKIP_KEYWORDS = {
    "shopify.com", "google.com", "youtube.com", "wikipedia.org",
    "twitter.com", "facebook.com", "instagram.com", "linkedin.com",
    "webservices", "agency", "blog", "news", "medium.com", "reddit.com",
    "github.com", "stackoverflow.com", "forbes.com", "techcrunch.com",
}


def _is_indian_domain(domain: str) -> bool:
    """Returns True for .myshopify.com stores or Indian TLD/keyword domains."""
    if domain.endswith(".myshopify.com"):
        # Check if the subdomain contains Indian keywords
        subdomain = domain.replace(".myshopify.com", "")
        return any(kw in subdomain.lower() for kw in INDIA_KEYWORDS)
    return any(domain.endswith(tld) for tld in INDIA_TLDS)


def _is_indian_contact(email: str, phone: str) -> bool:
    """Returns True if phone starts with +91 or email is Indian domain."""
    if phone and "+91" in phone.replace(" ", ""):
        return True
    if email:
        domain = email.split("@")[-1].lower()
        if domain.endswith(".in") or domain.endswith(".co.in"):
            return True
    return False


async def serper_search(
    client: httpx.AsyncClient,
    api_key: str,
    query: str,
    num_results: int = 30,
    tbs: str = "qdr:m",         # qdr:m = past month, qdr:w = past week
) -> set[str]:
    """Call Serper.dev and return a set of discovered domains."""
    domains: set[str] = set()
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
    payload = {
        "q": query,
        "num": num_results,
        "tbs": tbs,
        "gl": "in",     # India
        "hl": "en",     # English
    }
    try:
        resp = await client.post(SERPER_ENDPOINT, json=payload, headers=headers, timeout=15)
        if resp.status_code != 200:
            console.print(f"[red]Serper API error {resp.status_code}[/red]: {resp.text[:200]}")
            return domains
        data = resp.json()
    except Exception as exc:
        console.print(f"[red]Serper request failed for '{query}': {exc}[/red]")
        return domains

    for item in data.get("organic", []):
        link = item.get("link", "")
        if not link:
            continue
        netloc = urlparse(link).netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        # Always include actual .myshopify.com subdomains
        if netloc.endswith(".myshopify.com"):
            domains.add(netloc)
            continue
        # Include Indian TLD domains (high confidence = local brand)
        if _is_indian_domain(netloc):
            domains.add(netloc)
            continue
        # For all other custom domains, skip obvious non-store results
        if netloc and not any(kw in netloc for kw in SKIP_KEYWORDS):
            domains.add(netloc)

    return domains


async def run_discovery(
    api_key: str,
    max_age_days: int = 60,
    limit: int = 20,
    time_filter: str = "qdr:m",
) -> None:
    """Full pipeline: search → WHOIS filter → scrape → report."""

    console.print(
        f"[bold blue]Shopify Discovery Engine[/bold blue] | "
        f"Max Age: {max_age_days} days | Limit: {limit}"
    )

    all_candidates: set[str] = set()

    async with httpx.AsyncClient() as client:
        for q in QUERIES:
            console.print(f"  🔍 Searching: [cyan]{q}[/cyan]")
            found = await serper_search(client, api_key, q, tbs=time_filter)
            new = found - all_candidates
            all_candidates |= new
            console.print(f"     Found [green]{len(found)}[/green] results → "
                          f"[yellow]{len(all_candidates)}[/yellow] unique domains total")
            await asyncio.sleep(0.3)  # gentle rate-limit

    if not all_candidates:
        console.print("[red]No candidates found across all queries.[/red]")
        return

    console.print(
        f"\n[bold]Validating [cyan]{len(all_candidates)}[/cyan] domains via WHOIS...[/bold]"
    )

    fresh_targets: list[tuple[str, int]] = []
    for domain in sorted(all_candidates):
        if len(fresh_targets) >= limit:
            break
        # .myshopify.com subdomains don't have individual WHOIS — treat as fresh
        if domain.endswith(".myshopify.com"):
            console.print(f"  🆕 [green]{domain}[/green] — myshopify.com (assumed fresh)")
            fresh_targets.append((domain, 0))
            continue
        try:
            info = await asyncio.wait_for(get_domain_info(domain), timeout=10)
        except asyncio.TimeoutError:
            logger.debug("WHOIS timed out for %s — skipping", domain)
            continue
        if not info:
            logger.debug("No WHOIS data for %s — skipping", domain)
            continue
        age = info.get("age_days")
        if age is None:
            continue
        if age <= max_age_days:
            console.print(f"  🆕 [green]{domain}[/green] — {age} days old")
            fresh_targets.append((domain, age))
        else:
            logger.debug("%s is %d days old — too old", domain, age)
        await asyncio.sleep(0.2)

    if not fresh_targets:
        console.print(
            f"[yellow]No stores under {max_age_days} days old found. "
            f"Try increasing --max-age or using --time-filter qdr:y[/yellow]"
        )
        return

    console.print(
        f"\n[bold green]Scraping {len(fresh_targets)} Newborn Shopify Stores...[/bold green]\n"
    )

    urls = [f"https://{d}" for d, _ in fresh_targets]
    ages = {d: age for d, age in fresh_targets}

    async with ProductScraper(http_concurrency=5, browser_concurrency=2, enable_playwright=False) as scraper:
        tasks = [scraper.scrape(u) for u in urls]
        reports: list[ProductReport] = await asyncio.gather(*tasks)

    # ── Render table ────────────────────────────────────────────────────────
    table = Table(
        title=f"Newborn Shopify Merchant Intel (≤ {max_age_days} days old)",
        show_lines=True,
        expand=True,
    )
    table.add_column("Domain", style="cyan", no_wrap=True)
    table.add_column("Age", justify="right")
    table.add_column("Status", justify="center")
    table.add_column("🇮🇳", justify="center")  # India flag
    table.add_column("Email", style="green", overflow="fold")
    table.add_column("Phone", style="magenta", overflow="fold")

    status_styles = {"OK": "green", "Not a product URL": "yellow",
                     "Blocked": "red", "Error": "red"}

    csv_rows = []
    for r in reports:
        domain = urlparse(r.url).netloc
        age = ages.get(domain, "-")
        color = status_styles.get(r.status, "white")
        is_india = _is_indian_contact(r.email or "", r.phone or "")
        india_flag = "✅" if is_india else "?"
        table.add_row(
            domain,
            f"{age} d",
            f"[{color}]{r.status}[/{color}]",
            india_flag,
            r.email or "-",
            r.phone or "-",
        )
        csv_rows.append({
            "domain": domain,
            "age_days": ages.get(domain, ""),
            "status": r.status,
            "india_confirmed": "yes" if is_india else "maybe",
            "email": r.email or "",
            "phone": r.phone or "",
        })

    console.print(table)

    # ── Export ───────────────────────────────────────────────────────────────
    out_path = "newborn_stores.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["domain", "age_days", "status", "india_confirmed", "email", "phone"]
        )
        writer.writeheader()
        writer.writerows(csv_rows)
    console.print(f"[green]Saved → {out_path}[/green]")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Find new Shopify stores via Serper.dev + WHOIS age filter")
    p.add_argument("max_age", nargs="?", type=int, default=60,
                   help="Maximum domain age in days (default: 60)")
    p.add_argument("--limit", type=int, default=20,
                   help="Maximum stores to scrape (default: 20)")
    p.add_argument("--api-key", default=os.getenv("SERPER_API_KEY", ""),
                   help="Serper.dev API key (or set SERPER_API_KEY env var)")
    p.add_argument("--time-filter", default="qdr:m",
                   choices=["qdr:d", "qdr:w", "qdr:m", "qdr:y"],
                   help="Google time filter: d=day, w=week, m=month, y=year (default: qdr:m)")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if not args.api_key:
        console.print("[bold red]Error:[/bold red] No API key provided.")
        console.print("  Get a free key at [link]https://serper.dev[/link]")
        console.print("  Then run: [cyan]python discovery.py 60 --api-key YOUR_KEY[/cyan]")
        console.print("  Or set the env var: [cyan]$env:SERPER_API_KEY='YOUR_KEY'[/cyan]")
        sys.exit(1)

    logging.basicConfig(level=logging.WARNING)
    asyncio.run(run_discovery(
        api_key=args.api_key,
        max_age_days=args.max_age,
        limit=args.limit,
        time_filter=args.time_filter,
    ))
