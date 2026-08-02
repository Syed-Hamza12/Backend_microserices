"""Tests for document orchestration and delivery.

These cover the contract between the three services: Django owns the figures,
FastAPI renders, the Gateway sends, and nothing is written to disk along the
way. The renderer and gateway are faked here — each has its own suite — so
these stay fast and assert on orchestration rather than on layout.
"""

import os
import tempfile
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.accounts.models import Business, User
from apps.billing.models import Plan, PlanFeature, Subscription
from apps.customers.models import Customer
from apps.jobs.models import JobTask
from apps.sales import services as sales_services

from .delivery import handle_document_send_job
from .models import DocumentDelivery
from .services import DocumentError, build_payload_for

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake image data"
PDF_BYTES = b"%PDF-1.4 fake pdf data"


class DocumentTestMixin:
    def build_fixtures(self):
        self.user = User.objects.create_user(username="o@x.com", email="o@x.com", password="pw")
        self.business = Business.objects.create(
            owner=self.user, business_name="Hamza Traders", gateway_session_id="sess-1"
        )
        self.customer = Customer.objects.create(
            business=self.business, name="Ali Raza", phone="923001112222",
            opening_balance=Decimal("0"), current_balance=Decimal("0"),
        )
        self.sale, self.payment = sales_services.record_sale(
            business=self.business,
            customer=self.customer,
            items=[{"item_name": "Rice", "quantity": Decimal("2"), "rate": Decimal("500")}],
            amount_received=Decimal("400"),
            payment_method="cash",
        )

    def enable_whatsapp_send(self):
        plan = Plan.objects.create(name="Pro", price_pkr=Decimal("1000"))
        PlanFeature.objects.create(plan=plan, feature_key="whatsapp_send", enabled=True)
        Subscription.objects.create(
            business=self.business, plan=plan, status="active", started_at=timezone.now()
        )

    def make_delivery(self, doc_type="invoice", fmt="image", parameters=None):
        return DocumentDelivery.objects.create(
            business=self.business, customer=self.customer, doc_type=doc_type,
            requested_format=fmt, to_phone="923001112222", related_entry=self.sale,
            parameters=parameters or {"target_id": self.sale.id},
        )

    def make_job(self, delivery):
        return JobTask.objects.create(
            business=self.business, type="document_send", payload={"delivery_id": delivery.id}
        )


class PayloadBuildingTests(DocumentTestMixin, TestCase):
    def setUp(self):
        self.build_fixtures()

    def test_invoice_payload_comes_from_the_ledger(self):
        payload, entry, customer = build_payload_for(
            self.business, doc_type="invoice", target_id=self.sale.id
        )
        # Django is the single source of truth for every figure on a document.
        self.assertEqual(payload["subtotal"], "1000.00")
        self.assertEqual(payload["amount_received"], "400.00")
        self.assertEqual(payload["customer_name"], "Ali Raza")
        self.assertEqual(entry, self.sale)
        self.assertEqual(customer, self.customer)

    def test_invoice_arithmetic_reconciles(self):
        """Subtotal - Amount Received must equal the Balance shown.

        `record_sale` timestamps the linked payment 1ms after the sale, so the
        sale row's own balance_after predates that payment. Using it produced an
        invoice reading "Subtotal 3720, Received 1000, Balance 3720" — a
        document that visibly contradicts itself and overstates the debt.
        """
        payload, _, _ = build_payload_for(self.business, doc_type="invoice", target_id=self.sale.id)
        subtotal = Decimal(payload["subtotal"])
        received = Decimal(payload["amount_received"])
        balance = Decimal(payload["balance_after"])
        self.assertEqual(subtotal - received, balance)
        self.assertEqual(balance, Decimal("600.00"))

    def test_invoice_without_a_payment_shows_the_sale_balance(self):
        sale, payment = sales_services.record_sale(
            business=self.business, customer=self.customer,
            items=[{"item_name": "Oil", "quantity": Decimal("1"), "rate": Decimal("250")}],
        )
        self.assertIsNone(payment)
        payload, _, _ = build_payload_for(self.business, doc_type="invoice", target_id=sale.id)
        # Money is formatted to two decimals everywhere, including zero.
        self.assertEqual(payload["amount_received"], "0.00")
        self.assertEqual(Decimal(payload["balance_after"]), sale.balance_after)

    def test_receipt_requires_a_payment_entry(self):
        # Asking for a receipt against a sale must not silently produce one.
        with self.assertRaises(DocumentError):
            build_payload_for(self.business, doc_type="receipt", target_id=self.sale.id)
        payload, _, _ = build_payload_for(self.business, doc_type="receipt", target_id=self.payment.id)
        self.assertEqual(payload["amount"], "400.00")

    def test_another_businesss_entry_is_not_reachable(self):
        other_user = User.objects.create_user(username="b@x.com", email="b@x.com", password="pw")
        other = Business.objects.create(owner=other_user, business_name="Other Shop")
        with self.assertRaises(DocumentError):
            build_payload_for(other, doc_type="invoice", target_id=self.sale.id)


class DeliveryJobTests(DocumentTestMixin, TestCase):
    def setUp(self):
        self.build_fixtures()

    def test_successful_send_records_the_delivery(self):
        """Terminal success is "accepted" — WhatsApp took it. Baileys exposes no
        delivery receipt, so claiming more than that would mislead the owner."""
        delivery = self.make_delivery()
        with patch("apps.documents.delivery.render_document", return_value=(PNG_BYTES, "image")), \
             patch("apps.documents.delivery.gateway_client.send_media") as send:
            result = handle_document_send_job(self.make_job(delivery))

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, "accepted")
        self.assertEqual(delivery.delivered_format, "image")
        self.assertEqual(delivery.byte_size, len(PNG_BYTES))
        self.assertIsNotNone(delivery.accepted_at)
        self.assertEqual(result["status"], "accepted")

        # A bill goes out as an inline image, not an attachment.
        kwargs = send.call_args.kwargs
        self.assertEqual(kwargs["kind"], "image")
        self.assertTrue(kwargs["file_name"].endswith(".png"))
        self.assertEqual(kwargs["content"], PNG_BYTES)

    def test_pdf_is_sent_as_a_document_attachment(self):
        delivery = self.make_delivery(
            doc_type="statement", fmt="pdf", parameters={"customer_id": self.customer.id}
        )
        with patch("apps.documents.delivery.render_document", return_value=(PDF_BYTES, "pdf")), \
             patch("apps.documents.delivery.gateway_client.send_media") as send:
            handle_document_send_job(self.make_job(delivery))

        kwargs = send.call_args.kwargs
        self.assertEqual(kwargs["kind"], "document")
        self.assertTrue(kwargs["file_name"].endswith(".pdf"))

    def test_format_substitution_is_recorded_not_hidden(self):
        # An image too long to read is delivered as a PDF; the audit row must
        # show what the customer actually received.
        delivery = self.make_delivery(fmt="image")
        with patch("apps.documents.delivery.render_document", return_value=(PDF_BYTES, "pdf")), \
             patch("apps.documents.delivery.gateway_client.send_media") as send:
            handle_document_send_job(self.make_job(delivery))

        delivery.refresh_from_db()
        self.assertEqual(delivery.requested_format, "image")
        self.assertEqual(delivery.delivered_format, "pdf")
        self.assertEqual(send.call_args.kwargs["kind"], "document")

    def test_render_failure_is_recorded_and_nothing_is_sent(self):
        delivery = self.make_delivery()
        with patch("apps.documents.delivery.render_document",
                   side_effect=DocumentError("RENDER_UNAVAILABLE", "service down")), \
             patch("apps.documents.delivery.gateway_client.send_media") as send:
            result = handle_document_send_job(self.make_job(delivery))

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, "failed")
        self.assertEqual(delivery.error_code, "RENDER_UNAVAILABLE")
        self.assertEqual(result["status"], "failed")
        send.assert_not_called()

    def test_gateway_failure_is_recorded(self):
        from apps.whatsapp.gateway_client import GatewayError

        delivery = self.make_delivery()
        with patch("apps.documents.delivery.render_document", return_value=(PNG_BYTES, "image")), \
             patch("apps.documents.delivery.gateway_client.send_media",
                   side_effect=GatewayError(429, "RATE_LIMIT_EXCEEDED", "too fast")):
            handle_document_send_job(self.make_job(delivery))

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, "failed")
        self.assertEqual(delivery.error_code, "RATE_LIMIT_EXCEEDED")

    def test_a_delivery_cannot_be_sent_twice(self):
        # If the same job were processed twice, the customer must not receive
        # the document a second time.
        delivery = self.make_delivery()
        job = self.make_job(delivery)
        with patch("apps.documents.delivery.render_document", return_value=(PNG_BYTES, "image")), \
             patch("apps.documents.delivery.gateway_client.send_media") as send:
            handle_document_send_job(job)
            handle_document_send_job(job)

        self.assertEqual(send.call_count, 1)

    def test_nothing_is_written_to_disk(self):
        delivery = self.make_delivery()
        with tempfile.TemporaryDirectory() as tmp:
            before = set(os.listdir(tmp))
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with patch("apps.documents.delivery.render_document", return_value=(PNG_BYTES, "image")), \
                     patch("apps.documents.delivery.gateway_client.send_media"):
                    handle_document_send_job(self.make_job(delivery))
            finally:
                os.chdir(cwd)
            self.assertEqual(set(os.listdir(tmp)), before)


class SendEndpointTests(DocumentTestMixin, APITestCase):
    def setUp(self):
        self.build_fixtures()
        self.client.force_authenticate(user=self.user)

    def test_send_requires_the_whatsapp_send_feature(self):
        response = self.client.post(
            "/api/documents/send/", {"doc_type": "invoice", "target_id": self.sale.id}, format="json"
        )
        self.assertEqual(response.status_code, 403)

    def test_send_queues_a_job_and_creates_a_pending_delivery(self):
        self.enable_whatsapp_send()
        response = self.client.post(
            "/api/documents/send/",
            {"doc_type": "invoice", "target_id": self.sale.id, "format": "image"},
            format="json",
        )
        self.assertEqual(response.status_code, 202, response.content)
        data = response.json()["data"]
        delivery = DocumentDelivery.objects.get(pk=data["delivery"]["id"])
        self.assertEqual(delivery.status, "pending")
        # Recipient defaults to the customer the document belongs to.
        self.assertEqual(delivery.to_phone, "923001112222")
        self.assertTrue(JobTask.objects.filter(pk=data["job_id"], type="document_send").exists())

    def test_send_refuses_an_unsupported_format(self):
        # No doc_type/format combination is actually unsupported today (a
        # statement image is attempted and falls back to PDF via the
        # renderer's own over-length substitution, see
        # apps.documents.services.SUPPORTED_FORMATS) — this now exercises the
        # one input the serializer itself rejects: an unknown doc_type.
        self.enable_whatsapp_send()
        response = self.client.post(
            "/api/documents/send/",
            {"doc_type": "brochure", "customer_id": self.customer.id},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_send_allows_an_explicit_statement_image_request(self):
        # Statements default to PDF, but an explicit image request is
        # attempted rather than rejected outright — the renderer substitutes
        # PDF automatically if it doesn't fit, same mechanism already
        # governing invoice/receipt.
        self.enable_whatsapp_send()
        response = self.client.post(
            "/api/documents/send/",
            {"doc_type": "statement", "customer_id": self.customer.id, "format": "image"},
            format="json",
        )
        self.assertEqual(response.status_code, 202, response.content)

    def test_send_refuses_when_whatsapp_is_not_connected(self):
        self.enable_whatsapp_send()
        self.business.gateway_session_id = None
        self.business.save(update_fields=["gateway_session_id"])
        response = self.client.post(
            "/api/documents/send/", {"doc_type": "invoice", "target_id": self.sale.id}, format="json"
        )
        self.assertEqual(response.status_code, 409)

    def test_send_validates_the_target_before_queueing(self):
        # A bad target must fail immediately, not become a failed job the owner
        # has to go and discover later.
        self.enable_whatsapp_send()
        response = self.client.post(
            "/api/documents/send/", {"doc_type": "invoice", "target_id": 999999}, format="json"
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(JobTask.objects.filter(type="document_send").exists())

    def test_formats_endpoint_lists_defaults(self):
        response = self.client.get("/api/documents/formats/")
        data = response.json()["data"]
        self.assertEqual(data["invoice"]["default"], "image")
        self.assertEqual(data["statement"]["default"], "pdf")
        # Image is offered as an explicit, non-default option now — the owner
        # can still ask for one, they just aren't steered toward it.
        self.assertIn("image", data["statement"]["formats"])

    def test_render_returns_the_file_itself(self):
        with patch("apps.documents.views.render_document", return_value=(PNG_BYTES, "image")):
            response = self.client.post(
                "/api/documents/render/",
                {"doc_type": "invoice", "target_id": self.sale.id, "format": "image"},
                format="json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertEqual(response["X-Document-Format"], "image")
        self.assertEqual(response.content, PNG_BYTES)
