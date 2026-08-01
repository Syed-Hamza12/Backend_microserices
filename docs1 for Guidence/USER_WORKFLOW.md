# User Workflow — How the App Works, End to End

This is the single place to read to understand what the app actually does today, screen by screen,
as a business owner would experience it — not a code reference (see `BACKEND_INTEGRATION_GUIDE.md`
for that) and not a build log (see `docx/MILESTONES.md` / `ADDITIONAL_MILESTONES.md` for that).
Everything described here is built and working on mock/on-device data right now, except where
explicitly marked "still mocked, needs backend."

---

## 1. First launch — Onboarding

```
Splash Screen (branding)
    ↓
    ├─ Existing session found → straight to Dashboard
    └─ No session → Welcome / Introduction screen
           ↓
       Register Account
           ├─→ Google Sign-In, or
           └─→ Email Registration
           ↓
       Verify Account (only if verification is required)
           ↓
       Create Business
           - Business name, category, logo, language, currency
           ↓
       Dashboard (onboarding complete)
```

Once a business exists, every future app open skips straight from Splash to Dashboard (session
check, no re-onboarding).

---

## 2. The 5 bottom-nav tabs — always available from Dashboard onward

```
[ Home ] [ Customers ] [ Entries/Sales ] [ Chat ] [ Settings ]
```

This structure is fixed — every core action is reachable from one of these 5 tabs, no deep menus
required. Sections 3–7 below cover each tab.

---

## 3. Home (Dashboard)

- Greeting header + Business Overview summary cards (today's sales, pending balances, etc.)
- **Big centered voice button** — "Hold to Speak." Press-and-hold, WhatsApp-style: press starts
  listening, release stops it and sends whatever was captured (real on-device speech-to-text, not
  mocked). Releases into the same AI Chat conversation as the Chat tab — one voice feature, two
  doorways into it, not two separate features.
- **"Type Command"** secondary button — same destination (AI Chat), for typing instead of speaking.
- **Quick Actions row** — Add Entry / Add Payment / Add Customer / Statement, each jumping straight
  to that flow (Sections 4–5 below).

---

## 4. Customers tab

```
Customers List (search bar, + button to add)
    ↓ (+ button)
Add Customer (name, phone, address, opening balance)
    ↓ (tap an existing customer)
Customer Detail
    - Big balance display + action row: Call · WhatsApp · Edit Data · More
    - Overview | Bills | Payments | Notes tabs underneath
```

**Action row:**
- **Call** — opens the device dialer (stub until a later phone-integration milestone).
- **WhatsApp** — opens a chat with that customer's number (stub until the WhatsApp Gateway
  backend milestone — still mocked).
- **Edit Data** → Edit Records (one row per past transaction, edit icon on each) → Edit Entry
  (edit or delete that one specific Sale or Payment — editing/deleting is always allowed, no
  locking, per the app's correction policy).
- **More** → Past Sales List → pick a sale → Invoice Preview (view/share as PDF).

**The 4 tabs under the action row:**
- **Overview** — balance + summary + full transaction history (the screen's original view).
- **Bills** — this customer's sales/invoices only.
- **Payments** — this customer's payment records only.
- **Notes** — one free-text note field per customer, saved through the repository layer.

Editing the customer's own contact info (name/phone/address) is a separate action — the pencil
icon in Customer Detail's own app bar, not part of the action row above.

---

## 5. Entries/Sales tab — recording a Sale or a Payment

```
Add Entry screen
    - Toggle: Sale Entry / Payment Entry (switching modes never loses what's typed in the other)
    - Customer selector
    - Date picker
    ↓
Sale Entry mode:
    - Item rows (item name, qty, rate → amount computed) — add as many rows as needed
    - Subtotal shown live
    - Amount Received (optional) → if > 0, a payment method (Cash/Bank/JazzCash/EasyPaisa) is
      required
    - Live balance preview (previous + subtotal − received)
    ↓ (Save)
Balance updated, back to Customer Detail/Dashboard
    (a sale with a simultaneous partial/full payment is recorded as two linked entries —
    one Sale + one Payment — sharing the same balance calculation, sorted sale-then-payment)

Payment Entry mode:
    - Amount, payment method (required), date, note (optional)
    ↓ (Save)
Payment Receipt screen shown (preview/share) → back to Customer Detail, balance updated
```

Both modes show a loading state while saving and a retry-able error banner if the save fails (a
dev-only "simulate failure" toggle exists for testing this).

---

## 6. Chat tab — the AI accountant

This is the app's core feature: a conversation with an AI that can answer questions, draft bills,
and now read photos of receipts/bills.

```
Chat tab (or Dashboard's voice button / "Type Command" — same conversation, not a separate one)
    ↓
Conversation view: chat bubbles, quick-reply chips, and an input bar with
attach-photo / mic / send
```

### 6a. Typing or speaking to the AI

- Type a message and hit send, **or** press-and-hold the mic (real on-device speech-to-text,
  release to send the transcript) — both land in the same thread.
- The AI's reply appears as a bubble and — if the Settings voice-reply toggle is on — is spoken
  aloud via real on-device text-to-speech. A "Replay ▶" pill under any AI reply lets the owner
  re-hear it any time.
- **Currently mocked:** the AI's understanding is a keyword-matcher (e.g. "today's sales," "top 5
  customers," "draft a bill," "Kashan's statement") — replies are canned, not from a real model.
  This is the one piece that needs a real backend/LLM to become "real" AI (see
  `BACKEND_INTEGRATION_GUIDE.md` Section 7).

### 6b. Attaching a photo (receipt/bill) — Milestone 19

```
Tap the attach-photo icon next to the mic
    ↓
Choose: Take a photo / Choose from gallery
    ↓
Photo appears as the owner's message bubble
    ↓ (short delay, "typing" shown)
AI responds — currently always with a Draft Bill card, same as the text
"draft a bill" flow, to prove the round-trip UI works. **Currently mocked:**
the image's actual contents are ignored; a real backend needs to actually
read/OCR the photo and extract real amounts (see
`BACKEND_INTEGRATION_GUIDE.md` Section 7a).
```

### 6c. Draft Bill card — Edit vs. Confirm and Send

When the AI proposes a sale (from text, voice, or an image), it appears as an inline card with
customer name, previous balance, total, amount received, computed new balance, and two buttons.

```
Draft Bill card
    ├─→ "Edit Draft" → dedicated Edit Draft screen
    │       - Customer + previous balance shown read-only (a draft can't be
    │         reassigned to a different customer)
    │       - Total and Amount Received are editable, new balance recalculates live
    │       ↓ ("Save Draft")
    │   Back to chat — only that card's numbers update. **Nothing is saved to
    │   the customer's ledger at this point** — this is a chat-only edit.
    │
    └─→ "Confirm and Send" → **this is the only step that actually records
            the sale** (same save path as a manual Sales-tab entry, updating
            the customer's real balance)
            ↓ (save succeeds)
        Card locks: both buttons disappear, replaced by a "Sent ✓" badge —
        an already-confirmed draft can't be edited or sent again.
        An AI confirmation bubble appears: "Saved! The bill has been recorded."
```

If a real backend save ever fails, the card should stay editable (not lock to "Sent") and show an
inline error instead — see `BACKEND_INTEGRATION_GUIDE.md` Section 7b for exactly how this maps to
an API call.

### 6d. Document-ready card

For requests like "send Kashan's statement," the AI replies with a card pointing at a real,
already-generated document (e.g. a Statement) with a "View" button that opens the actual Statement
Preview screen — not a placeholder.

---

## 7. Settings tab

```
Settings
    ├─→ Business Profile (name, category, logo)
    ├─→ Business Information section: inline live WhatsApp connection-status badge
    ├─→ Currency (display/edit row)
    ├─→ WhatsApp Connection
    │      - Disconnected: "Connect" → QR code → scan → "Connected" (**still mocked** —
    │        real QR/session flow needs the WhatsApp Gateway backend,
    │        see `BACKEND_INTEGRATION_GUIDE.md` Section 9)
    │      - Connected: status shown + "Disconnect"
    ├─→ Language (Urdu / Roman Urdu / English)
    ├─→ AI Voice Reply (on/off toggle — controls Section 6a's spoken replies)
    ├─→ Notifications (inbox of invoice-sent / payment-received / WhatsApp-disconnected /
    │      pending-payment-reminder / daily-summary items)
    ├─→ Reports (Sales / Payment / Customer tabs, This Month/Last Month/Custom date range,
    │      3 summary tiles, a Sales-Over-Time line chart, Download PDF Report)
    └─→ More
           - Import Data (Excel), Export Data, Data Backup, Printer Settings, Reminders,
             Help & Support — all "Coming soon" stubs today
           - About App — real app version number
           - Rate Us, Share App — stubs today
```

**Never present in this app, by explicit product decision:** any multi-user/staff-account
functionality. This is single-owner-per-business, full stop.

---

## 8. What's real today vs. what still needs the backend

| Feature | Status |
|---|---|
| Onboarding, Dashboard, Customers, Sales/Payments, Invoices/Statements/Receipts, Reports, Settings | Real UI + logic, mock data (works fully once wired to Django — see `BACKEND_INTEGRATION_GUIDE.md`) |
| Voice — speech-to-text and text-to-speech | **Real**, on-device, no backend involved at all |
| AI understanding (what the chat "brain" actually does with a message or image) | **Mocked** — keyword-matcher for text, canned reply for images — needs a real AI/backend call |
| Draft Bill "Confirm and Send" | Real save into the mock repository today; maps 1:1 to a real `POST /api/sales/` call once the backend exists |
| Draft Bill "Edit Draft" / "Save Draft" | Real, chat-local only — by design, never calls a backend at all |
| WhatsApp connection | **Mocked** — needs the WhatsApp Gateway + Django in front of it |
| Image upload in chat | UI + local-file handling is real; the actual reading/extraction of the photo is mocked, needs a real OCR/vision backend call |

For exactly which API endpoints to build for each mocked piece, see `BACKEND_INTEGRATION_GUIDE.md`.
