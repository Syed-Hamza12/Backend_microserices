"""apps.voice_transcriber.services.handle_voice_transcribe_job — mirrors
apps.image_info_extractor's job-handler tests (see
apps.chat.test_sync.ClearChatHistoryTests.test_a_queued_image_job_survives_its_conversation_being_deleted)."""

from decimal import Decimal
from unittest import mock

from django.test import TestCase

from apps.accounts.models import Business, User
from apps.chat.models import ChatMessage, Conversation
from apps.customers.models import Customer
from apps.jobs.models import JobTask

from . import services


class VoiceTranscribeJobTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="m@x.com", email="m@x.com", password="pw")
        self.business = Business.objects.create(owner=self.user, business_name="Hamza Traders", language="roman_ur")
        Customer.objects.create(
            business=self.business, name="Ali", phone="923000000000",
            opening_balance=Decimal("0"), current_balance=Decimal("0"),
        )
        self.conversation = Conversation.objects.create(business=self.business)
        self.message = ChatMessage.objects.create(
            conversation=self.conversation, sender="owner", audio_url="/media/voice_notes/x.m4a",
        )
        self.job = JobTask.objects.create(
            business=self.business,
            type="voice_transcribe",
            payload={"conversation_id": self.conversation.id, "message_id": self.message.id},
        )

    def _read_audio_patch(self):
        return mock.patch.object(services, "_read_audio", return_value=(b"fake-audio-bytes", "audio/mp4"))

    def test_successful_transcription_fills_transcript_and_gets_a_reply(self):
        ai_reply = ChatMessage.objects.create(conversation=self.conversation, sender="ai", text="Samajh gaya")
        with self._read_audio_patch(), \
                mock.patch.object(services.google_client, "transcribe_audio", return_value="Ali ko 500 diye"), \
                mock.patch.object(services.chat_services, "generate_reply", return_value=ai_reply) as mock_generate:
            result = services.handle_voice_transcribe_job(self.job)

        self.message.refresh_from_db()
        self.assertEqual(self.message.text, "Ali ko 500 diye")
        self.assertEqual(self.message.speech_text, "Ali ko 500 diye")
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["message"]["text"], "Ali ko 500 diye")
        self.assertEqual(result["reply"]["id"], ai_reply.id)
        mock_generate.assert_called_once_with(
            business=self.business, conversation=self.conversation, text="Ali ko 500 diye", language="roman_ur",
        )

    def test_transcription_failure_refunds_ai_chat_and_leaves_a_fallback_message(self):
        with self._read_audio_patch(), \
                mock.patch.object(services.google_client, "transcribe_audio", side_effect=RuntimeError("boom")), \
                mock.patch.object(services, "refund_feature_usage") as mock_refund:
            result = services.handle_voice_transcribe_job(self.job)

        self.message.refresh_from_db()
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["reply"])
        self.assertEqual(self.message.text, services.FALLBACK_TEXT)
        mock_refund.assert_called_once_with(self.business, "ai_chat")

    def test_no_speech_heard_refunds_and_leaves_a_no_speech_message(self):
        with self._read_audio_patch(), \
                mock.patch.object(services.google_client, "transcribe_audio", return_value=""), \
                mock.patch.object(services, "refund_feature_usage") as mock_refund:
            result = services.handle_voice_transcribe_job(self.job)

        self.message.refresh_from_db()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.message.text, services.NO_SPEECH_TEXT)
        mock_refund.assert_called_once_with(self.business, "ai_chat")

    def test_a_queued_voice_job_survives_its_message_being_deleted(self):
        self.message.delete()

        result = services.handle_voice_transcribe_job(self.job)
        self.assertEqual(result["status"], "abandoned")

    def test_urdu_script_transcript_is_transliterated_back_to_roman_urdu(self):
        with self._read_audio_patch(), \
                mock.patch.object(services.google_client, "transcribe_audio", return_value="علی کو پانچ سو دیے"), \
                mock.patch.object(
                    services.chat_services, "transliterate_to_roman_urdu", return_value="Ali ko 500 diye"
                ) as mock_translit, \
                mock.patch.object(services.chat_services, "generate_reply", return_value=ChatMessage()) as mock_generate:
            services.handle_voice_transcribe_job(self.job)

        mock_translit.assert_called_once_with("علی کو پانچ سو دیے")
        mock_generate.assert_called_once()
        self.assertEqual(mock_generate.call_args.kwargs["text"], "Ali ko 500 diye")
