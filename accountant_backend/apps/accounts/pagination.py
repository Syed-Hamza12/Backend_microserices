"""Shared list pagination.

Response shape keeps `data` as a plain array and puts paging information in a
sibling `page` key:

    {"success": true, "data": [...], "page": {...}}

That is deliberate. Wrapping the rows in `{"results": [...]}` — DRF's default —
would break every existing client on the day it shipped, for a change that is
supposed to be invisible to them. A client that ignores `page` still works; it
just sees the first page.

Offset/limit rather than cursor: these are small per-business lists (one
shopkeeper's customers, one customer's ledger), always filtered by business and
ordered deterministically, so the cost and the stale-page risks that justify
cursor pagination don't apply here.
"""

DEFAULT_LIMIT = 200
MAX_LIMIT = 500


def _positive_int(value, default, maximum=None):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed < 0:
        return default
    if maximum is not None:
        return min(parsed, maximum)
    return parsed


def paginate(queryset, request, *, default_limit=DEFAULT_LIMIT, max_limit=MAX_LIMIT):
    """Slices `queryset` per the request's `limit`/`offset`.

    Returns `(items, page_meta)`. `total` comes from a COUNT on the same
    queryset, which is what lets a caller page through without guessing when to
    stop.
    """
    limit = _positive_int(request.query_params.get("limit"), default_limit, max_limit)
    offset = _positive_int(request.query_params.get("offset"), 0)
    limit = max(limit, 1)

    total = queryset.count()
    items = list(queryset[offset : offset + limit])

    return items, {
        "limit": limit,
        "offset": offset,
        "total": total,
        "has_more": offset + len(items) < total,
    }


def paginated_response(serializer_class, queryset, request, **serializer_kwargs):
    """`paginate` plus the standard envelope, for the common case."""
    items, page = paginate(queryset, request)
    return {
        "success": True,
        "data": serializer_class(items, many=True, **serializer_kwargs).data,
        "page": page,
    }
