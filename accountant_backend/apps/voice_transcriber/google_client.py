"""Voice-note transcription — the Gemini call behind the audio-upload chat
path (apps.voice_transcriber.services.handle_voice_transcribe_job).

This replaces the app's old on-device flow (record → speech_to_text →
TransliterateView) with a single server-side call. The old flow's real bug
was never the transliteration step — it was that Android's on-device
recognizer mis-heard Roman Urdu speech in the first place, and transliterating
a wrong transcript just produces confidently-wrong Roman Urdu text. Asking
Gemini to transcribe straight into Roman Urdu (when that's what it hears)
fixes this at the source instead of correcting it after the fact.

Shares the same canonical key-rotation/model-fallback client every other
Google-backed caller in this codebase uses
(apps.integrations.google_genai_client) — see that module's docstring for why
one implementation is kept instead of each caller having its own copy.
"""

import logging

from django.conf import settings
from google.genai import types

from apps.integrations.google_genai_client import generate, model_ladder

logger = logging.getLogger(__name__)

# Keyed by the same `language` codes as apps.chat.services.VALID_LANGUAGES /
# Business.LANGUAGE_CHOICES, so the caller never needs a separate mapping.
_LANGUAGE_INSTRUCTIONS = {
    "roman_ur": (
        "The speaker is talking in Roman Urdu (Urdu written phonetically with "
        "Latin letters, e.g. 'kal maine Ali ko 500 rupay diye'). Transcribe "
        "EXACTLY what they said, but write the transcript in Roman Urdu using "
        "Latin letters only — never in Urdu (Arabic) script, even if that is "
        "what the words would normally be written in. If the speaker mixes in "
        "English words or numbers, keep those as written."
    ),
    "ur": (
        "The speaker is talking in Urdu. Transcribe exactly what they said, in "
        "Urdu (Arabic) script."
    ),
    "en": "The speaker is talking in English. Transcribe exactly what they said.",
}

DEFAULT_INSTRUCTION = _LANGUAGE_INSTRUCTIONS["roman_ur"]

_PROMPT_TEMPLATE = """
Transcribe this voice message from a small shopkeeper talking to their
bookkeeping assistant about a sale, payment, or customer. {language_instruction}

Rules:
- Output ONLY the transcript text, nothing else — no preamble, no quotes, no
  commentary.
- If you cannot make out any speech at all (silence, noise, unintelligible),
  output exactly: [inaudible]
- Numbers, amounts, and customer names matter most here — a shopkeeper's own
  ledger entries turn into money records, so get digits and names right over
  smoothing the phrasing.
""".strip()


def transcribe_audio(audio_bytes: bytes, mime_type: str, *, language: str = "roman_ur") -> str:
    """Runs one voice note through the transcription model/key ladder.

    Raises RuntimeError (no keys configured) or the last provider error on
    total failure — callers must handle that gracefully, same contract as
    apps.image_info_extractor.gemini_client.extract_receipt_data (see
    services.handle_voice_transcribe_job's fallback-apology path).
    """
    language_instruction = _LANGUAGE_INSTRUCTIONS.get(language, DEFAULT_INSTRUCTION)
    prompt = _PROMPT_TEMPLATE.format(language_instruction=language_instruction)

    response = generate(
        settings.AUDIO_GEMINI_API_KEYS,
        model_ladder(settings.GEMINI_AUDIO_MODEL, settings.GEMINI_AUDIO_FALLBACK_MODELS),
        [types.Part.from_bytes(data=audio_bytes, mime_type=mime_type), prompt],
        logger=logger,
    )
    text = (response.text or "").strip()
    if text == "[inaudible]":
        return ""
    return text
