# Project Vision — AI Automated Accountant

## Vision
Empower small and medium-sized businesses (SMEs) with an AI-powered accountant that automates
daily financial operations through natural conversation, so owners spend time growing their
business instead of doing paperwork.

Make professional accounting assistance accessible and affordable for every SME by providing a
24/7 intelligent assistant that understands voice and text in English, Urdu, and Roman Urdu,
communicates through WhatsApp, and requires no accounting knowledge to use.

## Objectives

1. **Automate daily accounting** — record sales, purchases, expenses, and payments via voice/text/photo.
2. **Simplify financial management** — auto-generate invoices, receipts, statements, payment summaries; read challans/bills from photos and post them to the correct customer record.
3. **Conversational accounting** — talk to the AI like a human accountant, in English, Urdu, or Roman Urdu.
4. **WhatsApp integration** — receive instructions and send invoices/statements/reminders/confirmations over WhatsApp.
5. **Accurate records** — automatic balance/outstanding/profit/expense calculation, minimal human error.
6. **Real-time insights** — instant reports on sales, cash flow, balances, performance.
7. **Reduce operational cost** — remove repetitive bookkeeping, affordable for businesses that can't hire a full-time accountant.
8. **Scale with growth** — single-shop today, growing SME tomorrow; architecture must not block future multi-user access.
9. **Security & privacy** — encrypted communication, secure auth, owner has full control of their data.
10. **Increase productivity** — less time on admin, more time on customers and operations.

## Core Mission
Build an AI-powered Automated Accountant that acts as a digital employee for SMEs — handling
bookkeeping, invoicing, statements, payment tracking, and financial communication through natural
conversation and WhatsApp automation, making accounting simple, affordable, and accessible.

## Product Surfaces
- **Flutter mobile app** — the owner's primary interface (dashboard, customers, entries, chat, settings).
- **Django REST backend** — single source of truth: auth, business data, sales/payments, billing/plans, orchestration of AI + PDF + WhatsApp.
- **FastAPI microservice** — isolated worker for PDF generation and image (OCR/vision) extraction, so heavy/slow jobs never block the main API.
- **WhatsApp Gateway microservice** (Node/Baileys) — sends/receives WhatsApp messages on behalf of a business, fronted entirely by Django.

## Monetization Model
Three fixed tiers (PKR 400 / 800 / 1500) plus admin-editable custom monthly plans per business.
Unpaid/free-tier businesses keep full manual bookkeeping (customers, sales, payments, reports,
PDFs) but lose access to AI Chat, voice, and image/OCR extraction — those are paid-only automation
features. See `business_logic.md` and `ARCHITECTURE.md` for enforcement details.

## Non-Goals (explicit, for now)
- No multi-user/staff accounts per business (single owner per business).
- No multi-currency accounting beyond a per-business display currency.
- No public marketplace / no advertising.
