import os
from typing import Optional

from fastapi import Header, HTTPException

INTERNAL_KEY = os.environ.get("FASTAPI_INTERNAL_KEY", "")


def require_internal_key(x_internal_key: Optional[str] = Header(default=None)):
    if not INTERNAL_KEY or x_internal_key != INTERNAL_KEY:
        raise HTTPException(
            status_code=401,
            detail={"success": False, "error": {"code": "UNAUTHORIZED", "message": "Missing or invalid X-Internal-Key."}},
        )
