import logging
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Count, F, Max
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import Business
from apps.billing.permissions import HasFeature
from apps.billing.services import enforce_feature_gate
from apps.customers.models import Customer
from apps.customers.phone import normalize_phone
from apps.documents import services as document_services
from apps.image_info_extractor import matching
from apps.sales import services as sales_services
from apps.sales.models import ActivityEntry
from apps.sales.business_date import BusinessDateError, resolve as resolve_business_date, to_entry_timestamp
from apps.sales.serializers import MAX_MONEY, MIN_MONEY

from . import services
from .models import ChatMessage, ChatSyncState, Conversation
from .serializers import ChatMessageSerializer, ConversationSerializer, DraftBillSerializer

logger = logging.getLogger(__name__)

# Cap on how many line items a single AI draft may carry into the ledger.
MAX_DRAFT_ITEMS = 50

# Longest chat message accepted. Comfortably above any real bookkeeping
# instruction, far below what makes prompt costs or injection surface a problem.
MAX_CHAT_TEXT_LENGTH = 4000


def _release_draft_claim(message):
    """Undoes the `draft_confirmed` claim when the confirm didn't go through, so
    the owner can fix the draft and try again instead of being left with a
    draft the UI thinks was already sent."""
    ChatMessage.objects.filter(pk=message.pk).update(draft_confirmed=False)
    message.draft_confirmed = False


def _money(value, field_name):
    """Converts a model-supplied number to Decimal and range-checks it.

    Draft amounts arrive as JSON floats produced by a language model. They were
    previously passed into the ledger exactly as received — so a hallucinated,
    negative, absurdly large, or non-numeric value became a real entry, and a
    float's binary rounding error became the stored amount. Anything that fails
    these checks is a draft the owner has to correct, not money to record.
    """
    if value is None:
        raise ValueError(f"The draft is missing {field_name}.")
    try:
        # Via str() so a float like 0.1 becomes Decimal("0.1") rather than its
        # binary approximation.
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"The draft's {field_name} isn't a valid number.")
    if amount < 0:
        raise ValueError(f"The draft's {field_name} can't be negative.")
    if amount > MAX_MONEY:
        raise ValueError(f"The draft's {field_name} is unrealistically large.")
    return amount


def build_sale_from_draft(draft):
    """Turns a stored `draft_bill` into validated (items, amount_received, date).

    Keeps the real line items when the draft has them. The previous version
    collapsed every AI sale into one row literally named "AI Chat Draft Bill"
    with the whole total as its rate, so any invoice later generated from that
    sale showed a customer that placeholder instead of what they bought.
    """
    total = _money(draft.get("total_amount"), "total")
    payment_received = _money(draft.get("payment_received") or 0, "payment received")

    # The accounting date is resolved here, server-side, against the business
    # calendar — never taken as the model computed it. A model that fumbles
    # "yesterday" would otherwise put a sale on the wrong day of the ledger and
    # silently shift every balance after it.
    try:
        business_date = resolve_business_date(draft.get("date"))
    except BusinessDateError as exc:
        raise ValueError(str(exc))

    if total < MIN_MONEY:
        raise ValueError("The draft's total must be greater than zero.")
    if payment_received > total:
        raise ValueError("The payment received can't be more than the total.")

    raw_items = draft.get("items") or []
    if not isinstance(raw_items, list):
        raise ValueError("The draft's items aren't in a readable format.")
    if len(raw_items) > MAX_DRAFT_ITEMS:
        raise ValueError(f"A draft can't have more than {MAX_DRAFT_ITEMS} items.")

    items = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise ValueError("The draft's items aren't in a readable format.")
        name = str(raw.get("item_name") or "").strip()[:255]
        if not name:
            continue
        quantity = _money(raw.get("quantity"), f"quantity for {name}")
        rate = _money(raw.get("rate"), f"rate for {name}")
        if quantity <= 0 or rate <= 0:
            raise ValueError(f"The quantity and rate for {name} must be greater than zero.")
        items.append({"item_name": name, "quantity": quantity, "rate": rate})

    if items:
        itemised_total = sum(item["quantity"] * item["rate"] for item in items)
        if itemised_total.quantize(Decimal("0.01")) != total:
            # The model's own arithmetic disagreeing with its own line items is
            # exactly the case where guessing which figure it "meant" would put
            # a wrong number on someone's ledger.
            raise ValueError("The draft's items don't add up to its total — please edit it before confirming.")
    else:
        # No usable line items: fall back to a single row, but labelled with
        # something the customer can actually read on an invoice.
        items = [{"item_name": "Sale", "quantity": Decimal("1"), "rate": total}]

    return items, payment_received, business_date


class UpdateDraftBillView(APIView):
    """Replaces the stored `draft_bill` after the owner edits it in the app.

    Confirming a draft records the sale from the copy stored here, and the app
    had no way to write its edits back — so an owner who corrected the total,
    the items or the date on the Edit Draft screen still had the AI's original
    figures posted to the ledger, silently. The edit screen was decorative.

    The draft is re-validated through the same `build_sale_from_draft` used at
    confirm time, so a draft that cannot be recorded is rejected here, while the
    owner is still looking at it, rather than at confirm time.

    Not gated on `ai_chat`, matching ConfirmDraftBillView: the AI call that cost
    a quota slot already happened. Gating this would mean a plan expiring
    between drafting and confirming left the owner able to post the draft but
    not to correct it first.
    """

    permission_classes = [IsAuthenticated]

    def patch(self, request, message_id):
        try:
            business = request.user.business
        except Business.DoesNotExist:
            return Response(
                {"success": False, "error": {"code": "NO_BUSINESS", "message": "No business created yet."}},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            message = ChatMessage.objects.get(pk=message_id, conversation__business=business, sender="ai")
        except ChatMessage.DoesNotExist:
            return Response(
                {"success": False, "error": {"code": "NOT_FOUND", "message": "Draft bill message not found."}},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not message.draft_bill:
            return Response(
                {"success": False, "error": {"code": "NO_DRAFT_BILL", "message": "This message has no draft bill."}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # A sale already on the ledger must not be rewritten through the draft
        # that produced it — that is what the edit-entry flow is for.
        if message.draft_confirmed:
            return Response(
                {"success": False, "error": {"code": "ALREADY_CONFIRMED", "message": "This draft was already sent."}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = DraftBillSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        draft = dict(serializer.validated_data)
        draft["customer_name_guess"] = message.draft_bill.get("customer_name_guess")

        # The customer IS settable here, and has to be: a draft the AI could not
        # match to anyone is rejected at confirm time with "edit it and pick one
        # first", and this endpoint is the only place that edit can happen. A
        # draft is a proposal, not a ledger entry — nothing has been recorded
        # yet, so pointing it at the right customer is an edit like any other.
        # (Re-pointing an already-recorded sale is still a different operation,
        # handled by the transfer_entry flow.)
        #
        # The id is validated against this business rather than trusted: it
        # arrives from the client, and an unchecked value would attach a draft
        # to another business's customer.
        requested_customer_id = (request.data or {}).get("customer_id")
        matched = None
        if requested_customer_id:
            try:
                matched = Customer.objects.filter(
                    business=business, pk=int(requested_customer_id)
                ).first()
            except (TypeError, ValueError):
                # A non-numeric id is a malformed request, not a customer —
                # `pk=<garbage>` would raise straight out of the view.
                matched = None
        if matched:
            draft["customer_id"] = str(matched.id)
            draft["customer_name_guess"] = None
        else:
            draft["customer_id"] = message.draft_bill.get("customer_id")

        # The displayed name is re-derived from the id, never trusted from the
        # client, and an unmatched draft still gets a last matching attempt.
        services.link_drafted_customer(business, draft)

        try:
            build_sale_from_draft(draft)
        except ValueError as exc:
            return Response(
                {"success": False, "error": {"code": "INVALID_DRAFT", "message": str(exc)}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        message.draft_bill = draft
        message.save(update_fields=["draft_bill"])
        return Response({"success": True, "data": ChatMessageSerializer(message).data})


class TransliterateView(APIView):
    """Urdu script -> Roman Urdu for a dictated message.

    Android's ur-PK speech recogniser only ever returns native Urdu script, so
    an owner who chose Roman Urdu saw their own dictated words come back in a
    script they had explicitly not picked. The app calls this after capture, in
    Roman Urdu mode only, before showing the text.

    Deliberately not behind `ai_chat`: this is presentation of the owner's own
    words, and gating it would mean a Basic-plan owner's voice input silently
    changing script depending on their subscription.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        text = request.data.get("text")
        if not text or not str(text).strip():
            return Response(
                {"success": False, "error": {"code": "VALIDATION_ERROR", "message": "text is required."}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        converted = services.transliterate_to_roman_urdu(str(text)[:services.MAX_TRANSLITERATE_CHARS])
        return Response({"success": True, "data": {"text": converted}})


class SendChatMessageView(APIView):
    permission_classes = [IsAuthenticated, HasFeature]
    required_feature = "ai_chat"

    def post(self, request):
        try:
            business = request.user.business
        except Business.DoesNotExist:
            return Response(
                {"success": False, "error": {"code": "NO_BUSINESS", "message": "No business created yet."}},
                status=status.HTTP_404_NOT_FOUND,
            )

        text = request.data.get("text")
        if not text or not str(text).strip():
            return Response(
                {"success": False, "error": {"code": "VALIDATION_ERROR", "message": "text is required."}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        text = str(text)
        # Unbounded input went straight into the model prompt: a long paste
        # meant a large bill on every subsequent turn (it stays in history) and
        # gave far more room to bury instructions aimed at the assistant.
        if len(text) > MAX_CHAT_TEXT_LENGTH:
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": "MESSAGE_TOO_LONG",
                        "message": f"Messages are limited to {MAX_CHAT_TEXT_LENGTH} characters.",
                    },
                },
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

        # Feature-gate + usage-cap check happens here, before the (paid) Groq call.
        enforce_feature_gate(business, "ai_chat")

        # The language the owner has selected *right now*, sent with each
        # message. `business.language` is only ever written at Create Business,
        # and the app's Settings language switch was local-only — so an owner
        # who changed to English or Urdu afterwards kept getting replies in
        # whichever language they picked when they first signed up.
        language = services.resolve_language(request.data.get("language"), business)

        ai_message = services.generate_reply(
            business=business, conversation=conversation, text=text, language=language
        )

        return Response(
            {
                "success": True,
                "data": {
                    "conversationId": conversation.id,
                    "reply": ChatMessageSerializer(ai_message).data,
                },
            }
        )


class ConfirmDraftBillView(APIView):
    """The only endpoint that actually writes a Draft Bill to the ledger — "Edit Draft"/"Save
    Draft" is chat-local only and never calls the backend at all, per
    BACKEND_INTEGRATION_GUIDE.md Section 7b. Not feature-gated by ai_chat: this is the same
    sales-recording path as a manual entry (business_logic.md Section 6), so a business that
    somehow loses ai_chat mid-conversation can still confirm an already-drafted bill."""

    def post(self, request, message_id):
        try:
            business = request.user.business
        except Business.DoesNotExist:
            return Response(
                {"success": False, "error": {"code": "NO_BUSINESS", "message": "No business created yet."}},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            message = ChatMessage.objects.get(pk=message_id, conversation__business=business, sender="ai")
        except ChatMessage.DoesNotExist:
            return Response(
                {"success": False, "error": {"code": "NOT_FOUND", "message": "Draft bill message not found."}},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not message.draft_bill:
            return Response(
                {"success": False, "error": {"code": "NO_DRAFT_BILL", "message": "This message has no draft bill."}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Claim the draft before recording anything. Checking `draft_confirmed`
        # and setting it after the sale was written left a window wide enough
        # for a double tap on a slow connection to record the same sale twice —
        # real, duplicated money on the customer's ledger. This conditional
        # UPDATE lets exactly one request through; the flag is released again
        # below if the save itself fails.
        claimed = ChatMessage.objects.filter(pk=message.pk, draft_confirmed=False).update(draft_confirmed=True)
        if not claimed:
            return Response(
                {"success": False, "error": {"code": "ALREADY_CONFIRMED", "message": "This draft was already sent."}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        message.draft_confirmed = True

        draft = message.draft_bill
        # Drafts created before the name-matching fallback existed (and any the
        # model still leaves unlinked) can otherwise never be confirmed — the
        # owner taps a button that fails identically every time. Resolving the
        # guessed name here uses the same conservative matcher, so an ambiguous
        # name still falls through to the error below rather than picking a
        # ledger on the owner's behalf.
        if not draft.get("customer_id"):
            services.link_drafted_customer(business, draft)
            if draft.get("customer_id"):
                message.draft_bill = draft
                message.save(update_fields=["draft_bill"])

        customer_id = draft.get("customer_id")
        try:
            customer = Customer.objects.get(business=business, pk=customer_id)
        except (Customer.DoesNotExist, ValueError, TypeError):
            _release_draft_claim(message)
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": "CUSTOMER_NOT_MATCHED",
                        "message": "This draft isn't linked to a real customer yet — edit it and pick one first.",
                    },
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            items, payment_received, business_date = build_sale_from_draft(draft)
        except ValueError as exc:
            _release_draft_claim(message)
            return Response(
                {"success": False, "error": {"code": "INVALID_DRAFT", "message": str(exc)}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            sale_entry, payment_entry = sales_services.record_sale(
                business=business,
                customer=customer,
                items=items,
                amount_received=payment_received,
                payment_method="cash" if payment_received else None,
                # None means "today"; record_sale falls back to now().
                date=to_entry_timestamp(business_date) if business_date else None,
                created_by="ai_chat",
            )
        except Exception:  # noqa: BLE001 - draft must stay editable/unconfirmed on any save failure
            _release_draft_claim(message)
            return Response(
                {"success": False, "error": {"code": "SAVE_FAILED", "message": "Could not save the sale. Please try again."}},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # Every AI-created sale gets an audit row, the same way AI-initiated
        # edits and transfers already did. "The AI put this on my ledger" has
        # to be answerable after the fact for creations too, not just changes.
        sales_services.log_ai_created_sale(entry=sale_entry, source_message_id=message.id)

        # The button has always read "Confirm and Send" / "Confirm karein &
        # Bhejein", and until now it only ever did the first half — the sale
        # landed on the ledger and nothing was sent to anyone. Queued strictly
        # AFTER the sale is safely recorded, and deliberately non-fatal: a
        # delivery that can't be attempted (WhatsApp not connected, customer has
        # no number, quota spent) must not fail a confirm whose money is already
        # written. `delivery` tells the app which half happened.
        delivery = document_services.queue_invoice_send(business, sale_entry, customer)

        return Response(
            {
                "success": True,
                "data": {
                    "message": ChatMessageSerializer(message).data,
                    "sale_id": sale_entry.id,
                    "payment_id": payment_entry.id if payment_entry else None,
                    "delivery": delivery,
                },
            }
        )


class ConfirmDraftActionView(APIView):
    """Confirms an AI-proposed edit or transfer of an *existing* entry
    (`draft_action` — see BACKEND_INTEGRATION_GUIDE.md's chat section and
    apps.sales.services.edit_entry_via_ai/transfer_entry). Same
    not-feature-gated reasoning as ConfirmDraftBillView: this is the same
    risk level as a manual edit, just AI-assisted. Every field the AI
    claimed (entry_id, customer_id) is re-validated against this business's
    real data here — never trusted blindly from the stored draft_action."""

    def post(self, request, message_id):
        try:
            business = request.user.business
        except Business.DoesNotExist:
            return Response(
                {"success": False, "error": {"code": "NO_BUSINESS", "message": "No business created yet."}},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            message = ChatMessage.objects.get(pk=message_id, conversation__business=business, sender="ai")
        except ChatMessage.DoesNotExist:
            return Response(
                {"success": False, "error": {"code": "NOT_FOUND", "message": "Message not found."}},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not message.draft_action:
            return Response(
                {"success": False, "error": {"code": "NO_DRAFT_ACTION", "message": "This message has no draft action."}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Same single-claim guard as ConfirmDraftBillView: without it a double
        # tap applied the same AI-proposed edit twice, and the second undo
        # token silently replaced the first.
        claimed = ChatMessage.objects.filter(pk=message.pk, draft_confirmed=False).update(draft_confirmed=True)
        if not claimed:
            return Response(
                {"success": False, "error": {"code": "ALREADY_CONFIRMED", "message": "This action was already confirmed."}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        message.draft_confirmed = True

        draft = message.draft_action
        try:
            entry = ActivityEntry.objects.get(
                pk=draft.get("entry_id"), business=business, customer_id=draft.get("customer_id")
            )
        except (ActivityEntry.DoesNotExist, ValueError, TypeError):
            _release_draft_claim(message)
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": "ENTRY_NOT_MATCHED",
                        "message": "That record couldn't be found anymore — it may have already changed.",
                    },
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            if draft["action_type"] == "transfer_entry":
                try:
                    new_customer = Customer.objects.get(business=business, pk=draft.get("target_customer_id"))
                except (Customer.DoesNotExist, ValueError, TypeError):
                    _release_draft_claim(message)
                    return Response(
                        {
                            "success": False,
                            "error": {"code": "CUSTOMER_NOT_MATCHED", "message": "Target customer not found."},
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                entry, pending_undo = sales_services.transfer_entry(entry=entry, new_customer=new_customer)
            else:
                entry, pending_undo = sales_services.edit_entry_via_ai(entry=entry, changes=draft.get("changes") or {})
        except ValueError as exc:
            # A draft the model built wrong (bad item shape, impossible amount)
            # is the owner's to correct — tell them what's wrong instead of
            # returning a blank "something failed".
            _release_draft_claim(message)
            return Response(
                {"success": False, "error": {"code": "INVALID_DRAFT", "message": str(exc)}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception:  # noqa: BLE001 - draft must stay editable/unconfirmed on any save failure
            _release_draft_claim(message)
            return Response(
                {"success": False, "error": {"code": "SAVE_FAILED", "message": "Could not apply this change. Please try again."}},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            {
                "success": True,
                "data": {
                    "message": ChatMessageSerializer(message).data,
                    "entry_id": entry.id,
                    "undo": {"id": pending_undo.id, "expiresAt": pending_undo.expires_at},
                },
            }
        )


class ConversationListView(APIView):
    """Conversations for this business, most recently active first.

    The list the phone shows when the owner opens chat history. Bounded, and
    ordered by last activity rather than creation so a long-running
    conversation doesn't sink below newer empty ones.
    """

    MAX_CONVERSATIONS = 50

    def get(self, request):
        try:
            business = request.user.business
        except Business.DoesNotExist:
            return Response(
                {"success": False, "error": {"code": "NO_BUSINESS", "message": "No business created yet."}},
                status=status.HTTP_404_NOT_FOUND,
            )

        conversations = (
            Conversation.objects.filter(business=business)
            .annotate(message_count=Count("messages"), last_message_at=Max("messages__timestamp"))
            .order_by(F("last_message_at").desc(nulls_last=True), "-id")[: self.MAX_CONVERSATIONS]
        )
        return Response({"success": True, "data": ConversationSerializer(conversations, many=True).data})


class ConversationMessagesView(APIView):
    """Messages in one conversation, for the mobile offline cache.

    Delta sync: pass `updated_after` (ISO-8601) to get only what has changed
    since the client last synced. The cursor is `updated_at`, not the message
    id, because a message can *change* after it is created — confirming a draft
    flips `draft_confirmed`, and an id-based cursor would never send that
    update, leaving the phone showing a draft as unconfirmed forever.

    The comparison is inclusive (`>=`) so rows sharing the cursor's exact
    timestamp can't fall through the gap between two syncs. That can re-send a
    message the client already has, which is harmless: the client upserts by id.
    """

    DEFAULT_LIMIT = 100
    MAX_LIMIT = 500

    def get(self, request, conversation_id):
        try:
            business = request.user.business
        except Business.DoesNotExist:
            return Response(
                {"success": False, "error": {"code": "NO_BUSINESS", "message": "No business created yet."}},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            conversation = Conversation.objects.get(business=business, pk=conversation_id)
        except Conversation.DoesNotExist:
            return Response(
                {"success": False, "error": {"code": "NOT_FOUND", "message": "Conversation not found."}},
                status=status.HTTP_404_NOT_FOUND,
            )

        messages = ChatMessage.objects.filter(conversation=conversation)

        raw_cursor = request.query_params.get("updated_after")
        if raw_cursor:
            cursor = parse_datetime(raw_cursor)
            if cursor is None:
                return Response(
                    {
                        "success": False,
                        "error": {
                            "code": "VALIDATION_ERROR",
                            "message": "updated_after must be an ISO-8601 timestamp.",
                        },
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if timezone.is_naive(cursor):
                cursor = timezone.make_aware(cursor)
            messages = messages.filter(updated_at__gte=cursor)

        try:
            limit = min(int(request.query_params.get("limit", self.DEFAULT_LIMIT)), self.MAX_LIMIT)
        except (TypeError, ValueError):
            limit = self.DEFAULT_LIMIT

        messages = list(messages.order_by("updated_at", "id")[:limit])

        # The next cursor is the newest row actually returned, so a truncated
        # page resumes exactly where it stopped instead of skipping the
        # remainder. Falling back to the request cursor keeps an empty response
        # from rewinding the client to the beginning of time.
        next_cursor = messages[-1].updated_at.isoformat() if messages else raw_cursor

        return Response(
            {
                "success": True,
                "data": {
                    "conversation_id": conversation.id,
                    "messages": ChatMessageSerializer(messages, many=True).data,
                    "next_cursor": next_cursor,
                    # True when more rows were waiting than the limit allowed,
                    # so the client knows to sync again immediately.
                    "has_more": len(messages) == limit,
                },
            }
        )


class ChatSyncStateView(APIView):
    """Lightweight state check every device makes before syncing.

    Exists so deletions can propagate. Delta sync only ever returns rows that
    still exist, so a second device has no way to discover that history was
    wiped — it would keep serving conversations the business already erased.
    Comparing `history_cleared_at` against the value the client last stored
    tells it when to drop its cache and start from scratch.

    `server_time` is returned so clients never have to trust their own clock;
    every cursor in this system originates on the server.
    """

    def get(self, request):
        try:
            business = request.user.business
        except Business.DoesNotExist:
            return Response(
                {"success": False, "error": {"code": "NO_BUSINESS", "message": "No business created yet."}},
                status=status.HTTP_404_NOT_FOUND,
            )

        cleared_at = ChatSyncState.cleared_at_for(business)
        return Response(
            {
                "success": True,
                "data": {
                    "server_time": timezone.now().isoformat(),
                    "history_cleared_at": cleared_at.isoformat() if cleared_at else None,
                    "conversation_count": Conversation.objects.filter(business=business).count(),
                },
            }
        )


class ClearChatHistoryView(APIView):
    """Deletes every conversation and message for this business.

    Scoped strictly to the caller's own business. Deliberately a hard delete,
    not a soft one: the owner asked for the history to be gone, and rows kept
    behind a `deleted` flag would still be sitting in the database.

    The ledger is untouched. Sales and payments confirmed from a chat draft are
    `ActivityEntry` rows with no foreign key back to the message, and their
    audit trail records the originating message id as a plain integer — so
    erasing a conversation cannot erase money, or the record of where it came
    from.
    """

    def delete(self, request):
        try:
            business = request.user.business
        except Business.DoesNotExist:
            return Response(
                {"success": False, "error": {"code": "NO_BUSINESS", "message": "No business created yet."}},
                status=status.HTTP_404_NOT_FOUND,
            )

        with transaction.atomic():
            conversations = Conversation.objects.filter(business=business)
            message_count = ChatMessage.objects.filter(conversation__in=conversations).count()
            conversation_count = conversations.count()
            # Cascades to ChatMessage; nothing else references either table.
            conversations.delete()

            cleared_at = timezone.now()
            ChatSyncState.objects.update_or_create(
                business=business, defaults={"history_cleared_at": cleared_at}
            )

        logger.info(
            "chat history cleared: business=%s conversations=%s messages=%s",
            business.id,
            conversation_count,
            message_count,
        )

        return Response(
            {
                "success": True,
                "data": {
                    "deleted_conversations": conversation_count,
                    "deleted_messages": message_count,
                    "history_cleared_at": cleared_at.isoformat(),
                },
            }
        )


class ConfirmDraftDocumentView(APIView):
    """Confirms an AI-proposed statement/report and queues it for sending.

    The AI proposes; the owner confirms; the server builds the document from the
    ledger and hands it to the existing document-send pipeline. Nothing about
    the range is taken on trust — dates are re-resolved server-side and the
    customer is re-validated against this business.
    """

    def post(self, request, message_id):
        try:
            business = request.user.business
        except Business.DoesNotExist:
            return Response(
                {"success": False, "error": {"code": "NO_BUSINESS", "message": "No business created yet."}},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            message = ChatMessage.objects.get(pk=message_id, conversation__business=business, sender="ai")
        except ChatMessage.DoesNotExist:
            return Response(
                {"success": False, "error": {"code": "NOT_FOUND", "message": "Message not found."}},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not message.draft_document:
            return Response(
                {
                    "success": False,
                    "error": {"code": "NO_DRAFT_DOCUMENT", "message": "This message has no document request."},
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Same single-claim guard as the other confirms: a double tap must not
        # queue two identical documents to the customer.
        claimed = ChatMessage.objects.filter(pk=message.pk, draft_confirmed=False).update(draft_confirmed=True)
        if not claimed:
            return Response(
                {"success": False, "error": {"code": "ALREADY_CONFIRMED", "message": "This was already confirmed."}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        message.draft_confirmed = True

        draft = message.draft_document
        try:
            # A range may legitimately extend into the future (a forward-looking
            # statement), so future dates are permitted here.
            date_from = resolve_business_date(draft.get("date_from"))
            date_to = resolve_business_date(draft.get("date_to"))
        except BusinessDateError as exc:
            _release_draft_claim(message)
            return Response(
                {"success": False, "error": {"code": "INVALID_DRAFT", "message": str(exc)}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if date_from and date_to and date_from > date_to:
            _release_draft_claim(message)
            return Response(
                {
                    "success": False,
                    "error": {"code": "INVALID_DRAFT", "message": "The start date is after the end date."},
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        customer = None
        if draft.get("customer_id"):
            try:
                customer = Customer.objects.get(business=business, pk=draft["customer_id"])
            except (Customer.DoesNotExist, ValueError, TypeError):
                _release_draft_claim(message)
                return Response(
                    {"success": False, "error": {"code": "CUSTOMER_NOT_MATCHED", "message": "Customer not found."}},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # An invoice/receipt has no date range — it needs a specific entry,
        # and the model is never asked for one (see
        # apps.agent.capabilities.find_latest_entry): the same "most recent
        # matching entry" resolution the auto-execute path uses. Missing
        # this was a real bug — /documents/send/ requires target_id for
        # these two types and this view never supplied one, so tapping
        # Confirm & Send on a fallback invoice/receipt card always 400'd.
        target_id = None
        doc_type = draft["doc_type"]
        if doc_type in ("invoice", "receipt"):
            if customer is None:
                _release_draft_claim(message)
                return Response(
                    {"success": False, "error": {"code": "CUSTOMER_NOT_MATCHED", "message": "Customer not found."}},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            entry_type = "sale" if doc_type == "invoice" else "payment"
            entry = document_services.resolve_latest_entry_for_customer(business, customer, entry_type)
            if entry is None:
                _release_draft_claim(message)
                kind = "sale" if entry_type == "sale" else "payment"
                return Response(
                    {"success": False, "error": {"code": "NOT_FOUND", "message": f"{customer.name} has no {kind} on record yet."}},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            target_id = entry.id

        return Response(
            {
                "success": True,
                "data": {
                    "message": ChatMessageSerializer(message).data,
                    # The app takes these straight to /documents/send/ or
                    # /documents/render/, so one pipeline builds every document.
                    "document_request": {
                        "doc_type": doc_type,
                        "target_id": target_id,
                        "customer_id": customer.id if customer else None,
                        "date_from": date_from.isoformat() if date_from else None,
                        "date_to": date_to.isoformat() if date_to else None,
                    },
                },
            }
        )


class ConfirmDraftCustomerView(APIView):
    """Confirms an AI-proposed *new* customer (`draft_customer`).

    The model was told to check for near-duplicates before proposing this at
    all (see prompt.py's NEW CUSTOMERS section), but the model's own check is
    advisory, not a safeguard — this is the actual gate. It re-runs the exact
    same fuzzy matcher a photographed bill's customer name goes through
    (apps.image_info_extractor.matching), and refuses to create a customer
    when a close match already exists, the same "ask, don't guess" rule as
    everywhere else money-adjacent in this codebase. A new customer is safe
    to auto-approve on ambiguity in neither direction: creating a duplicate
    silently fragments that person's ledger across two rows forever.
    """

    def post(self, request, message_id):
        try:
            business = request.user.business
        except Business.DoesNotExist:
            return Response(
                {"success": False, "error": {"code": "NO_BUSINESS", "message": "No business created yet."}},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            message = ChatMessage.objects.get(pk=message_id, conversation__business=business, sender="ai")
        except ChatMessage.DoesNotExist:
            return Response(
                {"success": False, "error": {"code": "NOT_FOUND", "message": "Message not found."}},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not message.draft_customer:
            return Response(
                {"success": False, "error": {"code": "NO_DRAFT_CUSTOMER", "message": "This message has no new customer proposal."}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        claimed = ChatMessage.objects.filter(pk=message.pk, draft_confirmed=False).update(draft_confirmed=True)
        if not claimed:
            return Response(
                {"success": False, "error": {"code": "ALREADY_CONFIRMED", "message": "This was already confirmed."}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        message.draft_confirmed = True

        draft = message.draft_customer
        name = (draft.get("name") or "").strip()
        if not name:
            _release_draft_claim(message)
            return Response(
                {"success": False, "error": {"code": "INVALID_DRAFT", "message": "No name given for the new customer."}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        existing, candidates = matching.find_matching_customer(business, name)
        if existing is not None or candidates:
            _release_draft_claim(message)
            names = ", ".join(c.name for c in ([existing] if existing else candidates))
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": "POSSIBLE_DUPLICATE",
                        "message": f"This looks like it might already be an existing customer ({names}) — "
                        "check before adding a new one.",
                    },
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            opening_balance = Decimal(str(draft.get("opening_balance") or 0)).quantize(Decimal("0.01"))
        except InvalidOperation:
            opening_balance = Decimal("0")
        # A stated opening balance may be a real debt the customer already
        # carries — no positive-only floor here (unlike a sale line item,
        # which MIN_MONEY governs). Only the absurd-magnitude ceiling applies.
        if opening_balance > MAX_MONEY or opening_balance < -MAX_MONEY:
            opening_balance = Decimal("0")

        customer = Customer.objects.create(
            business=business,
            name=name,
            phone=normalize_phone(draft.get("phone") or ""),
            opening_balance=opening_balance,
            current_balance=opening_balance,
            projected_balance=opening_balance,
        )

        return Response(
            {
                "success": True,
                "data": {
                    "message": ChatMessageSerializer(message).data,
                    "customer_id": customer.id,
                },
            }
        )
