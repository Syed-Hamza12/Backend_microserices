"""Resolving the accounting date a business owner meant.

Two rules govern everything here:

1. **The business timezone decides what "today" is.** Not the server's clock and
   not the phone's. `settings.TIME_ZONE` is Asia/Karachi, so a sale entered at
   1am belongs to that day, not the previous one.

2. **The server resolves relative dates, never the model.** The AI is given
   today's business date and asked for an absolute `YYYY-MM-DD`, but it may
   still answer "yesterday" — or answer with arithmetic it got wrong. Anything
   relative is resolved here against the business calendar, so a mistake in the
   model cannot put a sale on the wrong day of the ledger.

`ActivityEntry.timestamp` stores the business date; `created_at` stays the
system clock for auditing. The two are never mixed.
"""

import re
from datetime import date, datetime, timedelta

from django.utils import timezone

# How far a date may be set in either direction. Generous enough for catching up
# on paperwork or scheduling next month's billing, bounded enough that a typo'd
# year can't silently land an entry in 2019 and rewrite every balance after it —
# or in 2027, where it would never mature and never be seen again.
MAX_BACKDATE_DAYS = 365
MAX_FORWARD_DAYS = 365

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


class BusinessDateError(ValueError):
    """The requested date can't be used, with a message safe to show the owner."""


class AmbiguousBusinessDateError(BusinessDateError):
    """The phrase could mean two different dates, so it must not be resolved.

    Raised for words like "kal", which in Urdu means both *yesterday* and
    *tomorrow*. Now that entries can be dated forward as well as back, guessing a
    direction is a coin flip that lands money on the wrong side of today — so the
    owner is asked instead.
    """


def business_today():
    """Today, in the business's timezone."""
    return timezone.localdate()


def _resolve_weekday(token, today):
    """`last monday` / `monday` -> the most recent matching past date."""
    match = re.fullmatch(r"(?:last\s+|this\s+|past\s+)?(" + "|".join(WEEKDAYS) + r")", token)
    if not match:
        return None
    target = WEEKDAYS[match.group(1)]
    # Always the most recent occurrence at or before today, never a future one:
    # "last Monday" said on a Monday means today, not seven days ago.
    delta = (today.weekday() - target) % 7
    if token.startswith("last") and delta == 0:
        delta = 7
    return today - timedelta(days=delta)


def resolve(value, *, today=None, allow_future=True):
    """Turns a model- or client-supplied date into a real business date.

    Accepts a `date`, a `datetime`, an absolute `YYYY-MM-DD` string, or a
    relative phrase ("today", "yesterday", "2 days ago", "last monday").
    Returns None when `value` is empty, meaning "use today".

    Raises [BusinessDateError] for anything unusable, rather than guessing.
    """
    if value is None or value == "":
        return None

    today = today or business_today()

    if isinstance(value, datetime):
        # An aware datetime is a point in time; convert it to the business day
        # it falls on rather than the UTC day.
        resolved = timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
        return _validate(resolved, today, allow_future)

    if isinstance(value, date):
        return _validate(value, today, allow_future)

    token = str(value).strip().lower()

    if token in ("today", "aaj"):
        return today
    if token == "yesterday":
        return _validate(today - timedelta(days=1), today, allow_future)
    if token == "tomorrow":
        return _validate(today + timedelta(days=1), today, allow_future)
    if token == "kal":
        yesterday = today - timedelta(days=1)
        tomorrow = today + timedelta(days=1)
        raise AmbiguousBusinessDateError(
            f"\"Kal\" could mean {yesterday:%d %b} (yesterday) or {tomorrow:%d %b} (tomorrow) — "
            f"which one do you mean?"
        )

    days_ago = re.fullmatch(r"(\d{1,3})\s+days?\s+ago", token)
    if days_ago:
        return _validate(today - timedelta(days=int(days_ago.group(1))), today, allow_future)

    weekday = _resolve_weekday(token, today)
    if weekday is not None:
        return _validate(weekday, today, allow_future)

    try:
        parsed = date.fromisoformat(token)
    except ValueError:
        raise BusinessDateError(f"'{value}' isn't a date I can use. Try a date like 2026-07-25.")
    return _validate(parsed, today, allow_future)


def _validate(value, today, allow_future):
    if not allow_future and value > today:
        raise BusinessDateError("That date is in the future — an entry can't be dated ahead of today.")
    if value < today - timedelta(days=MAX_BACKDATE_DAYS):
        raise BusinessDateError(
            f"That date is more than {MAX_BACKDATE_DAYS} days ago. Please check the year."
        )
    if value > today + timedelta(days=MAX_FORWARD_DAYS):
        # Almost always a mistyped year. Left unchecked it would sit in the
        # ledger un-matured and invisible for years.
        raise BusinessDateError(
            f"That date is more than {MAX_FORWARD_DAYS} days ahead. Please check the year."
        )
    return value


def is_future(value, *, today=None):
    """True when this date has not arrived yet in the business's timezone."""
    if value is None:
        return False
    return value > (today or business_today())


def to_entry_timestamp(business_date, *, now=None):
    """Combines a business date with the current time of day.

    Entries are ordered by `(timestamp, id)`, so the time component decides the
    order of several entries recorded for the same day. Using the current clock
    time means three sales backdated in one sitting keep the order they were
    entered, instead of all landing at midnight and being ordered by insertion
    id alone.
    """
    now = now or timezone.localtime()
    if business_date is None:
        return now
    if business_date == now.date():
        return now
    naive = datetime.combine(business_date, now.timetz())
    return naive if timezone.is_aware(naive) else timezone.make_aware(naive)
