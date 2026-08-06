from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Business, User
from apps.billing.models import Plan, PlanFeature, Subscription
from apps.chat.models import ChatMessage, Conversation
from apps.customers.models import Customer
from apps.documents.models import DocumentDelivery
from apps.jobs.models import JobTask
from apps.sales import services as sales_services

from .capabilities import CAPABILITIES
from .executor import execute_plan
from .goals import GoalManager
from .models import AgentGoal
from .planner import PlanningError, compose_plan, plan_from_reply
from .recovery import with_retries
from .results import Clarification


class RegistryDangerousTierTests(TestCase):
    """The entire "dangerous tier" guarantee is that a capability which could
    delete/reverse/bulk-modify data is never registered — not a runtime
    check, an absence. This test is the guard against someone quietly adding
    one without noticing what that means."""

    def test_no_capability_can_delete_reverse_or_bulk_modify(self):
        risky_words = ("delete", "reverse", "bulk", "remove", "wipe")
        for name in CAPABILITIES:
            self.assertFalse(
                any(word in name for word in risky_words),
                f"capability {name!r} looks dangerous-tier and must not be registered",
            )

    def test_only_safe_and_financial_tiers_exist(self):
        tiers = {cap.risk_tier for cap in CAPABILITIES.values()}
        self.assertEqual(tiers, {"safe", "financial"})


class ComposePlanTests(TestCase):
    """Structural composition only — no database, no resolve() calls. Proves
    backward-chaining actually produces the dependency order the worked
    examples in the plan need, from metadata alone."""

    def test_composes_the_full_chain_for_an_entry_based_document(self):
        steps = compose_plan("generate_document_from_entry", {"doc_type": "invoice"})
        steps = steps + ["send_whatsapp_document"]
        self.assertIn("find_customer", steps)
        self.assertIn("find_latest_entry", steps)
        self.assertIn("choose_rendering_format", steps)
        self.assertIn("generate_document_from_entry", steps)
        self.assertIn("send_whatsapp_document", steps)
        # find_customer must precede find_latest_entry (which needs its
        # output), and both must precede the document step.
        self.assertLess(steps.index("find_customer"), steps.index("find_latest_entry"))
        self.assertLess(steps.index("find_latest_entry"), steps.index("generate_document_from_entry"))
        self.assertLess(steps.index("generate_document_from_entry"), steps.index("send_whatsapp_document"))

    def test_composes_the_shorter_chain_for_a_range_based_document(self):
        steps = compose_plan("generate_document_from_range", {"doc_type": "statement"})
        self.assertIn("find_customer", steps)
        self.assertNotIn("find_latest_entry", steps)  # a statement has no single "entry"
        self.assertIn("choose_rendering_format", steps)
        self.assertIn("generate_document_from_range", steps)

    def test_already_known_inputs_short_circuit_composition(self):
        # customer_id already given -> find_customer is not needed at all.
        steps = compose_plan("generate_document_from_range", {"doc_type": "statement", "customer_id": 1})
        self.assertNotIn("find_customer", steps)

    def test_unsatisfiable_requirement_raises_planning_error(self):
        with self.assertRaises(PlanningError):
            compose_plan("record_payment", {})  # "amount" has no producer capability


class PlannerResolutionTests(TestCase):
    """plan_from_reply walks the composed chain and resolves every step
    against real data — this is where ambiguous/impossible requests become a
    Clarification instead of a guess."""

    def setUp(self):
        self.user = User.objects.create_user(username="p@x.com", email="p@x.com", password="pw")
        self.business = Business.objects.create(
            owner=self.user, business_name="Test Shop", gateway_session_id="sess-1"
        )
        self.conversation = Conversation.objects.create(business=self.business)
        self.message = ChatMessage.objects.create(conversation=self.conversation, sender="ai", text="...")
        self.customer = Customer.objects.create(
            business=self.business, name="Ali", phone="923001112222",
            opening_balance=Decimal("0"), current_balance=Decimal("0"),
        )
        # A stored gateway_session_id only means a session was created at
        # some point, not that it's live right now — resolve time checks the
        # gateway directly (see capabilities._resolve_send_whatsapp_document).
        # Tests that want the "actually connected" happy path mock this;
        # test_a_stale_session_id_still_yields_a_clarification below covers
        # the case that motivated the check.
        patcher = mock.patch(
            "apps.whatsapp.gateway_client.get_status", return_value={"status": "CONNECTED"}
        )
        self.mock_get_status = patcher.start()
        self.addCleanup(patcher.stop)

    def _reply(self, **draft_document):
        return {"text": "...", "draft_document": {"doc_type": "statement", "summary": "s", **draft_document}}

    def test_none_returned_for_a_reply_with_no_draft_document(self):
        self.assertIsNone(plan_from_reply(self.business, self.conversation, self.message, {"text": "hi"}))

    def test_none_returned_for_unsupported_doc_type(self):
        # "report" isn't auto-composed yet — falls through to the existing
        # tap-confirm draft_document path, unchanged.
        reply = self._reply(doc_type="report", customer_id=None)
        self.assertIsNone(plan_from_reply(self.business, self.conversation, self.message, reply))

    def test_unresolvable_customer_yields_a_clarification_not_a_guess(self):
        reply = self._reply(customer_id=999999)
        result = plan_from_reply(self.business, self.conversation, self.message, reply)
        self.assertIsInstance(result, Clarification)

    def test_resolvable_statement_produces_a_full_resolved_step_list(self):
        reply = self._reply(customer_id=self.customer.id)
        result = plan_from_reply(self.business, self.conversation, self.message, reply)
        self.assertIsInstance(result, list)
        names = [s["capability"] for s in result]
        self.assertIn("generate_document_from_range", names)
        self.assertIn("send_whatsapp_document", names)
        self.assertEqual(names[-1], "send_whatsapp_document")

    def test_no_whatsapp_connected_yields_a_clarification(self):
        self.business.gateway_session_id = None
        self.business.save(update_fields=["gateway_session_id"])
        reply = self._reply(customer_id=self.customer.id)
        result = plan_from_reply(self.business, self.conversation, self.message, reply)
        self.assertIsInstance(result, Clarification)
        self.assertIn("WhatsApp", result.message)

    def test_customer_with_no_phone_yields_a_clarification(self):
        self.customer.phone = ""
        self.customer.save(update_fields=["phone"])
        reply = self._reply(customer_id=self.customer.id)
        result = plan_from_reply(self.business, self.conversation, self.message, reply)
        self.assertIsInstance(result, Clarification)

    def test_invoice_resend_with_no_sale_on_record_yields_a_clarification(self):
        reply = self._reply(doc_type="invoice", customer_id=self.customer.id)
        result = plan_from_reply(self.business, self.conversation, self.message, reply)
        self.assertIsInstance(result, Clarification)

    def test_a_stale_session_id_still_yields_a_clarification(self):
        # The real bug report this guards against: the owner unlinked
        # WhatsApp from their phone's own Linked Devices menu, which ends
        # the session on the gateway side without ever clearing
        # business.gateway_session_id in Django — so the field alone is not
        # proof of a live connection, only a live status check is.
        self.mock_get_status.return_value = {"status": "DISCONNECTED"}
        reply = self._reply(customer_id=self.customer.id)
        result = plan_from_reply(self.business, self.conversation, self.message, reply)
        self.assertIsInstance(result, Clarification)
        self.assertIn("WhatsApp", result.message)

    def test_gateway_unreachable_yields_a_clarification_not_a_crash(self):
        from apps.whatsapp.gateway_client import GatewayError

        self.mock_get_status.side_effect = GatewayError(503, "GATEWAY_UNREACHABLE", "down")
        reply = self._reply(customer_id=self.customer.id)
        result = plan_from_reply(self.business, self.conversation, self.message, reply)
        self.assertIsInstance(result, Clarification)


class ExecutePlanTests(TestCase):
    """The Executor: runs a resolved plan, creates/updates one AgentGoal,
    and never marks the terminal WhatsApp-send step "done" before the async
    job actually resolves it."""

    def setUp(self):
        self.user = User.objects.create_user(username="e@x.com", email="e@x.com", password="pw")
        self.business = Business.objects.create(
            owner=self.user, business_name="Test Shop", gateway_session_id="sess-1"
        )
        self.conversation = Conversation.objects.create(business=self.business)
        self.message = ChatMessage.objects.create(conversation=self.conversation, sender="ai", text="...")
        self.customer = Customer.objects.create(
            business=self.business, name="Ali", phone="923001112222",
            opening_balance=Decimal("0"), current_balance=Decimal("0"),
        )
        plan = Plan.objects.create(name="Pro", price_pkr=Decimal("1000"))
        PlanFeature.objects.create(plan=plan, feature_key="whatsapp_send", enabled=True)
        Subscription.objects.create(business=self.business, plan=plan, status="active", started_at=timezone.now())
        patcher = mock.patch(
            "apps.whatsapp.gateway_client.get_status", return_value={"status": "CONNECTED"}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_fully_resolved_statement_send_queues_a_delivery_and_job(self):
        reply = {"text": "...", "draft_document": {"doc_type": "statement", "summary": "s", "customer_id": self.customer.id}}
        steps = plan_from_reply(self.business, self.conversation, self.message, reply)
        self.assertIsInstance(steps, list)

        outcome = execute_plan(business=self.business, conversation=self.conversation, message=self.message, steps=steps)
        self.assertTrue(outcome.success)
        self.assertIsNotNone(outcome.pending_delivery_id)
        self.assertTrue(DocumentDelivery.objects.filter(pk=outcome.pending_delivery_id).exists())
        self.assertTrue(JobTask.objects.filter(type="document_send").exists())

        goal = AgentGoal.objects.get(conversation=self.conversation)
        self.assertEqual(goal.status, "awaiting_verification")
        # Never claim success before the job resolves it.
        self.assertNotEqual(goal.status, "done")

    def test_goal_manager_closes_the_goal_only_once_the_event_arrives(self):
        reply = {"text": "...", "draft_document": {"doc_type": "statement", "summary": "s", "customer_id": self.customer.id}}
        steps = plan_from_reply(self.business, self.conversation, self.message, reply)
        outcome = execute_plan(business=self.business, conversation=self.conversation, message=self.message, steps=steps)
        goal = AgentGoal.objects.get(conversation=self.conversation)
        self.assertEqual(goal.status, "awaiting_verification")

        GoalManager.handle_event("document_delivery_accepted", {"delivery_id": outcome.pending_delivery_id})
        goal.refresh_from_db()
        self.assertEqual(goal.status, "done")
        self.assertEqual(goal.plan[-1]["status"], "done")

    def test_goal_manager_marks_failed_on_a_failed_event(self):
        reply = {"text": "...", "draft_document": {"doc_type": "statement", "summary": "s", "customer_id": self.customer.id}}
        steps = plan_from_reply(self.business, self.conversation, self.message, reply)
        outcome = execute_plan(business=self.business, conversation=self.conversation, message=self.message, steps=steps)

        GoalManager.handle_event("document_delivery_failed", {"delivery_id": outcome.pending_delivery_id})
        goal = AgentGoal.objects.get(conversation=self.conversation)
        self.assertEqual(goal.status, "failed")


class RecoveryTests(TestCase):
    def test_retries_up_to_max_attempts_on_a_retryable_error(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 2:
                raise ConnectionError("boom")
            return "ok"

        result = with_retries(flaky, max_attempts=2, retryable=(ConnectionError,), delay_seconds=0)
        self.assertEqual(result, "ok")
        self.assertEqual(calls["n"], 2)

    def test_gives_up_after_max_attempts(self):
        def always_fails():
            raise ConnectionError("boom")

        with self.assertRaises(ConnectionError):
            with_retries(always_fails, max_attempts=2, retryable=(ConnectionError,), delay_seconds=0)

    def test_should_retry_filter_narrows_beyond_exception_type(self):
        # Only retry a specific "code" attribute, e.g. GATEWAY_UNREACHABLE —
        # never a blanket retry on every GatewayError, per the ban-risk memory.
        class FakeGatewayError(Exception):
            def __init__(self, code):
                self.code = code

        calls = {"n": 0}

        def raises_wrong_code():
            calls["n"] += 1
            raise FakeGatewayError("SOME_OTHER_CODE")

        with self.assertRaises(FakeGatewayError):
            with_retries(
                raises_wrong_code, max_attempts=3, retryable=(FakeGatewayError,), delay_seconds=0,
                should_retry=lambda exc: exc.code == "GATEWAY_UNREACHABLE",
            )
        self.assertEqual(calls["n"], 1)  # never retried — wrong code


class InvoiceLatestEntryChainTests(TestCase):
    """The owner's own worked example: "send Ali's last invoice" with no
    entry id given anywhere — resolved entirely from customer + doc_type."""

    def setUp(self):
        self.user = User.objects.create_user(username="i@x.com", email="i@x.com", password="pw")
        self.business = Business.objects.create(
            owner=self.user, business_name="Test Shop", gateway_session_id="sess-1"
        )
        self.conversation = Conversation.objects.create(business=self.business)
        self.message = ChatMessage.objects.create(conversation=self.conversation, sender="ai", text="...")
        self.customer = Customer.objects.create(
            business=self.business, name="Ali", phone="923001112222",
            opening_balance=Decimal("0"), current_balance=Decimal("0"),
        )
        sales_services.record_sale(
            business=self.business, customer=self.customer,
            items=[{"item_name": "Rice", "quantity": Decimal("1"), "rate": Decimal("500")}],
        )
        plan = Plan.objects.create(name="Pro", price_pkr=Decimal("1000"))
        PlanFeature.objects.create(plan=plan, feature_key="whatsapp_send", enabled=True)
        Subscription.objects.create(business=self.business, plan=plan, status="active", started_at=timezone.now())
        patcher = mock.patch(
            "apps.whatsapp.gateway_client.get_status", return_value={"status": "CONNECTED"}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_resolves_the_latest_sale_with_no_entry_id_given(self):
        reply = {"text": "...", "draft_document": {"doc_type": "invoice", "summary": "s", "customer_id": self.customer.id}}
        steps = plan_from_reply(self.business, self.conversation, self.message, reply)
        self.assertIsInstance(steps, list)
        find_latest = next(s for s in steps if s["capability"] == "find_latest_entry")
        self.assertIn("entry_id", find_latest["resolved"])
