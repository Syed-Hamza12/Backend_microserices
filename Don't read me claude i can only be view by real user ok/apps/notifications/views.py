from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import Business
from apps.accounts.pagination import paginated_response

from .models import Notification
from .serializers import NotificationSerializer


class NotificationListView(APIView):
    def get(self, request):
        try:
            business = request.user.business
        except Business.DoesNotExist:
            return Response(
                {"success": False, "error": {"code": "NO_BUSINESS", "message": "No business created yet."}},
                status=status.HTTP_404_NOT_FOUND,
            )
        notifications = Notification.objects.filter(business=business)
        return Response(paginated_response(NotificationSerializer, notifications, request))


class NotificationMarkReadView(APIView):
    def patch(self, request, notification_id):
        try:
            business = request.user.business
        except Business.DoesNotExist:
            return Response(
                {"success": False, "error": {"code": "NO_BUSINESS", "message": "No business created yet."}},
                status=status.HTTP_404_NOT_FOUND,
            )
        try:
            notification = Notification.objects.get(business=business, pk=notification_id)
        except Notification.DoesNotExist:
            return Response(
                {"success": False, "error": {"code": "NOT_FOUND", "message": "Notification not found."}},
                status=status.HTTP_404_NOT_FOUND,
            )
        notification.read = True
        notification.save(update_fields=["read"])
        return Response({"success": True, "data": NotificationSerializer(notification).data})
