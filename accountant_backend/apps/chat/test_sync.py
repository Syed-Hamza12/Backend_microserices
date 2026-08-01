"""Multi-device synchronisation and chat-history deletion.

Kept separate from `tests.py` (AI draft handling) because these are about a
different property: two phones signed into one business converging on server
state, and history deletion reaching every device.
"""

from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.accounts.models import Business, User
from apps.customers.models import Customer
from apps.sales import services as sales_services
from apps.sales.models import ActivityEntry, EntryChangeLog

from .models import ChatMessage, ChatSyncState, Conversation

DRAFT_BILL = {"total_amount": 1000, "payment_received": 0, "previous_balance": 0}


class ChatTestCase(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="m@x.com", email="m@x.com", password="pw")
        self.business = Business.objects.create(owner=self.user, business_name="Hamza Traders")
        self.customer = Customer.objects.create(
            business=self.business, name="Ali", phone="923000000000",
            opening_balance=Decimal("0"), current_balance=Decimal("0"),
        )
        self.conversation = Conversation.objects.create(business=self.business)
        self.client.force_authenticate(user=self.user)

    def device(self):
        """An independent client with its own cursor — a second phone."""
        client = APIClient()
        client.force_authenticate(user=self.user)
        return client

    def sync(self, client, cursor=None):
        params = {"updated_after": cursor} if cursor else {}
        return client.get(
            f"/api/chat/conversations/{self.conversation.id}/messages/", params
        ).json()["data"]

    def message(self, sender="owner", text="hi", **kwargs):
        return ChatMessage.objects.create(
            conversation=self.conversation, sender=sender, text=text, **kwargs
        )

    def draft_message(self):
        return ChatMessage.objects.create(
            conversation=self.conversation,
            sender="ai",
            text="Draft ready",
            draft_bill={**DRAFT_BILL, "customer_id": str(self.customer.id)},
        )


class MultiDeviceSyncTests(ChatTestCase):
    def test_a_message_created_on_one_device_reaches_the_other(self):
        device_a, device_b = self.device(), self.device()
        self.message(text="from A")

        cursor_b = self.sync(device_b)["next_cursor"]
        self.message(sender="ai", text="reply")

        delta = self.sync(device_b, cursor_b)
        self.assertIn("reply", [m["text"] for m in delta["messages"]])
        self.assertEqual(len(self.sync(device_a)["messages"]), 2)

    def test_both_devices_converge_on_the_same_state(self):
        device_a, device_b = self.device(), self.device()
        self.message(text="one")
        cursor_a = self.sync(device_a)["next_cursor"]
        cursor_b = self.sync(device_b)["next_cursor"]

        self.message(sender="ai", text="two")
        self.message(text="three")

        state_a = {m["id"]: m["text"] for m in self.sync(device_a, cursor_a)["messages"]}
        state_b = {m["id"]: m["text"] for m in self.sync(device_b, cursor_b)["messages"]}
        self.assertEqual(state_a, state_b)

    def test_a_draft_confirmed_on_one_device_propagates_to_the_other(self):
        """The conflict that actually matters: stale draft state elsewhere."""
        device_b = self.device()
        message = self.draft_message()
        cursor_b = self.sync(device_b)["next_cursor"]

        confirmed = self.device().post(f"/api/chat/draft/{message.id}/confirm/")
        self.assertEqual(confirmed.status_code, 200, confirmed.content)

        delta = self.sync(device_b, cursor_b)
        returned = {m["id"]: m for m in delta["messages"]}
        self.assertIn(message.id, returned)
        self.assertTrue(returned[message.id]["draft_confirmed"])

    def test_a_stale_device_cannot_confirm_an_already_confirmed_draft(self):
        """Server state wins — a second tap must not record the sale twice."""
        message = self.draft_message()

        first = self.device().post(f"/api/chat/draft/{message.id}/confirm/")
        # Device B still shows the draft as pending, and the owner taps Confirm.
        second = self.device().post(f"/api/chat/draft/{message.id}/confirm/")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 400)
        self.assertEqual(second.json()["error"]["code"], "ALREADY_CONFIRMED")
        self.assertEqual(
            ActivityEntry.objects.filter(business=self.business, type="sale").count(), 1
        )

    def test_an_edit_on_one_device_is_not_lost_by_the_others_cursor(self):
        """Two devices editing state must both end at the server's version."""
        device_a, device_b = self.device(), self.device()
        message = self.message(sender="ai", text="original")
        cursor_a = self.sync(device_a)["next_cursor"]
        cursor_b = self.sync(device_b)["next_cursor"]

        message.text = "edited"
        message.save(update_fields=["text", "updated_at"])

        for client, cursor in ((device_a, cursor_a), (device_b, cursor_b)):
            returned = {m["id"]: m["text"] for m in self.sync(client, cursor)["messages"]}
            self.assertEqual(returned.get(message.id), "edited")

    def test_a_conversation_started_on_one_device_appears_on_the_other(self):
        device_b = self.device()
        before = len(device_b.get("/api/chat/conversations/").json()["data"])

        Conversation.objects.create(business=self.business)

        after = device_b.get("/api/chat/conversations/").json()["data"]
        self.assertEqual(len(after), before + 1)

    def test_sync_state_reports_server_time_not_client_time(self):
        data = self.device().get("/api/chat/sync-state/").json()["data"]
        self.assertIsNotNone(data["server_time"])
        # Every cursor in this system originates server-side, so devices never
        # have to trust their own clocks.
        self.assertIsNone(data["history_cleared_at"])


class ClearChatHistoryTests(ChatTestCase):
    def setUp(self):
        super().setUp()
        self.message(text="hello")
        self.message(sender="ai", text="hi there")

    def test_clearing_removes_conversations_and_messages(self):
        response = self.client.delete("/api/chat/history/")
        self.assertEqual(response.status_code, 200)

        data = response.json()["data"]
        self.assertEqual(data["deleted_conversations"], 1)
        self.assertEqual(data["deleted_messages"], 2)
        self.assertEqual(Conversation.objects.filter(business=self.business).count(), 0)
        self.assertEqual(ChatMessage.objects.count(), 0)

    def test_clearing_records_a_marker_other_devices_can_see(self):
        state = self.client.get("/api/chat/sync-state/").json()["data"]
        self.assertIsNone(state["history_cleared_at"])

        self.client.delete("/api/chat/history/")

        state = self.client.get("/api/chat/sync-state/").json()["data"]
        self.assertIsNotNone(state["history_cleared_at"])
        self.assertEqual(state["conversation_count"], 0)

    def test_a_second_device_learns_history_was_cleared(self):
        device_b = self.device()
        before = device_b.get("/api/chat/sync-state/").json()["data"]["history_cleared_at"]

        self.client.delete("/api/chat/history/")

        after = device_b.get("/api/chat/sync-state/").json()["data"]
        self.assertNotEqual(after["history_cleared_at"], before)
        # Delta sync alone could never tell device B this: that endpoint only
        # ever returns rows that still exist.
        self.assertEqual(after["conversation_count"], 0)

    def test_clearing_never_touches_the_ledger(self):
        """Chat is a conversation record; sales are money."""
        sale, _ = sales_services.record_sale(
            business=self.business,
            customer=self.customer,
            items=[{"item_name": "Rice", "quantity": Decimal("1"), "rate": Decimal("500")}],
            created_by="ai_chat",
        )
        sales_services.log_ai_created_sale(entry=sale, source_message_id=123)

        self.client.delete("/api/chat/history/")

        self.assertTrue(ActivityEntry.objects.filter(pk=sale.id).exists())
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.current_balance, Decimal("500"))
        # The record of where that sale came from survives too.
        self.assertTrue(
            EntryChangeLog.objects.filter(entry_id=sale.id, action="create").exists()
        )

    def test_clearing_only_affects_the_callers_business(self):
        other_user = User.objects.create_user(username="o@x.com", email="o@x.com", password="pw")
        other = Business.objects.create(owner=other_user, business_name="Other Shop")
        theirs = Conversation.objects.create(business=other)
        ChatMessage.objects.create(conversation=theirs, sender="owner", text="theirs")

        self.client.delete("/api/chat/history/")

        self.assertTrue(Conversation.objects.filter(pk=theirs.id).exists())
        self.assertEqual(ChatMessage.objects.filter(conversation=theirs).count(), 1)
        self.assertIsNone(ChatSyncState.cleared_at_for(other))

    def test_clearing_twice_is_harmless_and_advances_the_marker(self):
        self.client.delete("/api/chat/history/")
        first = self.client.get("/api/chat/sync-state/").json()["data"]["history_cleared_at"]

        response = self.client.delete("/api/chat/history/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["deleted_conversations"], 0)

        second = self.client.get("/api/chat/sync-state/").json()["data"]["history_cleared_at"]
        # A device offline across both wipes still learns it must reset.
        self.assertGreaterEqual(second, first)

    def test_new_conversations_work_normally_after_clearing(self):
        self.client.delete("/api/chat/history/")

        fresh = Conversation.objects.create(business=self.business)
        ChatMessage.objects.create(conversation=fresh, sender="owner", text="starting over")

        data = self.client.get(f"/api/chat/conversations/{fresh.id}/messages/").json()["data"]
        self.assertEqual([m["text"] for m in data["messages"]], ["starting over"])

    def test_a_queued_image_job_survives_its_conversation_being_deleted(self):
        """A job in flight when history is cleared must fail cleanly."""
        from apps.image_info_extractor.models import ExtractionJob
        from apps.image_info_extractor.services import handle_image_extract_job
        from apps.jobs.models import JobTask

        job = JobTask.objects.create(
            business=self.business,
            type="image_extract",
            payload={"conversation_id": self.conversation.id},
        )
        ExtractionJob.objects.create(
            business=self.business, job_task=job, source_image_url="/media/uploads/x.jpg",
            status="pending",
        )

        self.client.delete("/api/chat/history/")

        # Previously raised Conversation.DoesNotExist out of the worker.
        result = handle_image_extract_job(job)
        self.assertEqual(result["status"], "abandoned")


class SyncStateIsolationTests(APITestCase):
    def test_sync_state_requires_a_business(self):
        user = User.objects.create_user(username="nb@x.com", email="nb@x.com", password="pw")
        client = APIClient()
        client.force_authenticate(user=user)

        self.assertEqual(client.get("/api/chat/sync-state/").status_code, 404)
        self.assertEqual(client.delete("/api/chat/history/").status_code, 404)

    def test_sync_state_is_per_business(self):
        user_a = User.objects.create_user(username="a@x.com", email="a@x.com", password="pw")
        business_a = Business.objects.create(owner=user_a, business_name="A")
        user_b = User.objects.create_user(username="b@x.com", email="b@x.com", password="pw")
        Business.objects.create(owner=user_b, business_name="B")

        ChatSyncState.objects.create(business=business_a, history_cleared_at=timezone.now())

        client_b = APIClient()
        client_b.force_authenticate(user=user_b)
        self.assertIsNone(client_b.get("/api/chat/sync-state/").json()["data"]["history_cleared_at"])
