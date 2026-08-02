import logging

from django.conf import settings
from groq import Groq

logger = logging.getLogger(__name__)

_clients = {}


def _client_for(key):
    if key not in _clients:
        _clients[key] = Groq(api_key=key)
    return _clients[key]


def call_groq(*, messages, reasoning=False, response_format_json=True, timeout=20):
    """One place to change model name/retry/timeout for every Groq call in the app.
    `messages` is the full OpenAI-style messages list (system + history + new turn`).

    Free-tier key rotation: settings.GROQ_API_KEYS is one or more API keys
    (GROQ_API_KEY plus GROQ_API_KEY_1.._10 in .env). Always starts from key
    0 on every call (not "whichever key worked last") — a rate-limit
    rejection is fast, so there's no real cost to checking, and this way a
    key's quota resetting (per-minute/per-day) means it's automatically back
    in use on the very next call, with no separate "has it reset yet"
    tracking needed. On ANY failure with the current key — a 429 rate limit
    is the expected case, but this doesn't special-case it, any error just
    means "try the next key" — moves to the next configured key. Only
    raises once every configured key has failed."""
    keys = settings.GROQ_API_KEYS
    if not keys:
        raise RuntimeError("No GROQ_API_KEY configured on this server.")

    model = settings.GROQ_MODEL_REASONING if reasoning else settings.GROQ_MODEL_FAST
    kwargs = {"model": model, "messages": messages, "timeout": timeout}
    if response_format_json:
        kwargs["response_format"] = {"type": "json_object"}

    last_error = None
    for idx, key in enumerate(keys):
        client = _client_for(key)
        try:
            completion = client.chat.completions.create(**kwargs)
            content = completion.choices[0].message.content
            logger.info("groq call ok key_index=%s model=%s tokens=%s", idx, model, completion.usage)
            return content
        except Exception as exc:  # noqa: BLE001 - any failure rotates to the next key
            last_error = exc
            logger.warning("groq call failed key_index=%s model=%s error=%s", idx, model, exc)

    raise last_error


def call_groq_text(instructions: str, text: str) -> str:
    """Plain text-in/text-out call, for transliteration and speech_text
    script conversion — the same shape as
    `apps.image_info_extractor.gemini_client.generate_text`, on Groq instead
    of Gemini.

    These moved off Gemini entirely (previously the reasoning for using
    Gemini here at all): Gemini's free-tier quota is only 20 requests/day,
    and every one of these calls happens on ordinary chat traffic (every
    dictated message, every Roman Urdu reply's speech_text) — nowhere near
    OCR's volume. Gemini is now reserved for
    `apps.image_info_extractor.gemini_client.extract_receipt_data` only.

    Uses the reasoning-tier model (70B), not the fast one: 8B's Urdu output
    quality is the documented reason it's excluded from Urdu chat replies at
    all (see `apps.chat.services.select_model_tier`), and that applies here
    just as much.
    """
    return call_groq(
        messages=[{"role": "user", "content": f"{instructions}\n\n{text}"}],
        reasoning=True,
        response_format_json=False,
    )
