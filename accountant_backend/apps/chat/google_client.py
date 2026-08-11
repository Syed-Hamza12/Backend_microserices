"""Chat's model clients — both tiers now run on Google's Generative Language
API: `call_gemma_planner` (fast/planner tier, Gemma) and
`call_gemini_reasoning`/`call_gemini_text` (reasoning/quality tier, replacing
Groq's `llama-3.3-70b-versatile` — see `apps.chat.services._call_model` and
`_write_final_reply`). Each tier uses its own key pool
(`settings.FAST_GEMINI_API_KEYS` / `settings.QUALITY_GEMINI_API_KEYS`),
never shared with each other or with OCR's `GEMINI_API_KEYS` — three
features that used to compete for one shared free-tier daily quota now each
get their own.

Key rotation and model-fallback mechanics are NOT reimplemented here — see
`apps.integrations.google_genai_client`, the one canonical Google API
client this codebase uses (shared with
`apps.image_info_extractor.gemini_client`'s vision OCR extraction). This
module owns exactly two things: which model/keys each tier requests, and
translating between the OpenAI-style `{role, content}` messages list the
rest of `apps.chat` already builds and Google's `genai`
`contents`/`system_instruction` shape — so every function here returns a
plain string (JSON text or plain text), the same contract Groq's
`call_groq`/`call_groq_text` had before this swap. Callers in
`apps.chat.services` do not need to know or care which provider answered.
"""

import logging

from django.conf import settings
from google.genai import types

from apps.integrations.google_genai_client import generate, model_ladder

logger = logging.getLogger(__name__)


def _to_google_contents(messages):
    """OpenAI-style `[{"role": ..., "content": ...}, ...]` -> a
    `(system_instruction, contents)` pair for `google.genai`.

    Gemini has no "system" role inside `contents` at all — the system
    prompt is a separate `system_instruction` config value — and its two
    conversation roles are "user" and "model", not OpenAI's "user" and
    "assistant". `apps.chat.prompt.build_messages` always puts exactly one
    system message first (never more, in practice), but this concatenates
    if it ever saw more than one rather than silently dropping any.
    """
    system_instruction = None
    contents = []
    for message in messages:
        role = message["role"]
        text = message["content"]
        if role == "system":
            system_instruction = (
                f"{system_instruction}\n\n{text}" if system_instruction else text
            )
            continue
        google_role = "model" if role == "assistant" else "user"
        contents.append(types.Content(role=google_role, parts=[types.Part.from_text(text=text)]))
    return system_instruction, contents


def call_gemma_planner(*, messages, timeout=20):
    """Runs the fast/planner-tier JSON call through Gemma.

    Returns the raw text response (a JSON string) — the exact same return
    contract `apps.chat.groq_client.call_groq(reasoning=False)` had before
    this swap, so `apps.chat.services._call_model` needed no other change
    to its caller-side handling (`_parse_and_validate` parses this
    identically regardless of which provider produced it).

    `timeout` is accepted for interface parity with `call_groq` (both are
    swappable behind `_call_model`) but is not separately enforced here —
    `google.genai`'s client has its own default HTTP timeout; wiring a
    custom per-call timeout through would be a real feature, not a
    same-behavior swap, so it's deliberately left for a follow-up if ever
    needed rather than guessed at here.
    """
    system_instruction, contents = _to_google_contents(messages)
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        system_instruction=system_instruction,
    )
    models = model_ladder(settings.GOOGLE_FAST_MODEL, settings.GOOGLE_FAST_FALLBACK_MODELS)
    response = generate(settings.FAST_GEMINI_API_KEYS, models, contents, config=config, logger=logger)
    return response.text


def call_gemini_reasoning(*, messages, response_format_json=True, timeout=20):
    """Runs the reasoning/quality-tier call through Gemini — replaces Groq's
    `llama-3.3-70b-versatile` (see `apps.chat.services._call_model` and
    `_write_final_reply`).

    Same dual JSON/plain-text shape `apps.chat.groq_client.call_groq` had,
    so callers needed no other change: JSON mode (the default) returns a
    JSON string for `_call_model`'s and `_write_final_reply`'s JSON-mode
    calls; `response_format_json=False` returns plain text, used by
    `call_gemini_text` below for transliteration/speech-script conversion.

    Uses its own key pool (`settings.QUALITY_GEMINI_API_KEYS`) — this is the
    highest-volume chat traffic (every bill/edit/document-intent message,
    plus the entire Roman Urdu/Urdu response-writer pass), so it gets its
    own free-tier quota rather than competing with the fast tier or OCR.

    `timeout` is accepted for interface parity with the old `call_groq`
    but is not separately enforced here, same as `call_gemma_planner`.
    """
    system_instruction, contents = _to_google_contents(messages)
    config = types.GenerateContentConfig(
        response_mime_type="application/json" if response_format_json else "text/plain",
        system_instruction=system_instruction,
    )
    models = model_ladder(settings.GOOGLE_QUALITY_MODEL, settings.GOOGLE_QUALITY_FALLBACK_MODELS)
    response = generate(settings.QUALITY_GEMINI_API_KEYS, models, contents, config=config, logger=logger)
    return response.text


def call_gemini_text(instructions: str, text: str) -> str:
    """Plain text-in/text-out call on the reasoning/quality tier — for
    transliteration and speech_text script conversion. Mirrors
    `apps.chat.groq_client.call_groq_text`'s old shape exactly, just on
    Gemini now (see `call_gemini_reasoning`)."""
    return call_gemini_reasoning(
        messages=[{"role": "user", "content": f"{instructions}\n\n{text}"}],
        response_format_json=False,
    )
