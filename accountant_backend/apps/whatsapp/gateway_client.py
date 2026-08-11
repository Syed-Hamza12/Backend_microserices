import logging
from datetime import datetime, timedelta, timezone

import jwt
import requests
from django.conf import settings

logger = logging.getLogger(__name__)

GATEWAY_AUDIENCE = "whatsapp-gateway"
SESSION_CREATE_SCOPE = "session:create"

# Sending a document can involve the Gateway fetching the PDF and pacing the
# send behind its own anti-ban delay, so it needs more room than a status poll.
DEFAULT_TIMEOUT = 15
SEND_TIMEOUT = 45


class GatewayError(Exception):
    def __init__(self, status_code, code, message):
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _service_token(*, session_id=None, scope=None):
    """Mints a short-lived token scoped to ONE gateway session.

    `sub` is the session the token may act on, and the Gateway rejects any
    request whose session id doesn't match it. Previously every token was
    all-powerful: it authenticated "Django said so" but carried no statement
    about *which* session, so anything holding one could drive any business's
    WhatsApp number.
    """
    now = datetime.now(timezone.utc)
    payload = {
        "iss": "django-backend",
        "aud": GATEWAY_AUDIENCE,
        "iat": now,
        "exp": now + timedelta(seconds=60),
    }
    if session_id is not None:
        payload["sub"] = str(session_id)
    if scope is not None:
        payload["scope"] = scope
    return jwt.encode(payload, settings.WHATSAPP_GATEWAY_JWT_SECRET, algorithm="HS256")


def _headers(*, session_id=None, scope=None):
    return {"Authorization": f"Bearer {_service_token(session_id=session_id, scope=scope)}"}


def _request(method, path, *, session_id=None, scope=None, timeout=DEFAULT_TIMEOUT, headers_extra=None, **kwargs):
    url = f"{settings.WHATSAPP_GATEWAY_BASE_URL}{path}"
    headers = _headers(session_id=session_id, scope=scope)
    if headers_extra:
        headers.update(headers_extra)
    try:
        response = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
    except requests.RequestException as exc:
        # The Gateway being down/unreachable must surface as a clean, typed
        # failure the views already know how to render — not a raw 500. The
        # raw exception (hostnames, ports, connection internals) is logged
        # server-side only; GatewayError.message is what ends up stored on
        # DocumentDelivery.error_message and, from there, shown to the
        # business owner (and formerly, verbatim) — it must never carry
        # anything but a plain, safe sentence a shopkeeper can read.
        logger.warning("WhatsApp gateway request failed: %s %s -> %s", method, path, exc)
        raise GatewayError(503, "GATEWAY_UNREACHABLE", "WhatsApp gateway is not reachable.") from exc

    if response.status_code >= 400:
        try:
            error = response.json()["error"]
            code, message = error["code"], error["message"]
        except Exception:
            # The Gateway's error response wasn't the JSON shape expected —
            # e.g. an HTML error page from an unrelated service at a
            # misconfigured URL, or a proxy/firewall block page. That raw
            # body can contain anything (a real incident: it once carried
            # this business's own session id and a customer's phone number
            # in plaintext) and must never be stored or shown — only logged
            # for debugging.
            logger.warning(
                "WhatsApp gateway returned a non-JSON error (status=%s): %.500s",
                response.status_code, response.text,
            )
            code, message = "GATEWAY_ERROR", "WhatsApp gateway returned an unexpected error."
        raise GatewayError(response.status_code, code, message)
    return response


def create_session(display_name):
    return _request(
        "POST", "/sessions", scope=SESSION_CREATE_SCOPE, json={"displayName": display_name}
    ).json()["data"]


def connect_session(gateway_session_id):
    return _request("POST", f"/sessions/{gateway_session_id}/connect", session_id=gateway_session_id).json()["data"]


def get_status(gateway_session_id):
    return _request("GET", f"/sessions/{gateway_session_id}/status", session_id=gateway_session_id).json()["data"]


def get_qr_bytes(gateway_session_id):
    try:
        response = _request("GET", f"/sessions/{gateway_session_id}/qr", session_id=gateway_session_id)
    except GatewayError as exc:
        if exc.code == "QR_NOT_AVAILABLE":
            return None
        raise
    return response.content


def disconnect_session(gateway_session_id):
    return _request(
        "POST", f"/sessions/{gateway_session_id}/disconnect", session_id=gateway_session_id
    ).json()["data"]


def unlink_session(gateway_session_id):
    return _request("DELETE", f"/sessions/{gateway_session_id}", session_id=gateway_session_id).json()["data"]


def send_text(gateway_session_id, to, message):
    return _request(
        "POST",
        "/messages",
        session_id=gateway_session_id,
        timeout=SEND_TIMEOUT,
        json={"gatewaySessionId": gateway_session_id, "to": to, "message": message},
    ).json()["data"]


def send_media(gateway_session_id, to, *, kind, file_name, content, caption=None):
    """Uploads already-rendered bytes to the Gateway.

    The file travels in the request body rather than as a URL the Gateway
    fetches. That keeps generated documents transient — nothing is written to
    disk, nothing needs a public URL, and there is no temporary file to clean up
    afterwards. It also means the Gateway never makes an outbound fetch on our
    behalf, removing that request-forgery surface entirely.
    """
    params = {"gatewaySessionId": gateway_session_id, "to": to, "kind": kind, "fileName": file_name}
    if caption:
        params["caption"] = caption

    return _request(
        "POST",
        "/messages/media",
        session_id=gateway_session_id,
        timeout=SEND_TIMEOUT,
        params=params,
        data=content,
        headers_extra={"Content-Type": "application/octet-stream"},
    ).json()["data"]
