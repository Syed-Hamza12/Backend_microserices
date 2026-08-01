from rest_framework.throttling import AnonRateThrottle


class AuthRateThrottle(AnonRateThrottle):
    """Stricter, IP-keyed throttle for login/register/Google-auth —
    separate from the general 'anon' scope so a brute-force attempt against
    auth endpoints can't hide behind a rate generous enough for normal
    unauthenticated browsing (e.g. a public health-check), and so tuning one
    doesn't accidentally loosen the other.
    """

    scope = "auth"
