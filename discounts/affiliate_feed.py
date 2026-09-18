"""Parse affiliate CSV/XML product feeds into discovered deal rows."""

from __future__ import annotations

import csv
import gzip
import io
import zipfile
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, BinaryIO
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from xml.etree.ElementTree import ParseError

from .listing_discover import canonicalize_url
from .product_import import (
    USER_AGENT,
    ProductImportError,
    _blocked_message,
    validate_public_http_url,
)

ID_KEYS = ("id", "product_id", "merchant_product_id", "aw_product_id", "gtin", "sku", "ean")
TITLE_KEYS = ("title", "name", "product_name", "productname", "aw_product_name")
DESCRIPTION_KEYS = (
    "description",
    "product_short_description",
    "desc",
    "product_description",
)
PRICE_KEYS = ("rrp_price", "rrp", "was_price", "retail_price", "list_price", "price")
SALE_KEYS = (
    "sale_price",
    "search_price",
    "store_price",
    "discounted_price",
    "special_price",
    "display_price",
    "aw_price",
    "aw_search_price",
)
LINK_KEYS = (
    "aw_deep_link",
    "merchant_deep_link",
    "link",
    "url",
    "product_url",
    "destination_url",
)
IMAGE_KEYS = (
    "aw_image_url",
    "merchant_image_url",
    "image",
    "image_url",
    "product_image",
    "large_image",
    "image_large",
)
AVAIL_KEYS = ("availability", "stock_status", "in_stock", "stock")
END_KEYS = ("end_date", "valid_to", "price_valid_until", "expires", "expiry_date")

FEED_TIMEOUT_SECONDS = 60
MAX_FEED_BYTES = 20_000_000
ROW_SCAN_MULTIPLIER = 50


@dataclass
class DiscoveredDeal:
    source_key: str
    source_url: str
    title: str = ""
    description: str = ""
    detailed_description: str = ""
    original_price: str | None = None
    discounted_price: str | None = None
    image_urls: list[str] = field(default_factory=list)
    availability: str | None = None
    price_valid_until: str | None = None
    draft: dict[str, Any] | None = None


def parse_affiliate_feed(url: str, *, limit: int = 80) -> list[DiscoveredDeal]:
    validated = validate_public_http_url(url)
    return _collect_deals(_iter_feed_rows(validated), limit=max(1, limit))


def _collect_deals(rows: Iterator[dict[str, str]], *, limit: int) -> list[DiscoveredDeal]:
    scan_cap = max(limit * ROW_SCAN_MULTIPLIER, limit)
    discounted: list[DiscoveredDeal] = []
    others: list[DiscoveredDeal] = []
    seen: set[str] = set()
    scanned = 0
    for row in rows:
        scanned += 1
        deal = _row_to_deal(row)
        if deal is None or deal.source_key in seen:
            if scanned >= scan_cap:
                break
            continue
        seen.add(deal.source_key)
        if deal.discounted_price:
            discounted.append(deal)
        else:
            others.append(deal)
        if len(discounted) >= limit or scanned >= scan_cap:
            break
    chosen = (discounted + others)[:limit]
    if not chosen:
        raise ProductImportError(
            "unsupported_page",
            "Could not parse product rows from this affiliate feed.",
        )
    return chosen


def _iter_feed_rows(url: str) -> Iterator[dict[str, str]]:
    stream = _open_feed_binary(url)
    try:
        yield from _rows_from_binary(stream)
    finally:
        stream.close()


def _open_feed_binary(url: str) -> BinaryIO:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/gzip, application/zip, text/csv, application/xml, */*",
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
        },
        method="GET",
    )
    try:
        response = urlopen(request, timeout=FEED_TIMEOUT_SECONDS)
    except HTTPError as exc:
        code = int(exc.code or 0)
        if code in {401, 403, 429}:
            raise ProductImportError(
                "fetch_blocked",
                _blocked_message(url, code),
                http_status=code,
            ) from exc
        raise ProductImportError(
            "fetch_failed",
            f"Failed to fetch feed (HTTP {code}).",
            http_status=code,
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ProductImportError("fetch_failed", "Failed to fetch affiliate feed.") from exc

    budgeted = _ByteBudget(response, MAX_FEED_BYTES)
    peek = budgeted.read(4)
    inner: BinaryIO
    if peek.startswith(b"\x1f\x8b"):
        inner = gzip.GzipFile(fileobj=_PrefixedStream(peek, budgeted))  # type: ignore[assignment]
    elif peek.startswith(b"PK"):
        inner = _unzip_first_file(peek + budgeted.read())
        response.close()
        return inner
    else:
        inner = _PrefixedStream(peek, budgeted)
    return _ClosingStream(inner, response)


def _unzip_first_file(data: bytes) -> BinaryIO:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ProductImportError("unsupported_page", "Affiliate feed ZIP is invalid.") from exc
    name = next((item for item in archive.namelist() if not item.endswith("/")), "")
    if not name:
        archive.close()
        raise ProductImportError("unsupported_page", "Affiliate feed ZIP is empty.")
    handle = archive.open(name)
    return _ClosingStream(handle, archive)


def _rows_from_binary(stream: BinaryIO) -> Iterator[dict[str, str]]:
    buffered = stream if isinstance(stream, io.BufferedIOBase) else io.BufferedReader(stream)
    text = io.TextIOWrapper(buffered, encoding="utf-8", errors="replace", newline="")
    sample = text.read(4096)
    if not sample.strip():
        return
    if sample.lstrip().startswith("<"):
        body = sample + text.read(MAX_FEED_BYTES)
        yield from _from_xml(body)
        return
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(_iter_csv_lines(sample, text), dialect=dialect)
    for raw in reader:
        if not isinstance(raw, dict):
            continue
        yield {str(key or "").strip(): str(value or "").strip() for key, value in raw.items()}


def _iter_csv_lines(head: str, rest: io.TextIOBase) -> Iterator[str]:
    leftover = head
    while True:
        newline_at = leftover.find("\n")
        if newline_at == -1:
            more = rest.read(65_536)
            if not more:
                if leftover:
                    yield leftover
                return
            leftover += more
            continue
        yield leftover[: newline_at + 1]
        leftover = leftover[newline_at + 1 :]


def _from_csv(text: str) -> list[dict[str, str]]:
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    rows: list[dict[str, str]] = []
    for raw in reader:
        if not isinstance(raw, dict):
            continue
        rows.append({str(key or "").strip(): str(value or "").strip() for key, value in raw.items()})
    return rows


def _from_xml(text: str) -> list[dict[str, str]]:
    try:
        root = ET.fromstring(text)
    except ParseError as exc:
        raise ProductImportError("unsupported_page", "Affiliate feed XML is invalid.") from exc
    rows: list[dict[str, str]] = []
    candidates = list(root.findall(".//product")) + list(root.findall(".//item"))
    if not candidates:
        candidates = [child for child in list(root) if len(list(child))]
    for node in candidates:
        row: dict[str, str] = {}
        for child in list(node):
            tag = _local_name(child.tag)
            value = (child.text or "").strip()
            if not value:
                value = (child.get("url") or child.get("href") or "").strip()
            if tag and value and tag not in row:
                row[tag] = value
        if row:
            rows.append(row)
    return rows


def _row_to_deal(row: dict[str, str]) -> DiscoveredDeal | None:
    lookup = {key.lower().replace(" ", "_"): value for key, value in row.items() if key}
    product_id = _first(lookup, ID_KEYS)
    link = _first(lookup, LINK_KEYS)
    title = _first(lookup, TITLE_KEYS)
    if not link and not product_id:
        return None
    source_url = (link or "").strip()
    source_key = product_id or canonicalize_url(source_url)
    if not source_key:
        return None
    if product_id:
        source_key = f"feed:{product_id}"

    sale = _parse_decimal(_first(lookup, SALE_KEYS))
    price = _parse_decimal(_first(lookup, PRICE_KEYS))
    original = None
    discounted = None
    if price is not None and sale is not None and sale < price:
        original = f"{price:.2f}"
        discounted = f"{sale:.2f}"
    elif sale is not None:
        original = f"{sale:.2f}"
    elif price is not None:
        original = f"{price:.2f}"

    image = _first(lookup, IMAGE_KEYS)
    description = _first(lookup, DESCRIPTION_KEYS)
    availability = _first(lookup, AVAIL_KEYS)
    end_date = _first(lookup, END_KEYS)
    draft = {
        "source_url": source_url,
        "title": title,
        "description": description,
        "detailed_description": description,
        "original_price": original,
        "list_price": original if discounted else None,
        "sale_price": discounted or original,
        "image_urls": [image] if image else [],
        "external_url": source_url,
        "availability": availability or None,
        "price_valid_until": end_date or None,
        "suggested_offer_type": "item" if original else "percentage_bill",
        "ai_enriched": False,
        "warnings": [],
    }
    return DiscoveredDeal(
        source_key=source_key[:500],
        source_url=source_url,
        title=title,
        description=description,
        detailed_description=description,
        original_price=original,
        discounted_price=discounted,
        image_urls=[image] if image else [],
        availability=availability or None,
        price_valid_until=end_date or None,
        draft=draft,
    )


def _first(lookup: dict[str, str], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = lookup.get(key)
        if value:
            return value
    return ""


def _parse_decimal(value: str) -> Decimal | None:
    text = (value or "").strip().replace("€", "").replace("EUR", "").strip()
    if not text:
        return None
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        parts = text.split(",")
        text = text.replace(",", ".") if len(parts[-1]) <= 2 else text.replace(",", "")
    try:
        amount = Decimal(text)
    except Exception:
        return None
    return amount if amount > 0 else None


def _local_name(tag: str) -> str:
    if "}" in tag:
        tag = tag.rsplit("}", 1)[-1]
    return tag.lower().replace("-", "_")


class _ByteBudget:
    def __init__(self, fh: BinaryIO, max_bytes: int):
        self._fh = fh
        self._max = max_bytes
        self._n = 0

    def read(self, size: int = -1) -> bytes:
        if self._n >= self._max:
            raise ProductImportError("fetch_failed", "Affiliate feed is too large to import.")
        remaining = self._max - self._n
        chunk = self._fh.read(remaining if size < 0 else min(size, remaining))
        self._n += len(chunk)
        if size < 0 and self._n >= self._max:
            extra = self._fh.read(1)
            if extra:
                raise ProductImportError("fetch_failed", "Affiliate feed is too large to import.")
        return chunk

    def close(self) -> None:
        close = getattr(self._fh, "close", None)
        if close:
            close()


class _PrefixedStream(io.RawIOBase):
    def __init__(self, prefix: bytes, rest: BinaryIO):
        self._first = io.BytesIO(prefix)
        self._rest = rest

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        head = self._first.read() if size < 0 else self._first.read(size)
        if size < 0:
            return head + self._rest.read()
        if len(head) >= size:
            return head
        return head + self._rest.read(size - len(head))

    def readinto(self, b) -> int:
        data = self.read(len(b))
        n = len(data)
        b[:n] = data
        return n

    def close(self) -> None:
        close = getattr(self._rest, "close", None)
        if close:
            close()
        super().close()


class _ClosingStream(io.RawIOBase):
    def __init__(self, inner: BinaryIO, *owned: Any):
        self._inner = inner
        self._owned = owned

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        return self._inner.read() if size < 0 else self._inner.read(size)

    def readinto(self, b) -> int:
        readinto = getattr(self._inner, "readinto", None)
        if readinto:
            return int(readinto(b))
        data = self.read(len(b))
        n = len(data)
        b[:n] = data
        return n

    def close(self) -> None:
        for handle in (self._inner, *self._owned):
            closer = getattr(handle, "close", None)
            if closer:
                try:
                    closer()
                except Exception:
                    pass
        super().close()
