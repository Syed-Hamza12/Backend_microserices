import logging

from apps.notifications.services import create_notification

from . import gateway_client
from .gateway_client import GatewayError

logger = logging.getLogger(__name__)


def _ensure_session_id(business):
    if not business.gateway_session_id:
        session = gateway_client.create_session(business.business_name)
        business.gateway_session_id = session["id"]
        business.save(update_fields=["gateway_session_id"])
    return business.gateway_session_id


def connect(business):
    """First-ever connect creates a Gateway session; every call after that reuses the stored
    gateway_session_id, so a reconnect after disconnect skips the QR scan (per the Gateway's own
    reconnect behavior)."""
    session_id = _ensure_session_id(business)
    try:
        gateway_client.connect_session(session_id)
    except GatewayError as exc:
        if exc.code != "SESSION_NOT_FOUND":
            raise
        # The Gateway has no record of this session id at all — its sessions
        # store was wiped or never actually persisted this one (e.g. the
        # Gateway process crashed on startup before this session's directory
        # was written), while Business.gateway_session_id on our side still
        # points at it. Retrying the same id forever just 404s forever ("Not
        # Found: /api/whatsapp/connect/" on every tap) with no way for the
        # owner to recover except an unlink they'd have no reason to know to
        # do. Since the old id is provably dead on the Gateway already,
        # dropping it and creating a fresh session is equivalent to what
        # unlink+connect would achieve, and self-heals in place.
        logger.warning(
            "gateway session %s not found on gateway for business %s; creating a fresh one",
            session_id, business.id,
        )
        business.gateway_session_id = None
        business.save(update_fields=["gateway_session_id"])
        session_id = _ensure_session_id(business)
        gateway_client.connect_session(session_id)
    return {"id": session_id}


def get_status(business):
    if not business.gateway_session_id:
        return {"status": "NOT_CONNECTED", "phone": None, "needs_manual_reconnect": False, "message": None}
    try:
        data = gateway_client.get_status(business.gateway_session_id)
    except GatewayError as exc:
        if exc.code == "SESSION_NOT_FOUND":
            return {"status": "NOT_CONNECTED", "phone": None, "needs_manual_reconnect": False, "message": None}
        raise
    # `needsManualReconnect`/`lastError` tell the app the difference between
    # "still working on it" and "this has stopped and tapping Connect is the
    # only way forward". Without them the phone would keep polling a session
    # the Gateway has already parked, which is what the poll cap was papering
    # over.
    return {
        "status": data["status"],
        "phone": data.get("phone"),
        "needs_manual_reconnect": data.get("needsManualReconnect", False),
        "message": data.get("lastError"),
    }


def get_qr_bytes(business):
    if not business.gateway_session_id:
        return None
    return gateway_client.get_qr_bytes(business.gateway_session_id)


def disconnect(business):
    if not business.gateway_session_id:
        return
    try:
        gateway_client.disconnect_session(business.gateway_session_id)
    except GatewayError as exc:
        if exc.code != "SESSION_NOT_FOUND":
            raise
        # Already gone on the Gateway's side — same end state disconnect was
        # asked to reach, so this is a no-op success, not a failure.


def unlink(business):
    if not business.gateway_session_id:
        return
    try:
        gateway_client.unlink_session(business.gateway_session_id)
    except GatewayError as exc:
        if exc.code != "SESSION_NOT_FOUND":
            raise
        # Same reasoning as disconnect() above: a session the Gateway has no
        # record of is already unlinked as far as the owner is concerned —
        # clear the stale id below instead of surfacing a 404 for an unlink
        # that, functionally, already succeeded.
    business.gateway_session_id = None
    business.save(update_fields=["gateway_session_id"])


def _handle_send_failure(business, exc: GatewayError):
    if exc.code in ("RATE_LIMIT_EXCEEDED", "SESSION_NOT_CONNECTED"):
        create_notification(business, "whatsapp_disconnected", payload={"code": exc.code, "message": exc.message})


def send_text(business, to, message):
    """Records a whatsapp_disconnected Notification on RATE_LIMIT_EXCEEDED/SESSION_NOT_CONNECTED
    instead of silently dropping the send (backend_workflow.md Section 8) — re-raises so the
    caller/view can still surface the failure to the request that triggered it."""
    if not business.gateway_session_id:
        exc = GatewayError(409, "SESSION_NOT_CONNECTED", "WhatsApp is not connected for this business.")
        _handle_send_failure(business, exc)
        raise exc
    try:
        return gateway_client.send_text(business.gateway_session_id, to, message)
    except GatewayError as exc:
        _handle_send_failure(business, exc)
        raise


# send_document() was removed: documents are rendered on demand and their bytes
# streamed to the Gateway by apps.documents.delivery, so there is no stored file
# and no URL to send.
