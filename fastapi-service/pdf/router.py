import os
import uuid
from pathlib import Path
from typing import Any, Dict, Literal

from fastapi import APIRouter, Depends, HTTPException
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel
from xhtml2pdf import pisa

from deps import require_internal_key

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = BASE_DIR / "templates"
STORAGE_DIR = Path(os.environ.get("PDF_STORAGE_DIR", str(BASE_DIR / "generated_pdfs")))
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

# autoescape is essential here, not cosmetic. Everything rendered into these
# templates — business name, customer name, item names, notes — is user- or
# OCR-supplied, and xhtml2pdf resolves markup and resource references while
# building the PDF. Without escaping, an item name containing HTML (say, an
# <img src="file:///..."> read off a photographed bill) is interpreted rather
# than printed, and the resulting document is then sent to a customer over
# WhatsApp.
env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=select_autoescape(["html", "xml"]))

router = APIRouter(prefix="/pdf", tags=["pdf (deprecated)"], deprecated=True)

# DEPRECATED — superseded by /documents/render.
#
# This endpoint writes each PDF into STORAGE_DIR and returns a URL to it, which
# conflicts with generated documents being transient artifacts: the files
# accumulate forever and every one of them needs a public URL to be useful.
# /documents/render returns the bytes instead, so nothing is stored and there is
# nothing to clean up. Kept mounted only so Django's existing PDF job keeps
# working until it is switched over; delete this module at that point.

TEMPLATE_FOR_DOC_TYPE = {
    "invoice": "invoice.html",
    "receipt": "receipt.html",
    "statement": "statement.html",
    "report": "report.html",
}


class GeneratePdfRequest(BaseModel):
    doc_type: Literal["invoice", "receipt", "statement", "report"]
    business_id: int
    payload: Dict[str, Any]


@router.post("/generate", dependencies=[Depends(require_internal_key)])
def generate_pdf(body: GeneratePdfRequest):
    template_name = TEMPLATE_FOR_DOC_TYPE[body.doc_type]
    template_path = TEMPLATE_DIR / template_name
    if not template_path.exists():
        raise HTTPException(
            status_code=501,
            detail={
                "success": False,
                "error": {
                    "code": "TEMPLATE_NOT_IMPLEMENTED",
                    "message": f"No template yet for doc_type={body.doc_type}.",
                },
            },
        )

    template = env.get_template(template_name)
    html = template.render(**body.payload)

    filename = f"{body.doc_type}_{body.business_id}_{uuid.uuid4().hex}.pdf"
    output_path = STORAGE_DIR / filename

    with open(output_path, "wb") as f:
        result = pisa.CreatePDF(html, dest=f)

    if result.err:
        raise HTTPException(
            status_code=500,
            detail={"success": False, "error": {"code": "PDF_RENDER_FAILED", "message": "Failed to render PDF."}},
        )

    return {"success": True, "data": {"file_url": f"/media/pdfs/{filename}"}}
