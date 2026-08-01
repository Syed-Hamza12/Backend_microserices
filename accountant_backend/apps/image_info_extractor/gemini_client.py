import json
import logging

from django.conf import settings
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

_clients = {}

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


def _client_for(key):
    if key not in _clients:
        _clients[key] = genai.Client(api_key=key)
    return _clients[key]


def _models(primary, fallbacks):
    """The model to try first, then the fallbacks, de-duplicated.

    Rotating *keys* is the wrong remedy for the most common real failure:
    `503 UNAVAILABLE - "This model is currently experiencing high demand"` is
    the model being busy, not the key being out of quota, so every key fails
    identically and the owner just sees "Sorry, I couldn't process that".
    Falling back to a different model is what actually recovers.
    """
    seen, ordered = set(), []
    for name in [primary, *fallbacks]:
        if name and name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def _generate(contents, *, config=None, models=None):
    """Runs `contents` through the model/key ladder, returning the raw response.

    Ordered model-first: a busy model is retried on a different model before
    burning through the other keys, since a 503 has nothing to do with the key.
    """
    keys = settings.GEMINI_API_KEYS
    if not keys:
        raise RuntimeError("No GEMINI_API_KEY configured on this server yet.")

    last_error = None
    for model in models or _models(settings.GEMINI_MODEL, settings.GEMINI_FALLBACK_MODELS):
        for idx, key in enumerate(keys):
            try:
                response = _client_for(key).models.generate_content(
                    model=model, contents=contents, config=config
                )
                logger.info("gemini call ok key_index=%s model=%s", idx, model)
                return response
            except Exception as exc:  # noqa: BLE001 - any failure tries the next key/model
                last_error = exc
                logger.warning("gemini call failed key_index=%s model=%s error=%s", idx, model, exc)

    raise last_error


def extract_receipt_data(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict:
    """One place for the Gemini Vision call — model name/config lives here only.
    Raises RuntimeError if no GEMINI_API_KEY is configured; callers must handle that gracefully
    (see services.handle_image_extract_job's fallback-apology path).

    Free-tier key rotation: settings.GEMINI_API_KEYS is one or more API keys
    (GEMINI_API_KEY plus GEMINI_API_KEY_1.._10 in .env). Always starts from
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
        models=_models(settings.GEMINI_TEXT_MODEL, settings.GEMINI_TEXT_FALLBACK_MODELS),
    )
    return (response.text or "").strip()
