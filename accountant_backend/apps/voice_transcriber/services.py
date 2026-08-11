"""Async worker-side handling for voice-note chat messages.

Mirrors apps.image_info_extractor.services.handle_image_extract_job's shape
exactly — same JobTask-driven async pattern, same graceful-degradation and
refund rules — for the same reason that module documents: Gemini is called
directly from the Django job worker, no separate model/table needed here
since a voice note needs no equivalent of ExtractionJob's resolved_customer/
extracted_data bookkeeping — the transcript lives directly on the
ChatMessage it belongs to.
"""

import logging
import mimetypes
from pathlib import Path

from django.conf import settings

from apps.billing.services import refund_feature_usage
from apps.chat import services as chat_services
from apps.chat.models import ChatMessage, Conversation
from apps.chat.serializers import ChatMessageSerializer

from . import google_client

logger = logging.getLogger(__name__)

FALLBACK_TEXT = "Sorry, I couldn't understand that voice note — please try again."
NO_SPEECH_TEXT = "I couldn't make out any speech in that voice note — please try recording again."


def _read_audio(source_audio_url):
    """Same MEDIA_ROOT containment reasoning as
    apps.image_info_extractor.services._read_image — the URL is
    server-generated today, but this stays closed if that ever changes."""
    relative = source_audio_url
    if relative.startswith(settings.MEDIA_URL):
        relative = relative[len(settings.MEDIA_URL):]

    media_root = Path(settings.MEDIA_ROOT).resolve()
    path = (media_root / relative).resolve()
    if not path.is_relative_to(media_root):
        raise ValueError("Refusing to read audio from outside MEDIA_ROOT.")

    mime_type = mimetypes.guess_type(str(path))[0] or "audio/mp4"
    return path.read_bytes(), mime_type


def handle_voice_transcribe_job(job_task):
    """Called by the jobs worker loop for type="voice_transcribe" JobTasks.

    Transcribes the uploaded audio, fills the transcript into the owner's
    already-created ChatMessage (see apps.chat.views.UploadChatVoiceView,
    which creates it up front so the recording shows in chat immediately),
    then runs it through the exact same apps.chat.services.generate_reply
    pipeline typed text uses — so a voice note gets identical AI handling
    (drafts, replies, everything) to typing the same words.

    Returns {"status": ..., "message": <owner message JSON>, "reply": <ai
    message JSON or None>} — both are included (unlike the image job, which
    only returns the AI reply) because the owner's own message also changes
    here: it starts with no text and gets the transcript filled in.
    """
    business = job_task.business
    message_id = job_task.payload.get("message_id")

    try:
        message = ChatMessage.objects.get(pk=message_id, conversation__business=business, sender="owner")
    except ChatMessage.DoesNotExist:
        # The owner cleared their chat history while this job was queued —
        # same "nowhere to post the reply" case as the image job's deleted-
        # conversation abandon.
        logger.info("voice transcribe job %s abandoned: message was deleted", job_task.id)
        return {"status": "abandoned", "reason": "message_deleted"}

    conversation = message.conversation

    try:
        audio_bytes, mime_type = _read_audio(message.audio_url)
        language = business.language
        transcript = google_client.transcribe_audio(audio_bytes, mime_type, language=language)
    except Exception as exc:  # noqa: BLE001 - Gemini unreachable/not configured must degrade gracefully
        # The upload endpoint claimed an ai_chat slot before queueing this job
        # (apps.chat.views.UploadChatVoiceView). Transcription never happened,
        # so that slot is given back — same reasoning as the image path
        # refunding image_extraction on a failed extraction.
        refund_feature_usage(business, "ai_chat")
        message.text = FALLBACK_TEXT
        message.save(update_fields=["text"])
        logger.warning("voice transcription failed for message %s: %s", message.id, exc)
        return {
            "status": "failed",
            "message": ChatMessageSerializer(message).data,
            "reply": None,
        }

    if not transcript:
        refund_feature_usage(business, "ai_chat")
        message.text = NO_SPEECH_TEXT
        message.save(update_fields=["text"])
        return {
            "status": "failed",
            "message": ChatMessageSerializer(message).data,
            "reply": None,
        }

    # A Roman Urdu instruction can still come back in Urdu script if the model
    # ignores it — running it through the existing script->Roman-Urdu path
    # (apps.chat.services.transliterate_to_roman_urdu, already used for the
    # analogous case in the old on-device flow) is a safety net, not the
    # primary fix.
    if language == "roman_ur" and chat_services.has_urdu_script(transcript):
        transcript = chat_services.transliterate_to_roman_urdu(transcript)

    message.text = transcript
    message.speech_text = transcript
    message.save(update_fields=["text", "speech_text"])

    ai_message = chat_services.generate_reply(
        business=business, conversation=conversation, text=transcript, language=language
    )

    return {
        "status": "done",
        "message": ChatMessageSerializer(message).data,
        "reply": ChatMessageSerializer(ai_message).data,
    }
