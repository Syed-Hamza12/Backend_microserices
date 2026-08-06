from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .permissions import HasFeature


class DummyAiChatPingView(APIView):
    """Proves the feature-gate 403 path works end-to-end. Not a real feature — delete once
    Milestone 7's real ai_chat endpoint exists and can be tested the same way."""

    permission_classes = [IsAuthenticated, HasFeature]
    required_feature = "ai_chat"

    def get(self, request):
        return Response({"success": True, "data": {"message": "ai_chat feature is enabled for this business."}})
