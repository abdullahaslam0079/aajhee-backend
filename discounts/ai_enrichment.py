"""Optional Gemini enrichment for admin product URL import drafts."""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)

AI_TIMEOUT_SECONDS = 20
MAX_PAGE_TEXT_CHARS = 6_000
MAX_TITLE_LEN = 120
MAX_DESCRIPTION_LEN = 500
MAX_DETAILED_DESCRIPTION_LEN = 4_000
MAX_DISCOUNT_COPY_LEN = 280

OFFER_TYPES = frozenset({"item", "percentage_bill"})


def is_ai_enrichment_configured() -> bool:
    return bool(getattr(settings, "GEMINI_API_KEY", "") or "")


def enrich_product_draft(
    draft: dict[str, Any],
    *,
    categories: list[str] | None = None,
    page_text: str = "",
) -> dict[str, Any]:
    """
    Fill missing draft fields and add discount/category suggestions via Gemini.

    Never overwrites non-empty scraped values. On any failure, returns the original
    draft with a warning so import does not hard-fail.
    """
    result = dict(draft)
    result.setdefault("confidence", {})
    result.setdefault("warnings", [])
    result.setdefault("suggested_category", None)
    result.setdefault("suggested_discount_percent", None)
    result.setdefault("suggested_discount_copy", "")
    result["ai_enriched"] = False

    if not is_ai_enrichment_configured():
        return result

    category_names = [c.strip() for c in (categories or []) if (c or "").strip()]
    try:
        payload = _call_gemini(result, category_names=category_names, page_text=page_text)
    except Exception:
        logger.exception("Gemini product enrichment failed")
        warnings = list(result.get("warnings") or [])
        warnings.append("ai_enrichment_failed")
        result["warnings"] = warnings
        return result

    if not isinstance(payload, dict):
        warnings = list(result.get("warnings") or [])
        warnings.append("ai_enrichment_failed")
        result["warnings"] = warnings
        return result

    return _merge_ai_into_draft(result, payload, category_names=category_names)


def _call_gemini(
    draft: dict[str, Any],
    *,
    category_names: list[str],
    page_text: str,
) -> dict[str, Any]:
    from google import genai
    from google.genai import types

    api_key = settings.GEMINI_API_KEY
    model = getattr(settings, "GEMINI_MODEL", "gemini-2.0-flash") or "gemini-2.0-flash"
    excerpt = _truncate((page_text or "").strip(), MAX_PAGE_TEXT_CHARS)

    system_instruction = (
        "You enrich local-discount offer drafts for Aajhee. "
        "Return ONLY valid JSON matching the schema. "
        "Prefer facts from the page text. Do not invent specific prices unless clearly present. "
        "If a field cannot be determined, use null or an empty string. "
        "suggested_category must be exactly one of the allowed category names, or null. "
        "suggested_offer_type must be item or percentage_bill. "
        "suggested_discount_percent is a number between 1 and 90 when a discount is implied, else null. "
        "suggested_discount_copy is a short marketing blurb for the offer (max ~200 chars)."
    )

    user_prompt = {
        "allowed_categories": category_names,
        "scraped_draft": {
            "source_url": draft.get("source_url"),
            "title": draft.get("title") or "",
            "description": draft.get("description") or "",
            "detailed_description": draft.get("detailed_description") or "",
            "original_price": draft.get("original_price"),
            "currency": draft.get("currency"),
            "suggested_offer_type": draft.get("suggested_offer_type"),
            "warnings": draft.get("warnings") or [],
        },
        "page_text_excerpt": excerpt,
    }

    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "nullable": True},
            "description": {"type": "string", "nullable": True},
            "detailed_description": {"type": "string", "nullable": True},
            "original_price": {"type": "string", "nullable": True},
            "currency": {"type": "string", "nullable": True},
            "suggested_category": {"type": "string", "nullable": True},
            "suggested_discount_percent": {"type": "number", "nullable": True},
            "suggested_discount_copy": {"type": "string", "nullable": True},
            "suggested_offer_type": {
                "type": "string",
                "nullable": True,
            },
        },
        "required": [
            "title",
            "description",
            "detailed_description",
            "original_price",
            "currency",
            "suggested_category",
            "suggested_discount_percent",
            "suggested_discount_copy",
            "suggested_offer_type",
        ],
    }

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model,
        contents=json.dumps(user_prompt, ensure_ascii=False),
        config=types.GenerateContentConfig(
            system_instruction=system_instruction,
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0.2,
            http_options=types.HttpOptions(timeout=AI_TIMEOUT_SECONDS * 1000),
        ),
    )

    text = (getattr(response, "text", None) or "").strip()
    if not text:
        raise ValueError("Empty Gemini response")
    return json.loads(text)


def _merge_ai_into_draft(
    draft: dict[str, Any],
    ai: dict[str, Any],
    *,
    category_names: list[str],
) -> dict[str, Any]:
    result = dict(draft)
    confidence = dict(result.get("confidence") or {})
    warnings = list(result.get("warnings") or [])
    filled_any = False

    def is_blank(value: Any) -> bool:
        return value in (None, "", [], {})

    # Never overwrite non-empty scraped core fields.
    if is_blank(result.get("title")):
        title = _clean_text(ai.get("title"), MAX_TITLE_LEN)
        if title:
            result["title"] = title
            confidence["title"] = "ai"
            filled_any = True
            _remove_warning(warnings, "Title not found")

    if is_blank(result.get("description")):
        description = _clean_text(ai.get("description"), MAX_DESCRIPTION_LEN)
        if description:
            result["description"] = description
            confidence["description"] = "ai"
            filled_any = True
            _remove_warning(warnings, "Description not found")

    if is_blank(result.get("detailed_description")):
        detailed = _clean_text(ai.get("detailed_description"), MAX_DETAILED_DESCRIPTION_LEN)
        if not detailed:
            detailed = result.get("description") or ""
        if detailed:
            result["detailed_description"] = detailed
            confidence["detailed_description"] = "ai"
            filled_any = True

    if is_blank(result.get("original_price")):
        price = _normalize_price(ai.get("original_price"))
        if price is not None:
            result["original_price"] = f"{price:.2f}"
            confidence["original_price"] = "ai"
            filled_any = True
            _remove_warning(warnings, "Price not found")

    if is_blank(result.get("currency")):
        currency = _clean_text(ai.get("currency"), 8)
        if currency:
            result["currency"] = currency.upper()
            confidence["currency"] = "ai"
            filled_any = True

    category = _match_category(ai.get("suggested_category"), category_names)
    if category:
        result["suggested_category"] = category
        confidence["suggested_category"] = "ai"
        filled_any = True
    else:
        result["suggested_category"] = None

    discount_percent = _normalize_discount_percent(ai.get("suggested_discount_percent"))
    if discount_percent is not None:
        result["suggested_discount_percent"] = f"{discount_percent:.2f}"
        confidence["suggested_discount_percent"] = "ai"
        filled_any = True
    else:
        result["suggested_discount_percent"] = None

    discount_copy = _clean_text(ai.get("suggested_discount_copy"), MAX_DISCOUNT_COPY_LEN)
    if discount_copy:
        result["suggested_discount_copy"] = discount_copy
        confidence["suggested_discount_copy"] = "ai"
        filled_any = True
    else:
        result["suggested_discount_copy"] = ""

    offer_type = ai.get("suggested_offer_type")
    if isinstance(offer_type, str) and offer_type in OFFER_TYPES:
        # Suggestion field — allow AI refinement even if scrape set a default.
        result["suggested_offer_type"] = offer_type
        confidence["suggested_offer_type"] = "ai"
        filled_any = True
    elif result.get("original_price") and result.get("suggested_offer_type") != "item":
        result["suggested_offer_type"] = "item"

    result["confidence"] = confidence
    result["warnings"] = warnings
    result["ai_enriched"] = filled_any
    return result


def _match_category(value: Any, category_names: list[str]) -> str | None:
    if not value or not category_names:
        return None
    text = str(value).strip()
    if not text:
        return None
    lower = text.lower()
    for name in category_names:
        if name.lower() == lower:
            return name
    for name in category_names:
        if lower in name.lower() or name.lower() in lower:
            return name
    return None


def _normalize_price(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            amount = Decimal(str(value))
        except InvalidOperation:
            return None
        return amount if amount > 0 else None
    text = str(value).strip()
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


def _normalize_discount_percent(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if amount < 1 or amount > 90:
        return None
    return amount.quantize(Decimal("0.01"))


def _clean_text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    if not text:
        return ""
    return _truncate(text, limit)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _remove_warning(warnings: list[str], message: str) -> None:
    while message in warnings:
        warnings.remove(message)
