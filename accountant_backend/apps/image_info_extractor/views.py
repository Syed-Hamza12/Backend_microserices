from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import Business
from apps.accounts.uploads import save_validated_image
from apps.billing.permissions import HasFeature
from apps.billing.services import enforce_feature_gate
from apps.chat.models import ChatMessage, Conversation
from apps.jobs.dispatch import enqueue

from .models import ExtractionJob


class UploadChatImageView(APIView):
    permission_classes = [IsAuthenticated, HasFeature]
    required_feature = "image_extraction"
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        try:
            business = request.user.business
        except Business.DoesNotExist:
            return Response(
                {"success": False, "error": {"code": "NO_BUSINESS", "message": "No business created yet."}},
                status=status.HTTP_404_NOT_FOUND,
            )

        image = request.FILES.get("image")
        if not image:
            return Response(
                {"success": False, "error": {"code": "VALIDATION_ERROR", "message": "image file is required."}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        conversation_id = request.data.get("conversationId")
        if conversation_id:
            try:
                conversation = Conversation.objects.get(business=business, pk=conversation_id)
            except Conversation.DoesNotExist:
                return Response(
                    {"success": False, "error": {"code": "NOT_FOUND", "message": "Conversation not found."}},
                    status=status.HTTP_404_NOT_FOUND,
                )
        else:
            conversation = Conversation.objects.create(business=business)

        # Feature-gate check happens before any expensive work — the upload itself is cheap, but
        # this also blocks the JobTask (and therefore the Gemini call) from ever being created.
        enforce_feature_gate(business, "image_extraction")

        image_url = save_validated_image(image, subdirectory="uploads")

        ChatMessage.objects.create(conversation=conversation, sender="owner", image_url=image_url)

        job = enqueue(
            business=business,
            type="image_extract",
            payload={"conversation_id": conversation.id},
        )
        ExtractionJob.objects.create(
            business=business, job_task=job, source_image_url=image_url, status="pending"
        )

        return Response(
            {"success": True, "data": {"job_id": job.id, "conversationId": conversation.id}},
            status=status.HTTP_202_ACCEPTED,
        )
