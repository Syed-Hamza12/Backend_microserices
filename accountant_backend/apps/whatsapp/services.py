from apps.notifications.services import create_notification

from . import gateway_client
from .gateway_client import GatewayError


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
    gateway_client.disconnect_session(business.gateway_session_id)


def unlink(business):
    if not business.gateway_session_id:
        return
    gateway_client.unlink_session(business.gateway_session_id)
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
