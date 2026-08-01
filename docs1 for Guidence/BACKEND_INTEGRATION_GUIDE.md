# Backend Integration Guide (for connecting the Django backend to this Flutter app)

Written for someone with **no Flutter/Dart experience**. Every section explains the Dart concept
in plain terms before showing what to change. This is the one file to hand to yourself (or anyone
else) in a week when the Django backend exists — it tells you exactly what to build on the Django
side and exactly which files to touch in the Flutter app, for every feature.

---

## 1. The big picture

```
Flutter Mobile App  ──HTTP──>  Django Backend  ──┬──> Database (customers, sales, payments, chat, ...)
                                                  │
                                                  └──HTTP──> WhatsApp Gateway microservice
                                                              (the Node/Baileys service whose
                                                              Postman spec you already have)
```

Three hard rules, already baked into the app's structure so you don't have to fight it later:

1. **The Flutter app never calls the WhatsApp Gateway directly.** It only ever calls Django. Django
   is the one holding the Gateway's `x-api-key` and calling `localhost:3000/api/v1/...`. The phone
   never sees that key or that URL.
2. **The Flutter app never calls a database.** Every screen reads/writes through Django's REST API.
3. **Voice (speech-to-text / text-to-speech) is NOT part of this at all.** It runs entirely on the
   phone using on-device plugins (`speech_to_text`, `flutter_tts`). Django is never involved in
   voice. Skip that section if you're only thinking about backend work.

---

## 2. The one pattern used everywhere — read this before anything else

Every screen in the app reads/writes data through a **repository** — this is just a Dart word for
"a class listing the operations a screen is allowed to do" (e.g. "get all customers," "record a
sale"). Think of it like an API contract.

In Dart, that contract is written as an `abstract class` — this just means "a list of method names
and their input/output types, with no actual code inside them." It's not runnable by itself.

```dart
abstract class CustomerRepository {
  List<Customer> get customers;          // no body — just says "any implementation must have this"
  void addCustomer(Customer customer);   // same
}
```

Right now, every one of these contracts has exactly **one implementation**, always named
`Mock<Something>Repository`, which fakes the data in memory (hardcoded customers, `Future.delayed`
timers instead of real network calls, etc.) — no server involved at all today.

**Your job is not to rewrite the screens.** Every screen already only talks to the abstract
contract, never to the mock class directly. Your job for each domain is:

1. Write a new class, e.g. `DjangoCustomerRepository implements CustomerRepository`, that fulfills
   the exact same contract but makes real `http` calls to Django instead of touching an in-memory
   list.
2. Change **one line** in one file (`lib/screens/shell/app_shell.dart`) that currently does
   `MockCustomerRepository()` to instead do `DjangoCustomerRepository()`.

That's the entire integration model for almost everything in this app. Section 4 onward gives you
the exact contract (methods + data shapes) for every domain, so you can write each `Django*Repository`
class without needing to read the rest of the Flutter codebase.

### Where all the mock classes live today

| Domain | Interface + mock file |
|---|---|
| Auth / Session | `lib/data/repositories/auth_repository.dart` |
| Business Profile | `lib/data/repositories/business_profile_repository.dart` |
| Customers / Sales / Payments | `lib/data/customer_repository.dart` |
| AI Chat | `lib/data/repositories/chat_repository.dart` |
| Notifications | `lib/data/repositories/notification_repository.dart` |
| WhatsApp connection | `lib/data/repositories/whatsapp_repository.dart` |

### The one file where everything gets wired together

`lib/screens/shell/app_shell.dart` — near the top of the file:

```dart
class _AppShellState extends State<AppShell> {
  final _customerRepository = MockCustomerRepository();   // ← change this line
  final _whatsAppRepository = MockWhatsAppRepository();    // ← and this line
  ...
```

`AppSettings` (`lib/core/localization/app_settings_scope.dart`) is where Auth and Business Profile
get wired — it takes the repository as a constructor parameter, defaulting to the mock:

```dart
AppSettings({
  ...
  AuthRepository? authRepository,               // ← pass DjangoAuthRepository() here
  BusinessProfileRepository? businessProfileRepository,  // ← and here
})
```
That's created in `lib/app.dart`: `final AppSettings _settings = AppSettings();` — add the two
named arguments there once your Django repositories exist.

Chat's repository is wired in `ChatController`'s constructor call, also inside `app_shell.dart`:
```dart
_chatController ??= ChatController(
  customerRepository: _customerRepository,
  language: AppSettingsScope.of(context).language,
  settings: AppSettingsScope.of(context),
  // chatRepository: DjangoChatRepository(),   // ← add this
)
```

Notifications default inside the screen itself
(`lib/screens/settings/notifications_list_screen.dart`) — pass a `DjangoNotificationRepository()`
where it's constructed (`SettingsPlaceholderScreen`'s navigation call).

---

## 3. Adding the ability to make HTTP calls at all

The app currently has **zero HTTP networking code** — no package for it is installed, because
nothing has ever needed to call a real server. Before writing any `Django*Repository` class:

1. Open `pubspec.yaml`, find the `dependencies:` section, add:
   ```yaml
   http: ^1.2.0
   ```
2. Run `flutter pub get`.
3. Somewhere central (suggest a new file `lib/core/network/api_client.dart`), write one small
   helper that every `Django*Repository` reuses — base URL, auth header, JSON decode, error
   handling in one place instead of repeated in every repository:

   ```dart
   import 'dart:convert';
   import 'package:http/http.dart' as http;

   class ApiClient {
     static const baseUrl = 'https://your-django-domain.com/api'; // set this
     final String? authToken; // however you store the logged-in user's session token

     ApiClient({this.authToken});

     Map<String, String> get _headers => {
       'Content-Type': 'application/json',
       if (authToken != null) 'Authorization': 'Bearer $authToken',
     };

     Future<dynamic> get(String path) async {
       final res = await http.get(Uri.parse('$baseUrl$path'), headers: _headers);
       return _handle(res);
     }

     Future<dynamic> post(String path, Map<String, dynamic> body) async {
       final res = await http.post(Uri.parse('$baseUrl$path'),
           headers: _headers, body: jsonEncode(body));
       return _handle(res);
     }

     dynamic _handle(http.Response res) {
       if (res.statusCode >= 200 && res.statusCode < 300) {
         return res.body.isEmpty ? null : jsonDecode(res.body);
       }
       throw Exception('API error ${res.statusCode}: ${res.body}');
     }
   }
   ```

   Every example below assumes something like this exists. Exact shape (Bearer token vs cookie,
   your actual Django URL structure) is up to how you build Django's auth — adjust freely, the
   Flutter side doesn't care as long as the repository classes still fulfill their contracts.

---

## 4. Auth / Session

**File:** `lib/data/repositories/auth_repository.dart`

**Contract:**
```dart
abstract class AuthRepository {
  bool get hasValidSession;
  bool get hasBusiness;
  Future<void> signIn();
  Future<void> completeBusinessSetup();
  Future<void> signOut();
}
```

| Method | When it's called | What Django should do |
|---|---|---|
| `signIn()` | User taps "Continue with Google" or finishes email registration | Authenticate the user (Google OAuth token exchange, or email/password), store a session token on the device |
| `completeBusinessSetup()` | Right after Create Business form is submitted | Mark that this user has finished onboarding |
| `signOut()` | Settings → Logout | Invalidate the session token |
| `hasValidSession` / `hasBusiness` | Read on every app launch (Splash screen) to decide: go straight to Dashboard, or start onboarding | Should reflect real stored/checked session state, not just in-memory booleans |

**Real implementation notes:**
- `hasValidSession`/`hasBusiness` are currently plain in-memory booleans (`bool get` — no async).
  Your real version will likely need to check a locally-stored token (e.g. via the `shared_preferences`
  package — not yet installed, add it the same way as `http` above) rather than calling Django on
  every splash-screen check. Calling Django once at `signIn()` to get a token, then storing it
  locally and just checking "is a token present and not expired" for `hasValidSession`, is the
  usual pattern.
- Suggested Django endpoints: `POST /api/auth/google/`, `POST /api/auth/email-register/`,
  `POST /api/auth/logout/`, and whatever session-check endpoint you want to call once at startup to
  confirm the stored token is still valid server-side.

---

## 5. Business Profile

**File:** `lib/data/repositories/business_profile_repository.dart`

**Contract:**
```dart
class BusinessProfileData {
  final String businessName;
  final String businessCategory;
  final String currencyCode;
  final bool logoAdded;
}

abstract class BusinessProfileRepository {
  BusinessProfileData get profile;
  Future<void> update(BusinessProfileData profile);
}
```

| Method | When it's called | Django endpoint suggestion |
|---|---|---|
| `profile` (getter) | Read whenever a screen needs the business name/currency (Dashboard greeting, invoice headers, Settings) | `GET /api/business/profile/` — fetch once at login and cache in memory, since this getter is synchronous (not `Future`) in the current contract. **You will need to change this getter to something the app fetches once at startup and caches**, e.g. load it right after `signIn()` succeeds. |
| `update(profile)` | Create Business form submit, or Business Profile screen "Save Changes" | `PATCH /api/business/profile/` — body: `{businessName, businessCategory, currencyCode, logoAdded}` |

**Note on `logoAdded`:** currently just a boolean (the mock never actually uploads an image — it's
a placeholder toggle). A real logo upload would need a new method (e.g. `uploadLogo(File image)`
hitting a multipart endpoint) and a new field for the logo URL — this isn't in the contract yet
because no image picker exists in the app yet either. Flag this to whoever's building the upload UI
if logo upload is wanted for real.

---

## 6. Customers, Sales, Payments (one file — they're tightly linked)

**File:** `lib/data/customer_repository.dart` — this single file defines **three** contracts because
sales/payments always affect a customer's balance together, but a real backend can still serve them
from separate endpoints; the Dart side just needs one class implementing all three interfaces.

### Data shapes first

```dart
class Customer {
  final String id;
  final String name;
  final String phone;
  final String address;        // optional, defaults to ''
  final double openingBalance; // starting balance when customer was created
  final double currentBalance; // running balance — positive = customer owes you
}

class SaleLineItem {
  final String itemName;
  final double quantity;
  final double rate;
  // amount = quantity * rate (computed, not stored separately)
}

enum PaymentMethod { cash, bank, jazzCash, easyPaisa }

enum ActivityType { sale, payment }

class ActivityItem {                 // one row of a customer's transaction history
  final String customerName;
  final ActivityType type;           // sale or payment
  final double amount;               // sale total, or payment amount
  final double balanceAfter;         // running balance immediately after this entry
  final DateTime timestamp;
  final List<SaleLineItem> lineItems; // only for sales; empty for payments
  final String? saleGroupId;         // links a sale to its same-transaction payment, see below
  final PaymentMethod? method;       // only for payments; null for sales
}
```

**Important business rule** (already implemented in the mock — replicate it in Django, don't
reinvent it): when a sale is recorded with a partial/full amount received *at the same time*, that
creates **two** `ActivityItem`s — one `sale` and one linked `payment` — sharing the same
`saleGroupId`, timestamped 1ms apart so they sort sale-then-payment. The customer's `currentBalance`
is calculated exactly once as `previousBalance + saleTotal - amountReceived`. If your Django API
already returns activity/history rows shaped like this, the Flutter side needs zero changes to its
balance-display logic.

### The contract

```dart
abstract class CustomerRepository {
  List<Customer> get customers;
  List<ActivityItem> historyFor(String customerId);
  void addCustomer(Customer customer);
  void updateCustomer(Customer updated);
  void deleteEntry({required String customerId, required ActivityItem target});
  String noteFor(String customerId);
  void updateNote({required String customerId, required String note});

  // also implements SalesRepository:
  void recordSale({
    required String customerId,
    required List<SaleLineItem> items,
    double amountReceived = 0,
    PaymentMethod? paymentMethod,
    DateTime? date,
  });
  void editSale({required String customerId, required ActivityItem target, required List<SaleLineItem> items, DateTime? date});
  void deleteSaleLineItem({required String customerId, required ActivityItem target, required int lineItemIndex});

  // also implements PaymentRepository:
  void recordPayment({required String customerId, required double amount, required PaymentMethod method, DateTime? date, String note = ''});
  void editPayment({required String customerId, required ActivityItem target, required double amount, required PaymentMethod method, DateTime? date});
}
```

### Suggested Django endpoints

| Dart method | REST call |
|---|---|
| `customers` (getter) | `GET /api/customers/` |
| `historyFor(id)` | `GET /api/customers/{id}/history/` |
| `addCustomer(c)` | `POST /api/customers/` |
| `updateCustomer(c)` | `PATCH /api/customers/{id}/` |
| `deleteEntry(...)` | `DELETE /api/entries/{entryId}/` |
| `noteFor(id)` / `updateNote(...)` | `GET`/`PATCH /api/customers/{id}/note/` |
| `recordSale(...)` | `POST /api/sales/` |
| `editSale(...)` | `PATCH /api/sales/{saleId}/` |
| `deleteSaleLineItem(...)` | `DELETE /api/sales/{saleId}/items/{index}/` |
| `recordPayment(...)` | `POST /api/payments/` |
| `editPayment(...)` | `PATCH /api/payments/{paymentId}/` |

**Real-world change needed:** every one of these Dart methods is currently synchronous (`void`, not
`Future<void>`) because the mock updates an in-memory list instantly. A real network call is always
async. **You will need to change the abstract contract itself** — add `Future<void>` return types
and `async`/`await` — and update the small number of call sites (Add Entry screen's Save button,
Edit Entry screen, Record Edit List screen) to `await` the call and show a loading state, reusing
the same `PrimaryButton(isLoading: true)` pattern already used elsewhere in the app (e.g. Business
Profile's Save Changes button). This is the single biggest "shape" change across the whole
integration — everything else mostly slots in as-is.

**Balance recalculation:** the mock recalculates every later entry's `balanceAfter` whenever an
earlier entry is edited/deleted ("forward-only cascade" — entries before an edit point are never
touched). Do this calculation **in Django**, and have `historyFor()`/`recordSale()`/etc. always
return the already-correct `balanceAfter` values — the Flutter app just displays what it's given,
it doesn't recompute anything itself once real data arrives.

---

## 7. AI Chat

**File:** `lib/data/repositories/chat_repository.dart`

**Contract:**
```dart
abstract class ChatRepository {
  ChatMessage generateReply({
    required String text,
    required Strings strings,
    required CustomerRepository customers,
  });

  // Added for image upload (Milestone 19) — see "7a. AI Chat — Image upload"
  // below for the full picture.
  ChatMessage generateImageReply({
    required String imagePath,
    required Strings strings,
    required CustomerRepository customers,
  });
}
```

This is the **one contract that will change shape the most**, because right now it's a synchronous
keyword-matcher (`if text.contains("today's sales") return canned reply`) — there's no concept of a
real AI model call here at all. When you build the real AI backend:

```dart
abstract class ChatRepository {
  Future<ChatMessage> generateReply({
    required String text,
    required String conversationId, // so the backend can maintain context, per docx/ARCHITECTURE.md
  });
}
```

Suggested Django endpoint: `POST /api/chat/message/` — body `{conversationId, text}` — response
should include:
- `reply.text` — what's shown in the chat bubble (Roman Urdu, Urdu, or English, matching whatever
  the owner used)
- `reply.speechText` — **important**: if the owner is in Roman Urdu mode, this should be the same
  reply written in **native Urdu script**, not the Latin-script version. The on-device TTS
  mispronounces Latin-script Roman Urdu (this was found and fixed during testing — see
  `ChatMessage.speechText`'s doc comment in `lib/models/chat_message.dart`). If Django can return
  both a display string and a script-correct spoken string in one response, no Flutter-side
  transliteration is ever needed.
- Optionally `draftBill` / `documentReady` fields matching `DraftBillCardData`/`DocumentReadyCardData`
  (see `lib/models/chat_message.dart`) if the AI response should render as one of the special inline
  cards instead of plain text.

**`ChatMessage` shape** (what your `generateReply` must produce, one way or another):
```dart
class ChatMessage {
  final String id;
  final ChatSender sender;      // owner or ai
  final String? text;
  final String? speechText;     // see above — native-script version for TTS
  final DateTime timestamp;
  final DraftBillCardData? draftBill;
  final DocumentReadyCardData? documentReady;
  final bool draftConfirmed;    // see "7b. Draft Bill — Edit vs Confirm and Send" below
}
```

---

## 7a. AI Chat — Image upload (Milestone 19)

**Files:** `lib/models/chat_message.dart`, `lib/data/repositories/chat_repository.dart`,
`lib/data/chat_controller.dart`, `lib/widgets/chat/chat_input_bar.dart`,
`lib/widgets/chat/chat_bubble.dart`, `lib/screens/chat/chat_screen.dart`

The owner can now attach a photo (e.g. a receipt or handwritten bill) to the chat, via a new
attach-photo icon in `ChatInputBar` that opens a camera/gallery picker
(`image_picker` package, already added to `pubspec.yaml`). This is **UI + local-file plumbing
only, still mocked** — no image bytes leave the phone yet. Everything below is what's needed to
make it real.

### Current (mock) flow

1. Owner taps the attach icon → picks/takes a photo → `image_picker` returns a local file path.
2. `ChatController.sendImage(imagePath, {caption})` appends a `ChatMessage.image` bubble (shown via
   `Image.file(File(imagePath))` in `ChatBubble`) and sets `isTyping = true`.
3. After a fake 1200ms delay, `ChatRepository.generateImageReply(imagePath: ..., strings: ...,
   customers: ...)` is called and its `ChatMessage` result is appended as the AI's reply.
4. `MockChatRepository.generateImageReply` **ignores the actual image content** — it always
   returns the same canned `ChatMessage.draftBill` for the same sample customer (`c1`/Kashan) that
   the text `"draft a bill"` flow uses, just to prove the round-trip UI works end-to-end.

### What real integration needs

```dart
abstract class ChatRepository {
  Future<ChatMessage> generateImageReply({
    required File imageFile,      // or raw bytes — see below
    required String conversationId,
  });
}
```

Same async-conversion note as the rest of this guide (Section 6): the mock's `generateImageReply`
is synchronous today; the real one must be `Future<ChatMessage>`, and
`ChatController.sendImage`'s `Future.delayed(...)` block should `await` it instead of faking a
timer.

**Suggested Django endpoint:** `POST /api/chat/image/` as a **multipart/form-data** request (not
JSON, since it carries binary image data) — fields: `conversationId` and an `image` file part.
Django (or whatever OCR/vision model it calls — e.g. an LLM vision endpoint) reads the image and
extracts whatever structured data it can (line items, amounts, a customer name if handwritten,
etc.), then responds with the same reply shape Section 7 already defines for text chat:
- `reply.text` / `reply.speechText` — if the AI just wants to describe what it read back in a
  plain-text bubble (e.g. "I couldn't read this clearly, can you confirm the total?").
- `reply.draftBill` — the common case: the extracted amounts pre-filled into a
  `DraftBillCardData` (`customerId` if it could be matched to an existing customer,
  `previousBalance`, `totalAmount`, `paymentReceived`), so the owner just reviews/edits and taps
  "Confirm and Send" exactly like the existing text-triggered draft-bill flow.

**Flutter-side change needed once this endpoint exists:**
```dart
class DjangoChatRepository implements ChatRepository {
  final ApiClient _api; // Section 3's helper won't work as-is — see note below

  @override
  Future<ChatMessage> generateImageReply({required File imageFile, required String conversationId}) async {
    final request = http.MultipartRequest('POST', Uri.parse('${ApiClient.baseUrl}/chat/image/'))
      ..fields['conversationId'] = conversationId
      ..files.add(await http.MultipartFile.fromPath('image', imageFile.path));
    final streamed = await request.send();
    final res = await http.Response.fromStream(streamed);
    final data = jsonDecode(res.body);
    // map `data` to a ChatMessage the same way Section 7's text reply does
  }
}
```
Note: Section 3's `ApiClient.post` assumes a JSON body — it can't send multipart. Add a small
`postMultipart(path, fields, filePath)` helper alongside it rather than trying to force this
through the existing `get`/`post` methods.

**Not built yet, flag if wanted:** no compression/resizing happens before upload (only
`imageQuality: 85` from `image_picker` itself); no upload-progress indicator; no retry-on-failure
UI. `ChatController.sendImage` currently has no error handling at all since the mock can't fail —
add a try/catch around the real network call (mirroring `speechError`'s pattern in the same file)
before shipping this for real, so a failed extraction degrades gracefully instead of leaving the
typing indicator stuck.

---

## 7b. Draft Bill — Edit vs. Confirm and Send (post-Milestone-19 UX fix)

**Files:** `lib/models/chat_message.dart`, `lib/data/chat_controller.dart`,
`lib/screens/chat/edit_draft_screen.dart`, `lib/widgets/chat/draft_bill_card.dart`,
`lib/screens/chat/chat_screen.dart`

Direct user feedback corrected the original Draft Bill flow: **editing a draft must never touch
the database** — it only edits the card sitting in the chat message. The database (Django) is only
written to at the separate "Confirm and Send" step. This is an important distinction for your API
design: **two very different actions map to two very different backend calls (or no call at all).**

### Action 1 — "Edit Draft" → opens `EditDraftScreen` → "Save Draft"

This is **chat-local only. No backend call at all**, mock or real. The owner adjusts Total /
Amount Received on a dedicated screen (customer and previous balance are shown read-only, not
editable — a draft can't be reassigned to a different customer), taps "Save Draft", and
`ChatController.updateDraft(messageId, updatedDraftBillCardData)` just replaces that one message's
`draftBill` in the in-memory `messages` list and calls `notifyListeners()`. Nothing is sent to
Django here — don't build an endpoint for this step. If you want edits to survive an app restart or
sync across devices before confirmation, that's the one reason you'd add something like
`PATCH /api/chat/draft/{messageId}/` — but it's optional, not required for the app to work
correctly.

### Action 2 — "Confirm and Send" → this is the real save

This is the **only** point in the whole Draft Bill flow that should write anything to Django.
`ChatController.confirmDraft(messageId)` currently calls the mock's `CustomerRepository.recordSale`
directly (Section 6) — a real implementation should have your `DjangoChatRepository` (or wherever
you wire this) call the **exact same sales endpoint already defined in Section 6**
(`POST /api/sales/`), not a separate "confirm chat draft" endpoint — a confirmed AI draft and a
manually-entered sale are the same domain object once saved. Suggested request body, built from
`DraftBillCardData`:
```json
{
  "customerId": "<draftBill.customerId>",
  "items": [{ "itemName": "AI Chat Draft Bill", "quantity": 1, "rate": "<draftBill.totalAmount>" }],
  "amountReceived": "<draftBill.paymentReceived>",
  "paymentMethod": "cash"
}
```
(The mock hardcodes a single line item named `"AI Chat Draft Bill"` and `paymentMethod: cash`
because `DraftBillCardData` doesn't currently carry real line items or a payment method — if your
AI backend can extract actual line items or ask the owner which payment method was used, extend
`DraftBillCardData` with those fields and drop the hardcoding; this is flagged as a known
simplification, not an intentional restriction.)

**Only after Django confirms the save succeeded** should the Flutter side flip
`ChatMessage.draftConfirmed` to `true` on that message (via `ChatMessage.copyWithDraft(draftConfirmed:
true)`) — this is what disables both the "Edit Draft" and "Confirm and Send" buttons on the card
and replaces them with a "Sent ✓" badge (`DraftBillCard`'s `isSent` flag), so the owner can't edit
or double-submit an already-recorded bill. **If the Django call fails, do not set
`draftConfirmed`** — leave the card active so the owner can retry, and surface the failure the same
way other save failures do in this app (an inline error, not a raw exception — see `ErrorBanner`
usage elsewhere, e.g. Add Entry's save-failure pattern).

### Summary table

| Owner action | Screen/button | Backend call? | What changes |
|---|---|---|---|
| Edit Draft → Save Draft | `EditDraftScreen` | **No** (chat-local) | That message's `draftBill` fields only |
| Confirm and Send | Draft Bill card button | **Yes** — same `POST /api/sales/` as Section 6 | Real sale recorded; `draftConfirmed = true` on success; card locks to "Sent ✓" |

---

## 8. Notifications

**File:** `lib/data/repositories/notification_repository.dart`

**Contract:**
```dart
enum NotificationType { invoiceSent, paymentReceived, whatsappDisconnected, pendingPaymentReminder, dailySummary }

class NotificationItem {
  final NotificationType type;
  final DateTime timestamp;
}

abstract class NotificationRepository {
  List<NotificationItem> getNotifications();
}
```

Suggested endpoint: `GET /api/notifications/`. Same async-conversion note as Section 6 applies —
change to `Future<List<NotificationItem>>` and have the screen `await` it (a `FutureBuilder` or a
simple loading flag is enough; the screen already has empty-state handling built in). Push
delivery (actually notifying the phone in real time) is a separate concern from this — this
repository is only "fetch the inbox list"; real-time push would use Firebase Cloud Messaging or
similar, wired up separately in the Android/iOS native layers, not through this repository at all.

---

## 9. WhatsApp Connection — mapped directly to your Gateway API

**File:** `lib/data/repositories/whatsapp_repository.dart`

This is the domain your pasted Postman guide is *for*. Confirmed: **nothing here is wired to a real
backend yet** — the mobile app currently shows a fake icon instead of a QR code and just runs a
1.2-second timer before flipping to "connected." Here's exactly how the real pieces map together.

### Current (mock) contract
```dart
abstract class WhatsAppRepository extends ChangeNotifier {
  bool get isConnected;
  bool get isConnecting;
  Future<bool> connect();
  void disconnect();
}
```

### Contract you'll need to build to (extended)

The current contract has no way to carry a QR **image** or to represent "waiting for scan" vs
"connecting" vs "connected" as separate states, because the mock never needed to. Extend it like
this:

```dart
enum WhatsAppStatus { notConnected, qrReady, connecting, connected, error }

abstract class WhatsAppRepository extends ChangeNotifier {
  WhatsAppStatus get status;
  Uint8List? get qrImageBytes;   // the PNG bytes from GET /sessions/{id}/qr
  String? get connectedPhone;    // the "phone" field from a CONNECTED /status response

  Future<void> connect();        // kicks off the whole flow, see below
  Future<void> disconnect();
  Future<void> unlink();         // maps to DELETE /sessions/{id} — permanent
}
```

### How `connect()` should work end-to-end

This is a multi-step flow because your Gateway API is async (create → connect → poll for QR →
poll for CONNECTED). Django should hide most of this from the phone — the phone should only need to
call **one Django endpoint** to kick things off, then poll **one Django status endpoint**:

1. **Django side** (do this once, the first time a business connects WhatsApp — store the
   `gatewaySessionId` against that business in Django's DB so you don't create a new Gateway session
   every time):
   - `POST http://gateway-host/api/v1/sessions` with `{"displayName": "<business name>"}`
     → save the returned `id` as `gatewaySessionId`.
   - `POST http://gateway-host/api/v1/sessions/{gatewaySessionId}/connect`.
   - Now the Gateway is working in the background. Django doesn't wait here.

2. **Django exposes to the phone:**
   - `POST /api/whatsapp/connect/` — triggers step 1 above (or reuses an existing
     `gatewaySessionId` and just calls the Gateway's `/connect` again — see Section 9's "Reconnect"
     note in your Postman doc, reusing an existing session skips the QR scan entirely, which is
     what you want on every connect *after* the first).
   - `GET /api/whatsapp/status/` — Django calls the Gateway's `GET /sessions/{id}/status` and
     forwards the relevant fields: `{status: "QR_READY" | "CONNECTING" | "CONNECTED" | "ERROR" | "DISCONNECTED", phone: "923001234567" | null}`.
   - `GET /api/whatsapp/qr/` — Django calls the Gateway's `GET /sessions/{id}/qr` and streams the
     PNG bytes straight through to the phone (same `image/png` content type). Django should return
     a `404`/some "not ready" response if the Gateway returns `QR_NOT_AVAILABLE` — the phone should
     treat that as "keep polling status, don't show a broken image."
   - `POST /api/whatsapp/disconnect/` → Django calls the Gateway's `/disconnect`.
   - `DELETE /api/whatsapp/unlink/` → Django calls the Gateway's `DELETE /sessions/{id}` (this is
     the permanent, destructive one — logs the phone out of WhatsApp for real; only wire this to
     something the owner explicitly confirms, same as the existing disconnect-confirmation dialog
     in `WhatsAppConnectionScreen`).

3. **Flutter's `connect()` implementation**, once the above Django endpoints exist:
   ```dart
   class DjangoWhatsAppRepository extends ChangeNotifier implements WhatsAppRepository {
     final ApiClient _api;
     WhatsAppStatus _status = WhatsAppStatus.notConnected;
     Uint8List? _qrImageBytes;
     String? _connectedPhone;
     Timer? _pollTimer;

     @override
     WhatsAppStatus get status => _status;
     @override
     Uint8List? get qrImageBytes => _qrImageBytes;
     @override
     String? get connectedPhone => _connectedPhone;

     @override
     Future<void> connect() async {
       await _api.post('/whatsapp/connect/', {});
       _pollTimer?.cancel();
       _pollTimer = Timer.periodic(const Duration(seconds: 2), (_) => _poll());
       await _poll(); // check immediately too, don't wait 2s for the first check
     }

     Future<void> _poll() async {
       final data = await _api.get('/whatsapp/status/');
       final statusStr = data['status'] as String;
       if (statusStr == 'QR_READY') {
         _status = WhatsAppStatus.qrReady;
         _qrImageBytes = await _fetchQrBytes(); // GET /whatsapp/qr/, as raw bytes not JSON
       } else if (statusStr == 'CONNECTING') {
         _status = WhatsAppStatus.connecting;
       } else if (statusStr == 'CONNECTED') {
         _status = WhatsAppStatus.connected;
         _connectedPhone = data['phone'] as String?;
         _pollTimer?.cancel();
       } else if (statusStr == 'ERROR') {
         _status = WhatsAppStatus.error;
         _pollTimer?.cancel();
       }
       notifyListeners();
     }

     @override
     Future<void> disconnect() async {
       _pollTimer?.cancel();
       await _api.post('/whatsapp/disconnect/', {});
       _status = WhatsAppStatus.notConnected;
       notifyListeners();
     }

     @override
     Future<void> unlink() async {
       _pollTimer?.cancel();
       await _api.delete('/whatsapp/unlink/');
       _status = WhatsAppStatus.notConnected;
       notifyListeners();
     }
   }
   ```
   (`_fetchQrBytes()` needs a raw-bytes GET, not the JSON-decoding `ApiClient.get` from Section 3 —
   add a small `getBytes(path)` variant using `http.get` and reading `response.bodyBytes` directly.)

4. **`WhatsAppConnectionScreen` needs a small UI update** (the only screen in this entire guide that
   needs real code changes beyond swapping a repository) — replace the placeholder
   `Icon(Icons.qr_code_2_rounded)` with:
   ```dart
   repository.qrImageBytes != null
       ? Image.memory(repository.qrImageBytes!)
       : const Icon(Icons.qr_code_2_rounded, size: 110, color: AppColors.textSecondary)
   ```
   inside the same 180×180 container that's already there
   (`lib/screens/settings/whatsapp_connection_screen.dart`).

### Security note (matches your Postman doc's own warnings)

- The Gateway's `x-api-key` lives **only** in Django's server-side config/`.env` — never sent to,
  or stored on, the phone.
- The phone authenticates to **Django** (whatever session/token scheme Section 4 ends up using),
  and Django is the only thing that ever knows the Gateway exists.
- Django should store one `gatewaySessionId` per business (a column on your Business/Account model)
  — created once, reused for every future connect/reconnect, per your Postman doc's own note that
  reconnecting with saved credentials skips the QR scan.

---

## 10. Reports screen — no new backend work strictly required

`lib/core/utils/report_data.dart`'s `ReportData.build()` computes everything (totals, the sales
chart, per-customer breakdowns) **client-side**, from whatever `CustomerRepository.customers` and
`historyFor()` already return. Once Section 6's real repository is wired in, Reports works
automatically with zero additional Django endpoints — it's just aggregating real data instead of
mock data. (You could later add a dedicated `/api/reports/` endpoint to move that aggregation
server-side for performance with large datasets, but nothing requires it for correctness.)

---

## 11. Voice — confirm this needs NO backend work at all

`lib/data/voice/speech_service.dart` and `lib/data/voice/tts_service.dart` wrap the `speech_to_text`
and `flutter_tts` plugins — both run **entirely on the phone's own OS**, no network call, no Django
endpoint, nothing to build here. The only place Django touches voice indirectly is Section 7's
`speechText` field (Django deciding what text *should* be spoken), not the act of speaking it.

---

## 12. What still doesn't need touching at all

- **PDF generation** (`lib/core/pdf/document_pdf_builder.dart`) — builds invoices/statements/
  receipts/reports client-side from whatever data the repositories already returned. No backend
  involvement; keep as-is.
- **Localization** (`lib/core/localization/app_strings.dart`) — all UI text, unrelated to backend.
- **Business options / dropdown lists** (`lib/core/constants/business_options.dart`) — static
  lists (categories, currencies), no backend needed unless you want these configurable later.

---

## 13. Suggested order to actually do this work

1. Section 3 (add `http` package + `ApiClient`) — 10 minutes, unblocks everything else.
2. Section 4 (Auth) — nothing else works without a session existing first.
3. Section 5 (Business Profile) — small, needed right after Auth for onboarding to make sense.
4. Section 6 (Customers/Sales/Payments) — the biggest one, and everything else (Reports, Chat's
   customer lookups) depends on it.
5. Section 9 (WhatsApp) — biggest scope after Customers, because of the QR/polling flow; do this
   only once the Gateway itself is confirmed working end-to-end per your own Postman checklist.
6. Section 7 (Chat) — depends on whatever real AI/NLU service you're building; can be built and
   swapped in independently of everything else once ready.
7. Section 8 (Notifications) — smallest, do whenever convenient.

At every step: change one `Mock*Repository()` to your new `Django*Repository()` in `app_shell.dart`
(or `app.dart` for Auth/Business Profile), run the app, and that one feature is now real — every
other still-mocked feature keeps working exactly as before, since each domain is completely
independent of the others.
