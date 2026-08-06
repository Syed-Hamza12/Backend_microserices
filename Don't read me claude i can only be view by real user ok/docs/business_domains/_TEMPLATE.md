<!--
Template for a new business-domain document.

To add a new domain:
1. Copy this file to <BUSINESS_TYPE_CODE>.md in this same folder, where
   <BUSINESS_TYPE_CODE> is exactly one of the codes in
   apps/accounts/models.py's Business.BUSINESS_TYPE_CHOICES (e.g. TAILOR.md,
   POULTRY.md). The filename IS the lookup key — apps.chat.domain_knowledge
   maps business.business_type straight to "<code>.md", nothing fuzzier.
2. Fill in every section below with REAL Pakistani market knowledge for that
   trade. No placeholders, no generic "international business" content —
   write it the way an experienced Pakistani accountant who has actually
   worked with that trade would explain it to a new hire.
3. Keep every H1 ("# Heading") exactly as named below — apps.chat.
   domain_knowledge._SUMMARY_SECTIONS pulls specific sections by exact
   heading text into the condensed prompt context. A renamed or missing
   heading silently drops that section from what the AI actually sees,
   even though it's still in the file for human readers.
4. This file is a REFERENCE DOCUMENT for human maintainers and for the
   condensing logic to extract from — it is never pasted into an LLM call
   in full. "# Conversation Examples" in particular exists to document
   behavior for whoever next extends this system, not to be injected;
   apps.chat.domain_knowledge deliberately skips it.
5. No entry is needed anywhere else — apps.chat.domain_knowledge picks the
   file up automatically the next time that business_type is used, no
   restart required (it re-reads whenever the file's mtime changes).
-->

# Business Overview

<!-- How this specific trade actually works in Pakistan: who runs it, typical
shop size, supply chain (importer/wholesaler/retailer relationships),
seasonality, how digital vs. cash-heavy it typically is. -->

# Typical Customers

<!-- Retail / Wholesale / Factories / Contractors / Walk-in / Regular /
Credit / VIP — which of these actually apply to this trade and how. -->

# Typical Products

<!-- Many REAL Pakistani product examples for this trade, with real brand/
size/grade naming conventions where relevant. -->

# Common Units

<!-- Piece, Kg, Gram, Roll, Bundle, Packet, Dozen, Gross, Feet, Inch, Meter,
Yard, Liter, Bag, Carton, etc. — only the ones this trade actually uses,
and how they're actually said/abbreviated. -->

# Daily Workflow

<!-- Morning -> receiving stock -> selling -> recording credit -> receiving
payments -> closing accounts -> end of day, as it ACTUALLY happens for this
trade, not a generic retail description. -->

# Accounting Workflow

<!-- How owners in this trade usually record sales, credit, cash, advance,
partial payments, outstanding balances, returns, corrections. -->

# Common Pakistani Vocabulary

<!-- Real words owners actually use — not textbook language. -->

# Roman Urdu Variations

<!-- Real spelling variants for common words in this trade's vocabulary
(dates, ledger, payment, etc.) — tareekh/tarikh/tareek/tarek style. -->

# Product Nicknames

<!-- Real market nicknames for products in this trade. -->

# Product Categories

<!-- How this trade's own products are naturally grouped. -->

# Owner Behaviour

<!-- How much owners in this trade typically omit, how they give
instructions, how they correct mistakes, how they refer to customers. -->

# Customer Behaviour

<!-- How customers in this trade typically talk to the shop. -->

# Common Payment Behaviour

<!-- Cash / Credit / Monthly settlement / Dealer payments / Factory
payments, as it actually happens in this trade. -->

# Outstanding Balance Behaviour

<!-- How credit/udhaar is actually tracked and chased in this trade. -->

# Clarification Rules

<!-- When the AI should ask, when it should infer, when it should never
guess, SPECIFIC to this trade's real ambiguities. -->

# Frequently Forgotten Information

<!-- What owners in this trade routinely leave out of a spoken/typed
instruction, that the AI needs to either infer correctly or ask about. -->

# Voice Examples

<!-- Realistic Pakistani voice-transcript style for this trade — including
believable transcription artifacts (mid-word cutoffs, number/word
confusion, code-switching). -->

# Conversation Examples

<!-- 25-50+ realistic, DIVERSE conversations — each one should teach a
DIFFERENT behavior or reasoning pattern (sale, credit, partial payment,
full settlement, balance query, statement/invoice request, return,
exchange, correction, discount, transport charge, advance payment,
multiple products in one message, voice-transcript errors, Roman Urdu
spelling variation, mixed Urdu/English, ambiguous request, follow-up
question, trade-specific edge case). Never pad with near-duplicates that
only change a name or amount. -->

# Common AI Mistakes

<!-- Mistakes specific to this trade the AI must avoid. -->

# Best Practices

<!-- How the AI should behave in this specific trade. -->
