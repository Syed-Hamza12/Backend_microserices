import json
import os

from google import genai
from google.genai import types

EXTRACTION_PROMPT = """
Read this photo of a receipt, invoice, or handwritten bill/challan. Extract the following as JSON:
{
  "date": "YYYY-MM-DD or null if unclear",
  "amount": number or null,
  "customer_name": "string or null - best guess at the customer/buyer name if visible",
  "line_items": [{"item_name": "string", "quantity": number, "rate": number}],
  "raw_text": "all text you can read from the image"
}
Return ONLY the JSON object, no other text.
""".strip()

_client = None


def _get_client():
    global _client
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured on this server yet.")
    if _client is None:
        _client = genai.Client(api_key=api_key)
    return _client


def extract_receipt_data(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict:
    """Same extraction contract as apps/image_info_extractor/gemini_client.py on the Django
    side. Not called by Django today (see Milestone 9 notes in docs/milestones.md) - kept as a
    fully working alternate path so a future local vision model can be swapped in here without
    touching Django's app code, same as the PDF-generation pattern."""
    client = _get_client()
    model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
    response = client.models.generate_content(
        model=model,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            EXTRACTION_PROMPT,
        ],
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    return json.loads(response.text)
