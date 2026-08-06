from rest_framework.response import Response
from rest_framework.views import exception_handler


def custom_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is None:
        return None

    detail = response.data
    if isinstance(detail, dict) and "detail" in detail:
        message = str(detail["detail"])
    elif isinstance(detail, dict):
        message = "; ".join(f"{k}: {v}" for k, v in detail.items())
    else:
        message = str(detail)

    code = getattr(exc, "error_code", None) or exc.__class__.__name__.upper()

    response.data = {
        "success": False,
        "error": {
            "code": code,
            "message": message,
        },
    }
    return response
