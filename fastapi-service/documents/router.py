"""Stateless document rendering endpoint.

This is the single document-rendering surface for the whole platform: Django
orchestrates and owns the figures, this service turns a payload into bytes, and
nothing else renders documents. Keeping it stateless — bytes in the response,
no storage, no file URL — is what lets the caller treat the result as the
transient artifact it is.
"""

from typing import Any, Dict, Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from deps import require_internal_key

from .renderer import RenderError, render, supported_formats

router = APIRouter(prefix="/documents", tags=["documents"])

DocType = Literal["invoice", "receipt", "statement", "report"]
OutputFormat = Literal["pdf", "image"]


class RenderRequest(BaseModel):
    doc_type: DocType
    # Bills default to an image because customers read them inline in WhatsApp
    # without downloading anything; statements and reports are PDF-only, since
    # they are multi-page by nature.
    format: OutputFormat = "pdf"
    business_id: int
    payload: Dict[str, Any] = Field(default_factory=dict)


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"success": False, "error": {"code": code, "message": message}},
    )


@router.post("/render", dependencies=[Depends(require_internal_key)])
def render_document(body: RenderRequest) -> Response:
    """Renders a document and returns the raw bytes.

    Returns the file itself rather than JSON so the caller can stream it
    straight through to its destination without a base64 round-trip.
    """
    try:
        content, media_type, extension, actual_format = render(
            body.doc_type, body.format, body.payload
        )
    except RenderError as exc:
        raise _error(422, "RENDER_FAILED", str(exc))
    except Exception as exc:  # noqa: BLE001 - a bad payload must not surface as a bare 500
        raise _error(500, "RENDER_ERROR", f"Unexpected error rendering the document: {exc}")

    filename = f"{body.doc_type}_{body.business_id}.{extension}"
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # X-Document-Format is the format ACTUALLY produced, which may differ
            # from the one requested (a bill too long to read as an image comes
            # back as a PDF). Callers should record this value, not their request.
            "X-Document-Type": body.doc_type,
            "X-Document-Format": actual_format,
            "X-Document-Format-Requested": body.format,
        },
    )


@router.get("/formats", dependencies=[Depends(require_internal_key)])
def list_formats() -> dict:
    """Which output formats each document type supports.

    Lets Django (and the app behind it) offer only the choices that actually
    work, instead of discovering that a statement has no image format by
    getting a 422 after the owner has already tapped Send.
    """
    return {
        "success": True,
        "data": {
            doc_type: supported_formats(doc_type)
            for doc_type in ("invoice", "receipt", "statement", "report")
        },
    }
