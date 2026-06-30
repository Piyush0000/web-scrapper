"""CLI orchestrator: paste product URLs in, get merchant intelligence out."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys
from pathlib import Path
from typing import Iterable

from rich.console import Console
from rich.logging import RichHandler
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from scraper import CSV_FIELDS, ProductReport, ProductScraper

console = Console()


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True, markup=False)],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING if not verbose else logging.INFO)


def load_urls(args: argparse.Namespace) -> list[str]:
    raw: list[str] = []
    if args.urls:
        raw.extend(args.urls)
    if args.file:
        path = Path(args.file)
        if not path.exists():
            console.print(f"[red]Input file not found:[/red] {path}")
            sys.exit(2)
        raw.extend(
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
    if not raw:
        console.print("[red]No URLs supplied. Pass -u or -f.[/red]")
        sys.exit(2)

    seen: set[str] = set()
    cleaned: list[str] = []
    for item in raw:
        if not item.startswith(("http://", "https://")):
            item = "https://" + item
        if item not in seen:
            seen.add(item)
            cleaned.append(item)
    return cleaned


async def run_pipeline(
    urls: Iterable[str],
    http_concurrency: int,
    browser_concurrency: int,
    enable_playwright: bool,
) -> list[ProductReport]:
    urls = list(urls)
    results: list[ProductReport] = []

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    )

    with progress:
        task_id = progress.add_task("Scraping URLs", total=len(urls))
        async with ProductScraper(
            http_concurrency=http_concurrency,
            browser_concurrency=browser_concurrency,
            enable_playwright=enable_playwright,
        ) as scraper:
            async def _wrapped(u: str) -> ProductReport:
                try:
                    report = await scraper.scrape(u)
                except Exception as exc:
                    report = ProductReport(
                        url=u,
                        status="Error",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                progress.update(
                    task_id, advance=1,
                    description=f"Scraping URLs - last: {report.platform}/{report.status}",
                )
                return report

            tasks = [asyncio.create_task(_wrapped(u)) for u in urls]
            for coro in asyncio.as_completed(tasks):
                results.append(await coro)

    order = {u: i for i, u in enumerate(urls)}
    results.sort(key=lambda r: order.get(r.url, 1_000_000))
    return results


def render_summary(reports: list[ProductReport]) -> None:
    table = Table(title="Merchant Intelligence", show_lines=False, expand=True)
    table.add_column("Platform", justify="center")
    table.add_column("Status", justify="center")
    table.add_column("Product", overflow="fold")
    table.add_column("Price", justify="right")
    table.add_column("Merchant", overflow="fold")
    table.add_column("Email", overflow="fold")
    table.add_column("Phone", overflow="fold")

    status_styles = {
        "OK": "green",
        "Blocked": "red",
        "Captcha": "red",
        "Not Found": "yellow",
        "Unsupported": "yellow",
        "Skipped": "yellow",
        "Error": "red",
    }

    for r in reports:
        color = status_styles.get(r.status, "white")
        table.add_row(
            r.platform,
            f"[{color}]{r.status}[/{color}]",
            r.product_name or "-",
            r.price or "-",
            r.merchant_name or "-",
            r.email or "-",
            r.phone or "-",
        )

    console.print(table)


def export_json(reports: list[ProductReport], path: Path) -> None:
    payload = [r.to_dict() for r in reports]
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    console.print(f"[green]Wrote JSON ->[/green] {path} ({len(reports)} row(s))")


def export_csv(reports: list[ProductReport], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(CSV_FIELDS))
        writer.writeheader()
        for r in reports:
            writer.writerow(r.to_csv_row())
    console.print(f"[green]Wrote CSV ->[/green] {path} ({len(reports)} row(s))")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="merchant-intel",
        description="Multi-platform product/merchant intelligence scraper "
                    "(Shopify, Amazon, Meesho).",
    )
    parser.add_argument(
        "-u", "--url", dest="urls", nargs="+",
        help="One or more product URLs to scrape (space-separated).",
    )
    parser.add_argument("-f", "--file", help="File with one URL per line.")
    parser.add_argument(
        "-o", "--output-prefix", default="results",
        help="Output prefix; writes <prefix>.json and <prefix>.csv (default: results).",
    )
    parser.add_argument(
        "--no-csv", action="store_true", help="Skip writing the CSV file."
    )
    parser.add_argument(
        "--no-json", action="store_true", help="Skip writing the JSON file."
    )
    parser.add_argument(
        "--http-concurrency", type=int, default=10,
        help="Concurrent httpx (Shopify) scrapes (default: 10).",
    )
    parser.add_argument(
        "--browser-concurrency", type=int, default=3,
        help="Concurrent Playwright (Amazon/Meesho) scrapes (default: 3).",
    )
    parser.add_argument(
        "--no-playwright", action="store_true",
        help="Disable Playwright; Amazon/Meesho URLs will be skipped.",
    )
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable debug logging.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.verbose)
    urls = load_urls(args)

    console.print(
        f"[bold]Targets:[/bold] {len(urls)} URL(s) | "
        f"http_conc={args.http_concurrency} | "
        f"browser_conc={args.browser_concurrency} | "
        f"playwright={'off' if args.no_playwright else 'on'}"
    )

    try:
        reports = asyncio.run(
            run_pipeline(
                urls,
                http_concurrency=args.http_concurrency,
                browser_concurrency=args.browser_concurrency,
                enable_playwright=not args.no_playwright,
            )
        )
    except KeyboardInterrupt:
        console.print("[yellow]Interrupted by user.[/yellow]")
        return 130

    render_summary(reports)

    prefix = Path(args.output_prefix)
    if not args.no_json:
        export_json(reports, prefix.with_suffix(".json"))
    if not args.no_csv:
        export_csv(reports, prefix.with_suffix(".csv"))

    return 0


if __name__ == "__main__":
    sys.exit(main())
