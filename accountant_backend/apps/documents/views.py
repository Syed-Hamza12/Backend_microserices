from django.http import HttpResponse
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from apps.accounts.models import Business
from apps.accounts.pagination import paginated_response
from apps.billing.permissions import HasFeature
from apps.billing.services import enforce_feature_gate
from apps.customers.models import Customer
from apps.jobs.dispatch import enqueue

from .models import DocumentDelivery
from .serializers import DocumentDeliverySerializer, RenderDocumentSerializer, SendDocumentSerializer
from .services import (
    ALL_DOC_TYPES,
    DEFAULT_FORMAT,
    SUPPORTED_FORMATS,
    DocumentError,
    build_payload_for,
    render_document,
)


class DocumentThrottle(UserRateThrottle):
    """Rendering is real CPU work and sending costs WhatsApp reputation, so
    both paths get a tighter ceiling than ordinary reads."""

    scope = "documents"


def _error(code, message, code_status):
    return Response({"success": False, "error": {"code": code, "message": message}}, status=code_status)


def _business_or_error(request):
    try:
        return request.user.business, None
    except Business.DoesNotExist:
        return None, _error("NO_BUSINESS", "No business created yet.", status.HTTP_404_NOT_FOUND)


class DocumentFormatsView(APIView):
    """Which output formats each document type supports, and the default."""

    def get(self, request):
        return Response(
            {
                "success": True,
                "data": {
                    doc_type: {"formats": SUPPORTED_FORMATS[doc_type], "default": DEFAULT_FORMAT[doc_type]}
                    for doc_type in sorted(ALL_DOC_TYPES)
                },
            }
        )


class ExportExcelView(APIView):
    """Whole-ledger export: business name, then every customer's statement
    one after another, then grand totals — the Settings > Data Export
    button's only feature so far. Synchronous like RenderDocumentView since
    it's a direct download, not a WhatsApp send."""

    throttle_classes = [DocumentThrottle]

    def get(self, request):
        business, error = _business_or_error(request)
        if error:
            return error

        from .excel_export import build_export_workbook

        workbook_bytes = build_export_workbook(business)
        response = HttpResponse(
            workbook_bytes,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = 'attachment; filename="data_export.xlsx"'
        return response


class RenderDocumentView(APIView):
    """Renders a document and returns the file itself, for preview and sharing.

    Synchronous because there is a person waiting to look at it, and rendering
    alone is fast (~80ms for a PDF). Sending is the slow path and is a job.

    Nothing is stored: the bytes are streamed to the client and discarded.
    """

    throttle_classes = [DocumentThrottle]

    def post(self, request):
        business, error = _business_or_error(request)
        if error:
            return error

        serializer = RenderDocumentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        doc_type = data["doc_type"]
        output_format = data.get("format") or DEFAULT_FORMAT[doc_type]

        if output_format not in SUPPORTED_FORMATS[doc_type]:
            return _error(
                "UNSUPPORTED_FORMAT",
                f"{doc_type} cannot be produced as {output_format}.",
                status.HTTP_400_BAD_REQUEST,
            )

        try:
            payload, _entry, _customer = build_payload_for(
                business,
                doc_type=doc_type,
                target_id=data.get("target_id"),
                customer_id=data.get("customer_id"),
                date_from=data.get("date_from"),
                date_to=data.get("date_to"),
            )
            content, delivered_format = render_document(
                doc_type=doc_type,
                output_format=output_format,
                business_id=business.id,
                payload=payload,
            )
        except DocumentError as exc:
            code_status = (
                status.HTTP_404_NOT_FOUND if exc.code == "NOT_FOUND" else status.HTTP_502_BAD_GATEWAY
            )
            return _error(exc.code, exc.message, code_status)

        extension = "png" if delivered_format == "image" else "pdf"
        response = HttpResponse(
            content,
            content_type="image/png" if delivered_format == "image" else "application/pdf",
        )
        response["Content-Disposition"] = f'inline; filename="{doc_type}.{extension}"'
        # The renderer may substitute PDF for an over-long image; tell the
        # client what it actually received rather than letting it assume.
        response["X-Document-Format"] = delivered_format
        return response


class SendDocumentView(APIView):
    """Queues a document to be rendered and sent to a customer over WhatsApp.

    Returns a job id immediately. The work happens in the background worker
    because the Gateway paces sends deliberately to protect the business's
    WhatsApp number, which can take several seconds.
    """

    permission_classes = [IsAuthenticated, HasFeature]
    required_feature = "whatsapp_send"
    throttle_classes = [DocumentThrottle]

    def post(self, request):
        business, error = _business_or_error(request)
        if error:
            return error

        if not business.gateway_session_id:
            return _error(
                "SESSION_NOT_CONNECTED",
                "WhatsApp is not connected for this business.",
                status.HTTP_409_CONFLICT,
            )

        serializer = SendDocumentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        doc_type = data["doc_type"]
        output_format = data.get("format") or DEFAULT_FORMAT[doc_type]

        if output_format not in SUPPORTED_FORMATS[doc_type]:
            return _error(
                "UNSUPPORTED_FORMAT",
                f"{doc_type} cannot be sent as {output_format}.",
                status.HTTP_400_BAD_REQUEST,
            )

        # Validate the target and resolve the recipient before queueing, so a
        # bad request fails immediately instead of becoming a failed job the
        # owner has to go and discover.
        try:
            _payload, entry, customer = build_payload_for(
                business,
                doc_type=doc_type,
                target_id=data.get("target_id"),
                customer_id=data.get("customer_id"),
                date_from=data.get("date_from"),
                date_to=data.get("date_to"),
            )
        except DocumentError as exc:
            return _error(exc.code, exc.message, status.HTTP_404_NOT_FOUND)

        to_phone = data.get("to") or (customer.phone if customer else None)
        if not to_phone:
            return _error(
                "RECIPIENT_REQUIRED",
                "No phone number to send to — pass `to` or pick a customer with a phone number.",
                status.HTTP_400_BAD_REQUEST,
            )

        # Consumes the monthly whatsapp_send quota, exactly as an ad-hoc text
        # send does. HasFeature only answers "is this feature on the plan"; it
        # never touches UsageCounter — so document sends, the main product
        # action, were unmetered while text reminders were counted.
        enforce_feature_gate(business, "whatsapp_send")

        delivery = DocumentDelivery.objects.create(
            business=business,
            customer=customer,
            doc_type=doc_type,
            requested_format=output_format,
            to_phone=to_phone,
            related_entry=entry,
            parameters={
                "target_id": data.get("target_id"),
                "customer_id": data.get("customer_id") or (customer.id if customer else None),
                "date_from": data.get("date_from"),
                "date_to": data.get("date_to"),
            },
        )
        job = enqueue(
            business=business, type="document_send", payload={"delivery_id": delivery.id}
        )
        delivery.job_task = job
        delivery.save(update_fields=["job_task"])

        return Response(
            {
                "success": True,
                "data": {
                    "job_id": job.id,
                    "delivery": DocumentDeliverySerializer(delivery).data,
                },
            },
            status=status.HTTP_202_ACCEPTED,
        )


class DocumentDeliveryListView(APIView):
    """Delivery history — what was sent, to whom, and whether it arrived."""

    def get(self, request):
        business, error = _business_or_error(request)
        if error:
            return error

        deliveries = DocumentDelivery.objects.filter(business=business).select_related("customer")

        customer_id = request.query_params.get("customer_id")
        if customer_id:
            deliveries = deliveries.filter(customer_id=customer_id)

        return Response(paginated_response(DocumentDeliverySerializer, deliveries, request))


class DocumentDeliveryDetailView(APIView):
    """Status of one delivery, for polling after a send is queued."""

    def get(self, request, delivery_id):
        business, error = _business_or_error(request)
        if error:
            return error
        try:
            delivery = DocumentDelivery.objects.select_related("customer").get(
                business=business, pk=delivery_id
            )
        except DocumentDelivery.DoesNotExist:
            return _error("NOT_FOUND", "Delivery not found.", status.HTTP_404_NOT_FOUND)
        return Response({"success": True, "data": DocumentDeliverySerializer(delivery).data})
