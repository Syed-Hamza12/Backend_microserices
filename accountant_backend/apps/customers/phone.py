"""Normalizes a customer's phone number to the international format WhatsApp
(and Baileys, the unofficial client this product's Gateway is built on)
actually requires to resolve a recipient.

Nothing anywhere previously normalized this — a number saved as typed
("03339233158", Pakistan's local dialing format: a leading 0 in place of the
country code) went straight into the ledger, straight onto an invoice, and
straight into every WhatsApp send attempt. Baileys doesn't reject a
malformed JID with a clean error; it just never finds a matching WhatsApp
user for it ("USync fetch yielded no results"), so the send hangs until
Django's own timeout and is recorded as GATEWAY_UNREACHABLE — a message
that was never going anywhere, from the very first attempt, misreported as
a connectivity problem every single time.
"""

import re

#: Pakistan only, matching this product's currency/market — see
#: Business.currency_code. A local number is 11 digits starting with 0
#: (e.g. 03339233158); the WhatsApp-ready form drops the 0 and prefixes 92.
_PK_LOCAL = re.compile(r"^0(\d{10})$")
_PK_COUNTRY_CODE = "92"


def normalize_phone(raw):
    """Best-effort normalization — never raises. Returns `raw` unchanged
    (stripped of spaces/dashes only) for anything it doesn't recognize
    confidently, rather than guessing and silently corrupting a number that
    was already correct (e.g. already-international, or a genuinely
    different country's format).
    """
    if not raw:
        return raw
    digits_only = re.sub(r"[\s\-()]", "", str(raw))
    digits_only = digits_only.removeprefix("+")

    local_match = _PK_LOCAL.match(digits_only)
    if local_match:
        return _PK_COUNTRY_CODE + local_match.group(1)

    return digits_only
