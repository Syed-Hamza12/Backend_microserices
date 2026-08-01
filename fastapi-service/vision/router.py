from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from deps import require_internal_key

from .gemini_client import extract_receipt_data

router = APIRouter(prefix="/vision", tags=["vision"])


@router.post("/extract", dependencies=[Depends(require_internal_key)])
async def vision_extract(business_id: int = Form(...), image: UploadFile = File(...)):
    image_bytes = await image.read()
    mime_type = image.content_type or "image/jpeg"

    try:
        data = extract_receipt_data(image_bytes, mime_type)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=501,
            detail={"success": False, "error": {"code": "NOT_CONFIGURED", "message": str(exc)}},
        )
    except Exception as exc:  # noqa: BLE001 - Gemini call/parse errors surface as a clean 502
        raise HTTPException(
            status_code=502,
            detail={"success": False, "error": {"code": "VISION_EXTRACT_FAILED", "message": str(exc)}},
        )

    return {"success": True, "data": data}
