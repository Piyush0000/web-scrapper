import asyncio
import re
import httpx
import whois
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor

from bs4 import BeautifulSoup


def normalize_url(input_str: str) -> str:
    """Cleans up a domain/URL string to just the netloc."""
    if not input_str.startswith(('http://', 'https://')):
        input_str = 'https://' + input_str
    parsed = urlparse(input_str)
    return parsed.netloc or input_str


async def detect_shopify(
    client: httpx.AsyncClient, domain: str
) -> tuple[bool, Optional[str]]:
    """Fingerprint a store via headers and HTML.

    Returns (is_shopify, homepage_html). Returning the HTML lets callers
    reuse the same fetch for contact extraction — no extra request.
    """
    try:
        r = await client.get(f"https://{domain}", timeout=10)
    except Exception:
        return False, None

    shopify_headers = (
        "x-shopify-stage",
        "x-shopid",
        "x-shardid",
        "x-shopify-shopid",
        "x-storefront-variants",
    )
    if any(h in r.headers for h in shopify_headers):
        return True, r.text

    body_lower = r.text.lower()
    indicators = (
        "cdn.shopify.com",
        "shopify.theme",
        "shopify.shop",
        'content="shopify"',
        "shopify-pay-button",
    )
    if any(ind in body_lower for ind in indicators):
        return True, r.text

    return False, r.text


# ---------------------------------------------------------------------------
# Contact intelligence
# ---------------------------------------------------------------------------

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Phone candidates: either a leading + (international) or a separator
# (space/dot/hyphen/parens) in the run — bare digit strings are usually SKUs
# or tracking numbers, not phone numbers. Final validity is enforced by
# _is_valid_phone on digit count.
PHONE_RE = re.compile(
    r"\+\d[\d\s().\-]{8,18}\d"
    r"|"
    r"\(?\d{2,4}\)?[\s.\-]\d{2,4}[\s.\-]\d{2,5}(?:[\s.\-]\d{1,5})?"
)

EMAIL_BLOCKLIST_DOMAINS = {
    "shopify.com", "shopify.io", "myshopify.com",
    "sentry.io", "sentry-cdn.com",
    "example.com", "example.org", "example.net",
    "wixpress.com", "wordpress.com", "wp.com",
    "googleapis.com", "gstatic.com", "google.com",
    "facebook.com", "fb.com", "instagram.com",
    "schema.org", "w3.org",
}
EMAIL_BAD_EXTS = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
    ".woff", ".woff2", ".ttf", ".eot", ".ico",
)

CONTACT_PATHS = (
    "/pages/contact",
    "/pages/contact-us",
    "/pages/about-us",
    "/pages/about",
    "/contact",
    "/contact-us",
)


async def collect_contacts(
    client: httpx.AsyncClient,
    domain: str,
    homepage_html: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Home page first (free if HTML already fetched), then walk contact paths.

    Stops as soon as both email and phone are filled. Failures on any path
    are swallowed — contact extraction is best-effort.
    """
    if homepage_html is None:
        try:
            r = await client.get(f"https://{domain}", timeout=10)
            homepage_html = r.text
        except Exception:
            homepage_html = None

    email, phone = (None, None)
    if homepage_html:
        email, phone = extract_contacts(homepage_html)

    for path in CONTACT_PATHS:
        if email and phone:
            break
        try:
            r = await client.get(f"https://{domain}{path}", timeout=10)
        except Exception:
            continue
        if r.status_code != 200 or not r.text:
            continue
        e, p = extract_contacts(r.text)
        email = email or e
        phone = phone or p

    return email, phone


def _is_valid_email(email: str) -> bool:
    email = email.strip().rstrip(".,;:)>]")
    if not email or email.lower().endswith(EMAIL_BAD_EXTS):
        return False
    local, _, domain = email.partition("@")
    if not local or not domain or local.startswith(".") or local.endswith("."):
        return False
    domain_lower = domain.lower()
    if domain_lower in EMAIL_BLOCKLIST_DOMAINS:
        return False
    for blocked in EMAIL_BLOCKLIST_DOMAINS:
        if domain_lower.endswith("." + blocked):
            return False
    return True


def _is_valid_phone(candidate: str) -> bool:
    digits = re.sub(r"\D", "", candidate)
    if not (10 <= len(digits) <= 15):
        return False
    if len(set(digits)) == 1:
        return False
    return True


def extract_contacts(html: str) -> tuple[Optional[str], Optional[str]]:
    """Pull a single best email and phone from rendered HTML.

    Priority: mailto:/tel: hrefs (highest confidence), then regex on visible text.
    Returns (email, phone) — either may be None.
    """
    if not html:
        return None, None

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    email: Optional[str] = None
    phone: Optional[str] = None

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        href_lower = href.lower()
        if not email and href_lower.startswith("mailto:"):
            cand = href[7:].split("?", 1)[0].strip()
            if _is_valid_email(cand):
                email = cand.rstrip(".,;:)>]")
        elif not phone and href_lower.startswith("tel:"):
            cand = href[4:].strip()
            if _is_valid_phone(cand):
                phone = cand
        if email and phone:
            return email, phone

    text = soup.get_text(" ", strip=True)

    if not email:
        for match in EMAIL_RE.findall(text):
            if _is_valid_email(match):
                email = match.rstrip(".,;:)>]")
                break

    if not phone:
        for match in PHONE_RE.findall(text):
            cand = match.strip()
            if _is_valid_phone(cand):
                phone = cand
                break

    return email, phone


# ---------------------------------------------------------------------------
# WHOIS
# ---------------------------------------------------------------------------

def _whois_sync(domain: str):
    """Sync wrapper for whois lookup with robust date handling."""
    try:
        w = whois.whois(domain)
        created = w.creation_date

        if isinstance(created, list):
            created = created[0]

        if not created or not isinstance(created, datetime):
            return None

        # Normalize to UTC-aware so subtraction works regardless of how
        # the registrar reports the date (some are naive, some tz-aware).
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        age_days = (now - created).days
        return {
            "created": created.isoformat(),
            "age_days": age_days,
            "registrar": w.registrar
        }
    except Exception:
        return None


async def get_domain_info(domain: str):
    """Async wrapper for the sync WHOIS lookup to prevent event loop blocking."""
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as pool:
        return await loop.run_in_executor(pool, _whois_sync, domain)
