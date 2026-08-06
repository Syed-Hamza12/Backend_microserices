import json
import logging

from django.conf import settings
from google.genai import types

from apps.integrations.google_genai_client import generate as _google_generate, model_ladder

logger = logging.getLogger(__name__)

# The "amount" rules below are load-bearing, not decoration. Asking for a bare
# "amount" made the model return the bill's *grand total* — which on a
# shopkeeper's ledger routinely folds in a carried-forward balance. The goods
# then no longer summed to that figure, services._parse_items' reconciliation
# check dropped every line item, and the owner got a draft with the wrong total
# and nothing itemised. Keep previous_balance and amount_received as explicit
# fields: the model needs somewhere to put those numbers, or it puts them in
# "amount".
EXTRACTION_PROMPT = """
Read this photo of a receipt, invoice, or handwritten bill/challan (Pakistani or Indian
shopkeeper ledgers are common — Urdu, Hindi and English handwriting all appear). Extract
it as JSON with exactly this shape:

{
  "date": "YYYY-MM-DD, the date written on the bill, or null if none is written",
  "customer_name": "string or null - the customer/buyer the bill is made out to",
  "line_items": [{"item_name": "string", "quantity": number, "rate": number}],
  "amount": number or null,
  "previous_balance": number or null,
  "amount_received": number or null,
  "raw_text": "every word and number you can read from the image"
}

Rules, in order of importance:

1. "amount" is ONLY the value of goods or services newly sold on THIS bill —
   the sum of the line items. It must NEVER include a carried-forward balance.
   A handwritten bill often ends with a grand total that adds an old balance to
   today's goods. Do not put that grand total in "amount".
   Example: rows "20000 | 20mm black | 1.25 | 25,000", "Balance (200,000) | 200,000",
   then "Total 225,000" means amount = 25000, previous_balance = 200000.
   225000 is the grand total and belongs in NEITHER field.

2. A row whose label is a balance, "baqaya", "purana", "pichla", "previous",
   "brought forward", "b/f", "udhar" or similar is NOT a line item. Put its value
   in "previous_balance" and leave it out of "line_items".

3. Money already paid — "received", "jama", "wasool", "cash paid", "advance" —
   goes in "amount_received", not in "amount".

4. quantity x rate must equal that row's own written total, and the line items
   must add up to "amount" exactly. If a written total and your arithmetic
   disagree, trust the numbers written in the quantity and rate columns and
   recompute. A rate may be fractional (1.25), a quantity may be in thousands
   (20000) — do not round either.

5. Read digits carefully: South Asian handwriting often groups as 2,00,000
   (two lakh = 200000), and a trailing "/-" is not a digit. Never invent a
   value you cannot actually see — use null instead.

6. Every number must be a plain JSON number: no commas, no currency symbol.

Return ONLY the JSON object, no other text.
""".strip()


def _generate(contents, *, config=None, models=None):
    """Runs `contents` through the model/key ladder, returning the raw response.

    Key rotation and model-fallback mechanics live in
    apps.integrations.google_genai_client (the one canonical Google API
    client this codebase uses — apps.chat.google_client's fast-tier chat
    planner shares this same underlying implementation). This function
    keeps only what's specific to vision extraction: which keys/models to
    use and the vision-ladder default.
    """
    return _google_generate(
        settings.GEMINI_API_KEYS,
        models or model_ladder(settings.GEMINI_MODEL, settings.GEMINI_FALLBACK_MODELS),
        contents,
        config=config,
        logger=logger,
    )


def extract_receipt_data(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict:
    """One place for the Gemini Vision call — model name/config lives here only.
    Raises RuntimeError if no GEMINI_API_KEY is configured; callers must handle that gracefully
    (see services.handle_image_extract_job's fallback-apology path).

    Free-tier key rotation: settings.GEMINI_API_KEYS is one or more API keys
    (GEMINI_API_KEY plus GEMINI_API_KEY_1.._20 in .env). Always starts from
    key 0 on every call — see apps.chat.groq_client.call_groq's docstring
    for why (a reset key's quota comes back into use automatically, on the
    very next call, with no separate "has it reset yet" tracking needed)."""
    response = _generate(
        [
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            EXTRACTION_PROMPT,
        ],
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    return json.loads(response.text)


def generate_text(instructions: str, text: str) -> str:
    """A plain text-in/text-out call, for transliteration.

    Lives here rather than in apps.chat so both directions of the Urdu
    round trip share this module's key rotation and model fallback.

    Uses the *text* model ladder, not the vision one: this is transliteration,
    where the worst outcome is an awkward-sounding word, whereas the vision
    ladder reads money off a photograph and must stay on high-accuracy models.
    """
    response = _generate(
        [instructions + "\n\n" + text],
        models=model_ladder(settings.GEMINI_TEXT_MODEL, settings.GEMINI_TEXT_FALLBACK_MODELS),
    )
    return (response.text or "").strip()
