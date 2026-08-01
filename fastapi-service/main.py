from fastapi import Depends, FastAPI
from dotenv import load_dotenv

load_dotenv()

from deps import require_internal_key  # noqa: E402 - must load .env first
from documents.router import router as documents_router  # noqa: E402
from pdf.router import router as pdf_router  # noqa: E402
from vision.router import router as vision_router  # noqa: E402

app = FastAPI(title="Accountant FastAPI Service")
# /documents/render is the current rendering surface: stateless, returns bytes.
app.include_router(documents_router)
# /pdf/generate is DEPRECATED — it writes PDFs to disk and returns a URL, which
# conflicts with generated documents being transient. Still mounted so Django's
# existing job path keeps working until it is switched over, then removed.
app.include_router(pdf_router)
app.include_router(vision_router)


@app.get("/health")
def health():
    return {"success": True, "data": {"status": "ok"}}


@app.get("/internal/ping", dependencies=[Depends(require_internal_key)])
def internal_ping():
    return {"success": True, "data": {"pong": True}}
