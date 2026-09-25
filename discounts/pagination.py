"""Shared page/page_size helpers for list endpoints."""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from rest_framework.pagination import PageNumberPagination


class StandardResultsSetPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


def parse_page_params(request, *, default_page_size: int = 20) -> tuple[int, int]:
    try:
        page = max(int(request.query_params.get("page", 1)), 1)
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = min(
            max(int(request.query_params.get("page_size", default_page_size)), 1),
            100,
        )
    except (TypeError, ValueError):
        page_size = default_page_size
    return page, page_size


def slice_page(items: Sequence[Any], page: int, page_size: int) -> tuple[int, list[Any]]:
    total = len(items)
    start = (page - 1) * page_size
    return total, list(items[start : start + page_size])


def slice_queryset(queryset, page: int, page_size: int) -> tuple[int, list[Any]]:
    total = queryset.count()
    start = (page - 1) * page_size
    return total, list(queryset[start : start + page_size])


def page_payload(
    *,
    count: int,
    page: int,
    page_size: int,
    results: Iterable[Any],
    **extra: Any,
) -> dict[str, Any]:
    payload = {
        "count": count,
        "page": page,
        "page_size": page_size,
        "results": list(results),
    }
    payload.update(extra)
    return payload
