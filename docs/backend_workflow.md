# Backend Workflow — Request Lifecycles

This describes what actually happens, step by step, for each major flow — think of it as the
"how the digital employee does its job" reference.

## 1. Auth & session
```
Phone → POST /api/auth/google/ or /api/auth/email-register/
Django → verifies with Google / creates user → creates Business (if new) → issues JWT
Phone stores JWT, sends it as Authorization: Bearer <token> on every future call
```

## 2. Plain CRUD (customers, sales, payments, notes)
```
Phone → Django REST endpoint (see BACKEND_INTEGRATION_GUIDE.md Section 6 for exact routes)
Django → checks business ownership → validates → writes to SQLite → recalculates
         balanceAfter cascade for any later entries → returns updated record(s)
```
No queue, no AI, no external call — this path must stay fast and always available, even to
unpaid/manual-only businesses.

## 3. PDF generation (invoice / statement / receipt / report)
```
Phone → POST /api/documents/generate/ {doc_type, target_id}
Django → creates JobTask(type="pdf", payload={...}) → returns {job_id} immediately
Worker loop → picks up JobTask → POST fastapi/pdf/generate → gets PDF bytes/path
            → writes JobTask.result = {file_url} → status="done"
Phone → polls GET /api/jobs/{job_id}/ until status == "done" → downloads file_url
```

## 4. AI Chat (text)
```
Phone → POST /api/chat/message/ {conversationId, text}
Django (apps/chat) → checks business.plan.has_feature("ai_chat") — if false, 402/403 with a
                      clear "upgrade to use AI Chat" error, do NOT call Groq
                    → loads recent conversation context + relevant business data
                      (e.g. customer list summary, recent sales) as needed for the prompt
                    → calls Groq via groq_client.py — 8B model for simple/low-reasoning turns,
                      70B model when drafting a bill or resolving ambiguity (see
                      ai_automation_layer.md Section 1 for the split)
                    → parses model output into {text, speech_text, draft_bill?, document_ready?}
                    → saves ChatMessage rows → returns the reply
Phone → renders bubble, optionally speaks speech_text via on-device TTS
```
This call is synchronous (Groq is fast enough for chat) — no job queue needed here, unlike PDF/image.

## 5. AI Chat (image / receipt photo)
```
Phone → POST /api/chat/image/ (multipart: conversationId, image)
Django (apps/image_info_extractor) → checks business.plan.has_feature("image_extraction")
                                    → creates JobTask(type="image_extract", payload={image, conversationId})
                                    → returns {job_id} immediately, phone shows "typing…"
Worker loop → POST fastapi/vision/extract (Gemini Vision) → gets {date, amount, customer_name?, line_items[]}
            → if a required field is missing/ambiguous (e.g. no customer match), calls Groq to
              phrase a natural follow-up question instead of failing silently
            → writes JobTask.result = {chat_message} → status="done"
Phone → polls (or Django pushes via existing chat polling) → renders the reply, which is usually
         a Draft Bill card pre-filled from the extracted data
```

## 6. Draft Bill confirm
```
Phone → "Confirm and Send" tapped
Django → validates draft still unconfirmed → writes to sales/payments (same path as Section 2's
          manual entry — Draft Bill is never a separate data model, just a chat-message-shaped
          view of an unsaved Sale) → on success, flips ChatMessage.draft_confirmed = true
        → on failure, returns an error, draft stays editable, nothing is written
```

## 7. WhatsApp connect (once per business)
```
Phone → POST /api/whatsapp/connect/
Django (apps/whatsapp) → if business has no gatewaySessionId yet:
                            POST gateway/sessions {displayName} → store gatewaySessionId
                          POST gateway/sessions/{id}/connect
Phone → polls GET /api/whatsapp/status/ → Django proxies gateway's GET /sessions/{id}/status
Phone → when status == "QR_READY", GET /api/whatsapp/qr/ → Django proxies gateway's PNG
Phone → shows QR, owner scans with WhatsApp → phone keeps polling → status flips to CONNECTED
```
Every future connect (app restart, reconnect after disconnect) reuses the stored
`gatewaySessionId` and skips the QR step, per the Gateway's own reconnect behavior.

## 8. Sending a WhatsApp message to a customer (invoice/reminder/statement)
```
Trigger (e.g. "Confirm and Send" on an invoice, or a scheduled reminder job)
Django (apps/whatsapp) → POST gateway/messages or /messages/document
                        → on RATE_LIMIT_EXCEEDED (429) or SESSION_NOT_CONNECTED (409), records a
                          Notification (type=whatsappDisconnected or similar) so the owner sees it
                          in-app, does not silently drop the send
```

## 9. Billing / plan enforcement (applies to every gated request)
```
Any request to chat/image endpoints
  → Django resolves request.user.business.plan
  → plan.has_feature(feature_name) checked via billing app (see business_logic.md)
  → if false: return 403 {code: "FEATURE_NOT_ON_PLAN", message: "..."} — Flutter shows an
    upgrade prompt, never a raw error
  → if true but usage cap exceeded (e.g. monthly message cap on a plan): return 429 with a
    clear message, same pattern as the Gateway's own RATE_LIMIT_EXCEEDED shape for consistency
```

## 10. Error shape convention (matches the existing WhatsApp Gateway's own style, kept consistent end-to-end)
```json
{ "success": false, "error": { "code": "SOME_CODE", "message": "Human-readable explanation." } }
```
Django, FastAPI, and the Gateway all use this shape so the Flutter app has exactly one error
parser for the whole system.
