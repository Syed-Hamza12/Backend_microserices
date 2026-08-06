"""Bounded, capability-scoped retry — deliberately not a general resilience
layer. Every use of this is named at its call site and justified there
because retrying is not always safe: retrying a WhatsApp send too eagerly is
exactly the kind of thing that increases connection/send frequency the
`whatsapp-gateway-ban-risk` memory warns about (a prior reconnect bug already
put the linked number at risk). Callers pass a narrow `retryable` exception
set and a small `max_attempts` per use, not a blanket policy.
"""

import logging
import time

logger = logging.getLogger(__name__)


def with_retries(fn, *, max_attempts=2, retryable=(Exception,), delay_seconds=1, should_retry=None):
    """Calls `fn()`, retrying up to `max_attempts` total attempts when it
    raises one of `retryable`. `should_retry(exc) -> bool`, if given, is an
    extra filter on top of the exception type — used where only some
    instances of an exception type are transient (e.g. only one specific
    `GatewayError` code).

    Re-raises the last exception if every attempt fails. Never swallows a
    non-retryable exception's first occurrence.
    """
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except retryable as exc:  # noqa: BLE001 - narrowed by the caller's `retryable` tuple
            if should_retry is not None and not should_retry(exc):
                raise
            last_exc = exc
            logger.warning("with_retries: attempt %s/%s failed: %s", attempt, max_attempts, exc)
            if attempt < max_attempts:
                time.sleep(delay_seconds)
    raise last_exc
