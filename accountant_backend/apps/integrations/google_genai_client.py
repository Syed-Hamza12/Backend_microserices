"""Canonical Google Generative Language API client — the key-rotation and
model-fallback mechanics shared by every caller of a Google-hosted model in
this codebase (today: `apps.image_info_extractor.gemini_client`'s vision
OCR extraction, and `apps.chat.google_client`'s fast-tier chat planner).

This module owns exactly one thing: "how to reach Google reliably given N
API keys and M candidate models." It owns no prompts, no response schemas,
no business logic — those stay in each caller, which is what keeps this
extraction worth doing rather than just an extra layer of indirection.
Before this existed, `apps.image_info_extractor.gemini_client` had its own
copy of this exact rotation loop; a second copy would have been written for
the chat planner. One canonical implementation means a fix to the rotation
logic (e.g. how a 503 is distinguished from a quota 429) lands for every
Google-backed caller at once, not just the one someone happened to be
editing.
"""

from google import genai

_clients = {}


def client_for(key):
    """One `genai.Client` per API key, reused across calls — building a
    fresh client per request was needless overhead for something that's
    just a thin HTTP wrapper around a key."""
    if key not in _clients:
        _clients[key] = genai.Client(api_key=key)
    return _clients[key]


def model_ladder(primary, fallbacks=None):
    """The model to try first, then the fallbacks, de-duplicated and with
    empty/None entries dropped. Kept as a plain ordering helper (not baked
    into `generate`) so a caller with only one real model can just pass
    `model_ladder(settings.SOME_MODEL)` and get a clean single-item list.
    """
    seen, ordered = set(), []
    for name in [primary, *(fallbacks or [])]:
        if name and name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def generate(keys, models, contents, *, config=None, logger=None):
    """Runs `contents` through the model/key ladder, returning the raw
    `google.genai` response. Ordered model-first, not key-first: the most
    common real failure is `503 UNAVAILABLE - "This model is currently
    experiencing high demand"`, which is the MODEL being busy, not the key
    being out of quota — every key would fail identically against a busy
    model, so a different model is what actually recovers, and is tried
    before burning through the other keys on one that's already known to
    be struggling.

    Raises RuntimeError if `keys` is empty (nothing configured — the
    caller decides how to degrade, same as apps.chat.groq_client.call_groq's
    "no keys configured" behavior). Otherwise re-raises the last error seen
    once every (model, key) combination has failed.
    """
    if not keys:
        raise RuntimeError("No Google API key configured on this server.")

    last_error = None
    for model in models:
        for idx, key in enumerate(keys):
            try:
                response = client_for(key).models.generate_content(
                    model=model, contents=contents, config=config
                )
                if logger:
                    logger.info("google genai call ok key_index=%s model=%s", idx, model)
                return response
            except Exception as exc:  # noqa: BLE001 - any failure tries the next key/model
                last_error = exc
                if logger:
                    logger.warning(
                        "google genai call failed key_index=%s model=%s error=%s", idx, model, exc
                    )

    raise last_error
