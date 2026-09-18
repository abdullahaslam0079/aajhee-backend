"""Free product page importer: fetch HTML and extract Product data via JSON-LD / Open Graph."""

from __future__ import annotations

import ipaddress
import json
import re
import socket
from decimal import Decimal, InvalidOperation
from typing import Any, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

from .ai_enrichment import enrich_product_draft

USER_AGENT = (
    "Mozilla/5.0 (compatible; AajheeBot/1.0; +https://aajhee.com; "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36)"
)
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
FETCH_TIMEOUT_SECONDS = 15
MAX_RESPONSE_BYTES = 2_000_000
MAX_IMAGES = 12
MAX_PAGE_TEXT_CHARS = 8_000


class ProductImportError(Exception):
    def __init__(self, code: str, message: str, http_status: int | None = None):
        self.code = code
        self.message = message
        self.http_status = http_status
        super().__init__(message)


def import_product_from_url(
    url: str,
    *,
    categories: Sequence[str] | None = None,
    enrich: bool = True,
) -> dict[str, Any]:
    cleaned = (url or "").strip()
    if not cleaned:
        raise ProductImportError("invalid_url", "URL is required.")

    validated = _validate_public_http_url(cleaned)
    html = _fetch_html(validated)
    soup = BeautifulSoup(html, "lxml")

    json_ld = _from_json_ld(soup, validated)
    open_graph = _from_open_graph(soup, validated)
    fallback = _from_html_fallback(soup)

    draft = _merge_sources(validated, json_ld, open_graph, fallback)
    if not draft["title"] and not draft["image_urls"]:
        raise ProductImportError(
            "unsupported_page",
            "Could not extract product details from this page.",
        )

    draft.setdefault("suggested_category", None)
    draft.setdefault("suggested_discount_percent", None)
    draft.setdefault("suggested_discount_copy", "")
    draft["ai_enriched"] = False

    if not enrich:
        return draft

    page_text = _page_text_excerpt(soup)
    return enrich_product_draft(
        draft,
        categories=list(categories or []),
        page_text=page_text,
    )


def _page_text_excerpt(soup: BeautifulSoup) -> str:
    """Visible page text for AI context (scripts/styles stripped)."""
    clone = BeautifulSoup(str(soup), "lxml")
    for tag in clone(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()
    text = clone.get_text(separator=" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= MAX_PAGE_TEXT_CHARS:
        return text
    return text[: MAX_PAGE_TEXT_CHARS - 1].rstrip() + "…"


def validate_public_http_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ProductImportError("invalid_url", "Only http(s) URLs are supported.")
    if not parsed.hostname:
        raise ProductImportError("invalid_url", "URL host is missing.")

    host = parsed.hostname.lower()
    if host in {"localhost"} or host.endswith(".localhost"):
        raise ProductImportError("invalid_url", "Localhost URLs are not allowed.")

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ProductImportError("fetch_failed", "Could not resolve host.") from exc

    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        ):
            raise ProductImportError("invalid_url", "URL host is not publicly reachable.")

    return parsed.geturl()


def fetch_public_text(url: str) -> tuple[str, int]:
    last_error: ProductImportError | None = None
    for user_agent in (USER_AGENT, BROWSER_USER_AGENT):
        try:
            text, status_code = _fetch_public_text_once(url, user_agent=user_agent)
        except ProductImportError as exc:
            last_error = exc
            if exc.http_status in {401, 403, 429} and user_agent != BROWSER_USER_AGENT:
                continue
            raise
        if _looks_like_bot_block(text, status_code):
            last_error = ProductImportError(
                "fetch_blocked",
                _blocked_message(url, status_code or 403),
                http_status=status_code or 403,
            )
            if user_agent != BROWSER_USER_AGENT:
                continue
            raise last_error
        return text, status_code
    if last_error:
        raise last_error
    raise ProductImportError("fetch_failed", "Failed to fetch page.")


def _fetch_public_text_once(url: str, *, user_agent: str) -> tuple[str, int]:
    request = Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/csv;q=0.8,*/*;q=0.7",
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            status_code = int(getattr(response, "status", 200) or 200)
            content_type = (response.headers.get("Content-Type") or "").lower()
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(65_536)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise ProductImportError(
                        "fetch_failed",
                        "Page is too large to import.",
                    )
                chunks.append(chunk)
            raw = b"".join(chunks)
            charset = "utf-8"
            if "charset=" in content_type:
                charset = content_type.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
            try:
                text = raw.decode(charset, errors="replace")
            except LookupError:
                text = raw.decode("utf-8", errors="replace")
            return text, status_code
    except ProductImportError:
        raise
    except HTTPError as exc:
        code = int(exc.code or 0)
        if code in {404, 410}:
            raise ProductImportError(
                "http_404",
                f"Failed to fetch page (HTTP {code}).",
                http_status=code,
            ) from exc
        if code in {401, 403, 429}:
            raise ProductImportError(
                "fetch_blocked",
                _blocked_message(url, code),
                http_status=code,
            ) from exc
        raise ProductImportError(
            "fetch_failed",
            f"Failed to fetch page (HTTP {code}).",
            http_status=code,
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ProductImportError("fetch_failed", "Failed to fetch page.") from exc


def _blocked_message(url: str, status_code: int) -> str:
    host = (urlparse(url).hostname or "this shop").lower()
    if host.startswith("www."):
        host = host[4:]
    return (
        f"{host} blocked automated access (HTTP {status_code}). "
        "The page opens in your browser, but the shop blocks Goluto's server. "
        "Use an affiliate feed, or import individual product URLs."
    )


def _looks_like_bot_block(text: str, status_code: int) -> bool:
    if status_code in {401, 403, 429}:
        return True
    sample = (text or "")[:4000].lower()
    if "access denied" in sample and "permission to access" in sample:
        return True
    if "bm-verify" in sample:
        return True
    return False


def _fetch_html(url: str) -> str:
    text, _status = fetch_public_text(url)
    return text


def _validate_public_http_url(url: str) -> str:
    return validate_public_http_url(url)


def _from_json_ld(soup: BeautifulSoup, page_url: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "title": None,
        "description": None,
        "detailed_description": None,
        "original_price": None,
        "currency": None,
        "image_urls": [],
        "confidence": {},
    }

    scripts = soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)})
    nodes: list[dict[str, Any]] = []
    for script in scripts:
        text = script.string or script.get_text() or ""
        text = text.strip()
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        nodes.extend(_flatten_json_ld(data))

    product = next((n for n in nodes if _is_type(n, "Product")), None)
    offer_node = None
    if product is None:
        offer_node = next((n for n in nodes if _is_type(n, "Offer")), None)
    else:
        offers = product.get("offers")
        if isinstance(offers, list) and offers:
            first = offers[0]
            offer_node = first if isinstance(first, dict) else None
        elif isinstance(offers, dict):
            offer_node = offers

    source = product or offer_node
    if not source:
        return result

    title = _as_text(source.get("name") or source.get("headline"))
    if title:
        result["title"] = title
        result["confidence"]["title"] = "json_ld"

    description = _as_text(source.get("description"))
    if description:
        result["description"] = _truncate(description, 500)
        result["detailed_description"] = description
        result["confidence"]["description"] = "json_ld"

    images = _normalize_images(source.get("image"), page_url)
    if images:
        result["image_urls"] = images
        result["confidence"]["images"] = "json_ld"

    if offer_node:
        sale_price = _parse_price(
            offer_node.get("price") or offer_node.get("lowPrice")
        )
        list_price = _parse_price(
            offer_node.get("highPrice") or offer_node.get("listPrice")
        )
        spec = offer_node.get("priceSpecification")
        if isinstance(spec, list) and spec:
            spec = spec[0]
        if isinstance(spec, dict) and list_price is None:
            list_price = _parse_price(spec.get("price") if spec.get("priceType") else None)
            if list_price is None:
                list_price = _parse_price(spec.get("listPrice") or spec.get("price"))
        if sale_price is not None:
            result["sale_price"] = f"{sale_price:.2f}"
            result["original_price"] = f"{sale_price:.2f}"
            result["confidence"]["price"] = "json_ld"
        if list_price is not None:
            result["list_price"] = f"{list_price:.2f}"
            if sale_price is not None and list_price > sale_price:
                result["original_price"] = f"{list_price:.2f}"
        currency = _as_text(offer_node.get("priceCurrency"))
        if currency:
            result["currency"] = currency
            result["confidence"]["currency"] = "json_ld"
        availability = _normalize_availability(offer_node.get("availability"))
        if availability:
            result["availability"] = availability
        valid_until = _as_text(offer_node.get("priceValidUntil"))
        if valid_until:
            result["price_valid_until"] = valid_until

    return result


def _from_open_graph(soup: BeautifulSoup, page_url: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "title": None,
        "description": None,
        "detailed_description": None,
        "original_price": None,
        "currency": None,
        "image_urls": [],
        "confidence": {},
    }

    def meta(*keys: str) -> str | None:
        for key in keys:
            tag = soup.find("meta", attrs={"property": key}) or soup.find(
                "meta", attrs={"name": key}
            )
            if tag and tag.get("content"):
                value = str(tag["content"]).strip()
                if value:
                    return value
        return None

    title = meta("og:title", "twitter:title")
    if title:
        result["title"] = title
        result["confidence"]["title"] = "open_graph"

    description = meta("og:description", "twitter:description", "description")
    if description:
        result["description"] = _truncate(description, 500)
        result["detailed_description"] = description
        result["confidence"]["description"] = "open_graph"

    images: list[str] = []
    for tag in soup.find_all("meta", attrs={"property": re.compile(r"^og:image", re.I)}):
        content = (tag.get("content") or "").strip()
        if content:
            images.append(urljoin(page_url, content))
    twitter_image = meta("twitter:image", "twitter:image:src")
    if twitter_image:
        images.append(urljoin(page_url, twitter_image))
    images = _dedupe_urls(images)[:MAX_IMAGES]
    if images:
        result["image_urls"] = images
        result["confidence"]["images"] = "open_graph"

    price = _parse_price(
        meta(
            "product:price:amount",
            "og:price:amount",
            "twitter:data1",
        )
    )
    if price is not None:
        result["original_price"] = f"{price:.2f}"
        result["confidence"]["price"] = "open_graph"

    currency = meta("product:price:currency", "og:price:currency")
    if currency:
        result["currency"] = currency
        result["confidence"]["currency"] = "open_graph"

    return result


def _from_html_fallback(soup: BeautifulSoup) -> dict[str, Any]:
    result: dict[str, Any] = {
        "title": None,
        "description": None,
        "detailed_description": None,
        "original_price": None,
        "currency": None,
        "image_urls": [],
        "confidence": {},
    }
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
        if title:
            result["title"] = title
            result["confidence"]["title"] = "html"
    return result


def _merge_sources(
    source_url: str,
    json_ld: dict[str, Any],
    open_graph: dict[str, Any],
    fallback: dict[str, Any],
) -> dict[str, Any]:
    confidence: dict[str, str] = {}
    warnings: list[str] = []

    def pick(field: str) -> Any:
        for source_name, source in (
            ("json_ld", json_ld),
            ("open_graph", open_graph),
            ("html", fallback),
        ):
            value = source.get(field)
            if value in (None, "", [], {}):
                continue
            confidence[field] = source.get("confidence", {}).get(field, source_name)
            return value
        return None

    title = pick("title")
    description = pick("description")
    detailed = pick("detailed_description") or description
    original_price = pick("original_price")
    currency = pick("currency")
    list_price = pick("list_price")
    sale_price = pick("sale_price")
    availability = pick("availability")
    price_valid_until = pick("price_valid_until")

    image_urls: list[str] = []
    for source_name, source in (
        ("json_ld", json_ld),
        ("open_graph", open_graph),
    ):
        for url in source.get("image_urls") or []:
            if url not in image_urls:
                image_urls.append(url)
                confidence.setdefault("images", source_name)
        if image_urls:
            break
    image_urls = image_urls[:MAX_IMAGES]

    if not title:
        warnings.append("Title not found")
    if not description:
        warnings.append("Description not found")
    if not original_price:
        warnings.append("Price not found")
    if not image_urls:
        warnings.append("Images not found")
    elif len(image_urls) == 1:
        warnings.append("Only one image found")

    suggested_offer_type = "item" if original_price else "percentage_bill"

    return {
        "source_url": source_url,
        "title": title or "",
        "description": description or "",
        "detailed_description": detailed or "",
        "original_price": original_price,
        "list_price": list_price,
        "sale_price": sale_price,
        "currency": currency,
        "availability": availability,
        "price_valid_until": price_valid_until,
        "image_urls": image_urls,
        "external_url": source_url,
        "external_url_label": "View product",
        "suggested_offer_type": suggested_offer_type,
        "confidence": confidence,
        "warnings": warnings,
    }


def _flatten_json_ld(data: Any) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    if isinstance(data, list):
        for item in data:
            nodes.extend(_flatten_json_ld(item))
        return nodes
    if not isinstance(data, dict):
        return nodes
    if "@graph" in data and isinstance(data["@graph"], list):
        for item in data["@graph"]:
            nodes.extend(_flatten_json_ld(item))
    nodes.append(data)
    return nodes


def _is_type(node: dict[str, Any], type_name: str) -> bool:
    raw = node.get("@type")
    if isinstance(raw, list):
        types = [str(item).lower() for item in raw]
    elif raw is None:
        types = []
    else:
        types = [str(raw).lower()]
    needle = type_name.lower()
    return any(needle == t or t.endswith("/" + needle) or t.endswith(":" + needle) for t in types)


def _normalize_availability(value: Any) -> str | None:
    text = _as_text(value)
    if not text:
        return None
    lowered = text.strip().split("/")[-1].split(":")[-1]
    return lowered or None


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("@value") or value.get("name") or value.get("text")
    if isinstance(value, list):
        for item in value:
            text = _as_text(item)
            if text:
                return text
        return None
    text = str(value).strip()
    return text or None


def _normalize_images(value: Any, page_url: str) -> list[str]:
    urls: list[str] = []

    def add(item: Any) -> None:
        if isinstance(item, str):
            urls.append(urljoin(page_url, item.strip()))
        elif isinstance(item, dict):
            content = item.get("url") or item.get("contentUrl") or item.get("@id")
            if content:
                urls.append(urljoin(page_url, str(content).strip()))

    if isinstance(value, list):
        for item in value:
            add(item)
    else:
        add(value)
    return _dedupe_urls(urls)[:MAX_IMAGES]


def _dedupe_urls(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for url in urls:
        cleaned = (url or "").strip()
        if not cleaned or cleaned in seen:
            continue
        if not cleaned.startswith(("http://", "https://")):
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


def _parse_price(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            amount = Decimal(str(value))
        except InvalidOperation:
            return None
        return amount if amount > 0 else None

    text = str(value).strip()
    if not text:
        return None
    # Prefer the first number-like token; support EU formats 1.299,99 and 1299.99
    match = re.search(r"(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})|\d+(?:[.,]\d{2})?)", text)
    if not match:
        return None
    token = match.group(1)
    if "," in token and "." in token:
        if token.rfind(",") > token.rfind("."):
            token = token.replace(".", "").replace(",", ".")
        else:
            token = token.replace(",", "")
    elif "," in token:
        parts = token.split(",")
        token = token.replace(",", ".") if len(parts[-1]) <= 2 else token.replace(",", "")
    try:
        amount = Decimal(token)
    except InvalidOperation:
        return None
    return amount if amount > 0 else None


def _truncate(text: str, limit: int) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"
