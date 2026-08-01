# Feature List

## Always available (all plans, including unpaid/Basic)
- Auth: Google sign-in, email registration
- Business Profile: name, category, logo, currency, language
- Customers: add/edit, search, balances, notes, per-customer history
- Sales & Payments: add/edit/delete entries, multi-line-item sales, 4 payment methods, live balance preview, forward-only balance cascade on edits
- Invoices, Statements, Receipts: generation and PDF preview/share
- Reports: sales/payment/customer breakdowns, date ranges, sales-over-time chart, PDF export
- Notifications inbox
- Settings: currency, language, WhatsApp connection UI (connect/see status even if sending is gated — see `business_logic.md` Section 2 scope note)
- Voice: on-device speech-to-text and text-to-speech (not billed — no backend cost)

## Gated — requires `ai_chat` feature (Standard+)
- AI Chat text conversation — natural-language Q&A about the business ("today's sales," "top 5 customers," etc.), draft-bill creation via text or voice
- Spoken AI replies (`voice_reply`, may be bundled with `ai_chat` depending on plan config)

## Gated — requires `image_extraction` feature (Premium+)
- Attach-photo-to-chat: reading a challan/receipt photo, extracting date/amount/customer/line items, auto-drafting a bill

## Gated — requires `whatsapp_send` feature (Premium+ / per business_logic.md scope decision)
- Sending invoices, statements, payment confirmations, and reminders to customers over WhatsApp

## Admin-only (Django admin, not exposed to the phone)
- Manage Plans and PlanFeatures (prices, which features, usage caps)
- Manually assign/override a business's Subscription (custom monthly deals)
- View JobTask queue health, UsageCounter per business
- View/manage WhatsApp Gateway sessions if troubleshooting is needed

## Explicitly out of scope for now (flag before building)
- Multi-user/staff accounts per business
- In-app payment collection (JazzCash/EasyPaisa gateway integration) — plan changes are manual/admin-driven for now
- Import Data (Excel), Export Data, Data Backup, Printer Settings, scheduled Reminders, Help & Support, Rate Us, Share App — all UI stubs today per `USER_WORKFLOW.md`, no backend planned until explicitly prioritized
