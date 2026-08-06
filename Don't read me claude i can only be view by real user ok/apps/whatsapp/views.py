from django.http import HttpResponse
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import Business
from apps.billing.permissions import HasFeature
from apps.billing.services import enforce_feature_gate

from . import services
from .gateway_client import GatewayError


def _business_or_error(request):
    try:
        return request.user.business, None
    except Business.DoesNotExist:
        return None, Response(
            {"success": False, "error": {"code": "NO_BUSINESS", "message": "No business created yet."}},
            status=status.HTTP_404_NOT_FOUND,
        )


def _gateway_error_response(exc: GatewayError):
    return Response({"success": False, "error": {"code": exc.code, "message": exc.message}}, status=exc.status_code)


class WhatsAppConnectView(APIView):
    def post(self, request):
        business, error = _business_or_error(request)
        if error:
            return error
        try:
            data = services.connect(business)
        except GatewayError as exc:
            return _gateway_error_response(exc)
        return Response({"success": True, "data": data})


class WhatsAppStatusView(APIView):
    def get(self, request):
        business, error = _business_or_error(request)
        if error:
            return error
        try:
            data = services.get_status(business)
        except GatewayError as exc:
            return _gateway_error_response(exc)
        return Response({"success": True, "data": data})


class WhatsAppQrView(APIView):
    def get(self, request):
        business, error = _business_or_error(request)
        if error:
            return error
        try:
            png_bytes = services.get_qr_bytes(business)
        except GatewayError as exc:
            return _gateway_error_response(exc)
        if png_bytes is None:
            return Response(
                {"success": False, "error": {"code": "QR_NOT_AVAILABLE", "message": "No QR code available right now."}},
                status=status.HTTP_404_NOT_FOUND,
            )
        return HttpResponse(png_bytes, content_type="image/png")


class WhatsAppDisconnectView(APIView):
    def post(self, request):
        business, error = _business_or_error(request)
        if error:
            return error
        try:
            services.disconnect(business)
        except GatewayError as exc:
            return _gateway_error_response(exc)
        return Response({"success": True, "data": {}})


class WhatsAppUnlinkView(APIView):
    def delete(self, request):
        business, error = _business_or_error(request)
        if error:
            return error
        try:
            services.unlink(business)
        except GatewayError as exc:
            return _gateway_error_response(exc)
        return Response({"success": True, "data": {}})


class WhatsAppSendTextView(APIView):
    """Ad-hoc text send (reminders etc.) — gated by whatsapp_send, per feature_list.md's scope
    note that Basic-tier businesses can connect WhatsApp but not send through it."""

    permission_classes = [IsAuthenticated, HasFeature]
    required_feature = "whatsapp_send"

    def post(self, request):
        business, error = _business_or_error(request)
        if error:
            return error
        to = request.data.get("to")
        message = request.data.get("message")
        if not to or not message:
            return Response(
                {"success": False, "error": {"code": "VALIDATION_ERROR", "message": "to and message are required."}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        enforce_feature_gate(business, "whatsapp_send")
        try:
            services.send_text(business, to, message)
        except GatewayError as exc:
            return _gateway_error_response(exc)
        return Response({"success": True, "data": {}})


# WhatsAppSendDocumentView was removed. Sending a document is now
# POST /api/documents/send/ (apps.documents.views.SendDocumentView), which
# renders on demand and streams the bytes to the Gateway rather than sending a
# URL to a previously stored file. See apps/documents/delivery.py.
