"""Gemini as the chat reasoning engine, for Roman Urdu replies.

Roman Urdu is always routed here (see `apps.chat.services.select_model_tier`) —
not just the reasoning-tier subset — because Groq's Llama models write
grammatically-passable but oddly-phrased Roman Urdu, and the owner chose
pronunciation/phrasing quality over the lower per-call cost of Groq for this
one language. English and native Urdu-script traffic never reach this module.
"""

import logging

from django.conf import settings
from google.genai import types

from apps.image_info_extractor.gemini_client import _generate, _models

logger = logging.getLogger(__name__)


def call_gemini_chat(*, messages, timeout=20):
    """Same job as `apps.chat.groq_client.call_groq`, same input shape
    (an OpenAI-style messages list: system + history + new turn), same
    output shape (raw JSON text for `AiReplySerializer` to validate) — so
    `generate_reply` can dispatch to either without branching beyond the
    single call site.

    `timeout` is accepted for signature parity with `call_groq` but isn't
    passed through: the Gemini SDK call here (`_generate`, shared with the
    vision/transliteration paths) doesn't expose a per-call timeout knob,
    same as every other `apps.image_info_extractor.gemini_client` function.
    """
    system_content = None
    contents = []
    for message in messages:
        if message["role"] == "system":
            # Only one system message is ever sent (see
            # `prompt.build_messages`); a later one would silently replace
            # the accumulated business context, so keep the first only.
            if system_content is None:
                system_content = message["content"]
            continue
        role = "user" if message["role"] == "user" else "model"
        contents.append(types.Content(role=role, parts=[types.Part.from_text(text=message["content"])]))

    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        system_instruction=system_content,
    )
    response = _generate(
        contents,
        config=config,
        models=_models(settings.GEMINI_TEXT_MODEL, settings.GEMINI_TEXT_FALLBACK_MODELS),
    )
    logger.info("gemini chat call ok")
    return (response.text or "").strip()
