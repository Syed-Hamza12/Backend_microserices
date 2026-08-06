# Business Overview

A Pakistani medical store (pharmacy) sells medicines, first-aid items, baby products, and often
basic medical devices (thermometers, BP monitors, nebulizers). Most are independently owned
neighborhood stores, some run by a qualified pharmacist, many run by the owner with a hired
counter attendant. Stock comes from pharmaceutical distributors/wholesalers, usually on short
credit terms (weekly/bi-weekly settlement), and is expiry-sensitive — near-expiry stock is
tracked and sometimes returned to the distributor under return policies specific to that
distributor. Customers range from walk-in one-off buyers to regular patients with chronic
conditions (diabetes, blood pressure, thyroid) who buy the same medicines monthly, often on
credit, especially if they're known to the owner or referred by a nearby doctor's clinic. Doctor
referrals are a significant customer source — many pharmacies sit next to or near clinics and get
steady prescription-driven traffic. Cash is dominant at the counter; credit is extended mostly to
known regulars, not new customers, because medicine margins are thin and trust matters more here
than in most retail.

# Typical Customers

- **Walk-in retail** — one-off buyers, mostly cash.
- **Regular/chronic patients** — monthly repeat buyers (diabetes, BP, thyroid, etc.), often on
  credit settled monthly.
- **Doctor-referred customers** — prescription-driven, sometimes first-time but become regulars.
- **Credit/khata customers** — known families, settled monthly or as convenient.
- **Emergency/urgent buyers** — small quantities, always cash, rarely tracked as a customer.
- **Other clinics/small hospitals nearby** — occasional bulk purchases on account.

# Typical Products

Panadol, Panadol CF, Brufen, Disprin, Augmentin, Calpol (pediatric), ORS (Oral Rehydration
Salts), cough syrups, Rigix, antacids (Risek, Gaviscon), Piriton, insulin (various brands),
Glucophage/Metformin, BP medicines (Concor, Norvasc, Tenormin), thyroid medicine (Eltroxin),
vitamins/supplements (Surbex, Centrum), baby formula and diapers, bandages/cotton/dressing,
syringes, Dettol/antiseptics, thermometers, BP monitors (Omron), nebulizer machines and kits,
condoms and family planning items, skin creams (Betnovate, etc.), eye/ear drops.

# Common Units

- **Strip** — the most common medicine unit (a strip of 10 tablets, typically).
- **Tablet/Goli** — individual tablets, sometimes sold loose for small quantities.
- **Box** — a full box containing multiple strips, or a single-item box (syrup, ointment).
- **Bottle** — syrups, liquid medicines.
- **Vial/Ampule** — injectable medicines.
- **Piece** — devices (thermometer, BP monitor), diaper packs, individual items.
- **Course** — a full prescribed course of a medicine (a bundled multi-item concept, not a
  physical unit, but owners sometimes bill "1 course" as a package).

# Daily Workflow

Morning: check overnight/urgent walk-ins, verify stock against yesterday's closing. Through the
day: counter sales (mostly cash, quick), regular customers picking up monthly medicine (often
credit), occasional doctor-referred new customers. Owner or pharmacist checks expiry dates
periodically and separates near-expiry stock. Evening: cash reconciliation against the day's
sales, credit account updates for regulars who took medicine without paying, and any distributor
orders placed for restocking. End of month: settle distributor credit, chase outstanding
customer balances, especially for regulars who've built up a running tab.

# Accounting Workflow

- **Cash sale**: the default for most walk-in purchases, recorded per transaction.
- **Credit/khata sale**: recorded against a known regular's running balance, common for chronic
  medicine buyers, settled monthly.
- **Partial payment**: less common than in other trades since amounts are usually small, but
  happens for larger orders (e.g. a full month's diabetes medicine bought at once).
- **Full settlement**: "Ali ne is mahine ka pura hisaab de diya" — clears the running balance.
- **Returns**: medicine returns are unusual for a walk-in customer (health/safety reasons) but
  distributor-side returns for near-expiry stock are common — these are a different kind of
  transaction (supplier-side, not customer-facing) that this app's customer-ledger model doesn't
  directly represent; the AI should recognize the distinction and ask rather than log a
  distributor return as a customer sale reversal.
- **Corrections**: quantity/item corrections happen when a substitute medicine was given instead
  of what was originally billed.

# Common Pakistani Vocabulary

Dawai (medicine), Goli (tablet), Strip, Sui/Injection, Capsule, Syrup, Nuskha (prescription),
Doctor ki parchi, Khata, Udhaar, Pura mahine ka hisaab, Bill/Parchi, Cash counter, Distributor/
Company wala, Expiry date, Stock khatam, Regular customer/patient.

# Roman Urdu Variations

- tareekh / tarikh / tareek / tarek
- khata / khatha / khta
- bhej / bejh / bhj
- jama / jma
- likh / lkh
- dawai / dawa / dawaii
- nuskha / nuska / nusqa
- parchi / parchee / parci
- hisaab / hisab / hisaub
- baqi / baaki / baki
- goli / goly / goliyan (plural)

# Product Nicknames

"Panadol" (used generically for any paracetamol, brand or not — like "Xerox" for photocopying),
"CF" (Panadol CF, flu variant), "Disprin" (used generically for any fast-dissolving pain
tablet), "ORS" (rehydration salts), "Drip" (IV fluids/injections colloquially), "Sui" (any
injection), "Syrup" (any liquid medicine, especially for children), "BP ki dawai" (blood
pressure medicine, generic reference regardless of brand), "Sugar ki dawai" (diabetes medicine).

# Product Categories

Pain/fever relief, Antibiotics, Cough & cold, Digestive/antacids, Chronic disease medicine
(diabetes, BP, thyroid), Pediatric/baby products, Vitamins/supplements, First-aid & dressing,
Devices (thermometer, BP monitor, nebulizer), Skin/topical, Family planning.

# Owner Behaviour

Pharmacy owners/attendants speak in short, medicine-name-first bursts — "2 Panadol strip, 1 ORS"
— and rarely narrate pricing aloud for standard items since prices are fixed and known. For
regular/chronic patients, owners often just say "Ali ka mahine wala" (Ali's monthly one) without
listing items, expecting the AI/system to know or ask what "Ali's monthly one" usually includes.
Corrections happen fast when a substitute item was given ("wo dawai nahi thi, dusri di thi").
Owners refer to regulars by name and sometimes by condition ("sugar wale Ali sahab").

# Customer Behaviour

Walk-in customers rarely discuss credit. Regular/chronic patients often just name their usual
medicine set or say "mera mahine wala de do" (give me my monthly one) without listing items,
relying on the pharmacy's memory of their routine purchase.

# Common Payment Behaviour

Cash dominant for one-off/walk-in. Credit extended selectively to known regular/chronic patients,
settled monthly. Distributor payments to the pharmacy's own suppliers are typically weekly or
bi-weekly credit terms — a cost/payable the owner tracks separately from customer sales, not to
be confused with a customer transaction.

# Outstanding Balance Behaviour

Regular patients' balances are usually modest (a month's worth of routine medicine) and settled
predictably at month-end or on a pension/salary date. Owners are generally lenient with known
regulars given the health-necessity nature of the purchases, but do track and gently remind.

# Clarification Rules

- If "Ali ka mahine wala de do" (Ali's monthly one) is said with no item list, check Ali's own
  recent purchase history first for his usual medicine set before asking; if no clear pattern
  exists, ask what's needed.
- Never guess a specific medicine brand/strength from a vague symptom description ("bukhar ki
  dawai" / fever medicine) — if multiple items in this business's own history could match, ask
  which one, or record generically only if the owner is comfortable with that.
- Distributor-side stock returns are NOT a customer transaction — if a message sounds like a
  stock/supplier return rather than a customer sale reversal, ask for clarification rather than
  logging it against a customer.
- Quantity ambiguity between "strip" and "tablet" matters for price — never assume one when the
  other is stated or implied.

# Frequently Forgotten Information

Owners often forget to state whether a quantity is strips or loose tablets, whether this is a new
customer or an existing regular being restocked, and whether an amount given is per-item or a
total. For chronic patients, owners often omit the actual medicine names entirely, relying on
"usual" — the AI should check history rather than ask every time this happens, but should ask if
no clear pattern exists yet.

# Voice Examples

"do Panadol strip... ek ORS... cash"

"Ali sahab ka mahine wala de do... sugar wali... jaisa hamesha"

"ye dawai wapis aa gayi thi, expiry qareeb thi, company ko wapis bhej di"

"teen sau ka bill tha Ahmed ka... udhaar likh do"

# Conversation Examples

1. **Simple cash sale, specific items**
Owner: "2 Panadol strip, 1 ORS, cash, 250"
AI: Records a cash sale of 2 Panadol strips + 1 ORS totaling 250, no customer needed for a walk-in.

2. **Regular chronic patient, "usual" medicine**
Owner: "Ali sahab ka mahine wala de do"
AI: Looks up Ali's own recent purchase history to identify his usual monthly medicine set and proposes a matching draft; if no clear recurring pattern is found, asks what he needs this month.

3. **Credit sale to known regular**
Owner: "Ahmed ka udhaar likh do, teen sau"
AI: Records a 300 credit sale against Ahmed's balance.

4. **Full monthly settlement**
Owner: "Ali ne is mahine ka pura hisaab de diya"
AI: Proposes recording a full-balance payment for Ali using his real current outstanding balance, never a guessed figure.

5. **Partial payment on a larger order**
Owner: "Ahmed ne 500 diye, baaki abhi udhaar hai"
AI: Records a 500 payment against Ahmed's balance without assuming it fully settles whatever the current outstanding amount is.

6. **Balance inquiry**
Owner: "Ali sahab ka kitna baaki hai"
AI: Answers from the real recorded balance only.

7. **Ambiguous strip vs. tablet quantity**
Owner: "5 Brufen de do"
AI: If "5" could plausibly mean 5 strips or 5 loose tablets and it isn't clear from context/price, asks which was meant rather than assuming.

8. **Substitute item correction right after billing**
Owner: "wo dawai nahi thi, doosri di thi, Brufen ki jagah Panadol"
AI: Recognizes this as a correction to the just-created entry's item, proposing an edit rather than a new, contradictory sale.

9. **Distributor-side return, not a customer transaction**
Owner: "ye stock company ko wapis bhej diya, expiry qareeb thi"
AI: Recognizes this is a supplier/distributor return, not a customer sale reversal, and asks the owner how they'd like it noted rather than logging it against any customer.

10. **New customer with a doctor referral**
Owner: "naya customer hai, doctor sahab ne bheja hai, iska naam likh lo, Sara"
AI: Proposes adding Sara as a new customer (checking for near-duplicate names first, per this system's existing duplicate-guard behavior) before recording her purchase.

11. **Multiple items, mixed known/unknown pricing**
Owner: "1 Augmentin box 450, 1 Calpol syrup, ORS 2 packs — total 900"
AI: Records all items under the stated total; if Calpol/ORS individual prices aren't stated and aren't needed since the total is given, does not force item-level pricing.

12. **Voice transcript with hesitation and self-correction**
Owner (voice): "do Panadol... nahi teen Panadol strip... aur ek ORS... Ahmed ke liye"
AI: Reconstructs the corrected final quantity (3 Panadol strips, not 2) rather than the first-spoken number, recognizing the self-correction mid-sentence.

13. **Roman Urdu variant for prescription/date**
Owner: "doctor ki parchi pe 5 tareek ki dawai likhi thi, wahi de do"
AI: Recognizes "tareek" refers to a date on the prescription and asks for the actual medicine names if they weren't otherwise stated, since a date alone doesn't identify what to sell.

14. **Mixed Urdu/English**
Owner: "Ahmed's monthly diabetes medicine likh do, credit pe"
AI: Records Ahmed's diabetes medicine purchase as credit, using his known recurring set if on file.

15. **Follow-up referencing the just-made draft**
Owner: "bill banao" → AI drafts → Owner: "isko Ahmed ke number pe bhej do"
AI: Understands "isko" refers to the just-created draft/bill, proceeding to prepare it for WhatsApp delivery to Ahmed rather than asking which bill.

16. **Vague reference to a prior day's entry**
Owner: "kal wali Ahmed ki entry ghalat thi, hatao"
AI: Locates yesterday's specific entry for Ahmed and proposes removing/reversing it, asking for clarification if more than one entry exists for him that day.

17. **Statement request for a chronic patient**
Owner: "Ali sahab ki is saal ki statement bhej do"
AI: Prepares a statement covering the requested period and queues it for delivery, correctly stating plainly if WhatsApp isn't connected rather than claiming success.

18. **Small emergency cash purchase, no customer tracking needed**
Owner: "ek ORS packet, cash, 60"
AI: Records a simple 60 cash sale with no customer record required.

19. **Trade-specific edge case: injection/vial sold with a note about who administers it**
Owner: "1 sui de di Ahmed ko, ghar pe khud lagayega"
AI: Records the sale of the injection to Ahmed; does not infer anything about administration method into the ledger entry itself (not this system's concern) beyond the item and amount.

20. **Trade-specific edge case: bulk purchase by a nearby clinic**
Owner: "clinic wale aaj bulk mein 10 box Augmentin le gaye, udhaar pe"
AI: Records a bulk credit sale of 10 boxes of Augmentin to the clinic customer.

21. **Ambiguous "the usual" for a NEW customer with no history**
Owner: "Sara ka wohi usual de do"
AI: If Sara is a new customer with no purchase history on record, this cannot be inferred — asks what "usual" refers to rather than guessing from another customer's pattern.

22. **Discount for a known regular**
Owner: "regular customer hai, thora kam kar diya, 400 ki jagah 350 le liye"
AI: Records the sale at the actual charged amount (350), not the list price.

23. **Owner asks what a specific patient bought last time**
Owner: "Ali sahab ne pichli baar kya liya tha"
AI: Answers from the real most recent entry for Ali — never invents an item if none is on record.

24. **Correction of a wrongly attributed sale**
Owner: "wo bill Ahmed ka nahi, Bilal ka tha"
AI: Proposes transferring the just-created entry from Ahmed to Bilal rather than duplicating it.

25. **Owner asks for total credit exposure across regulars**
Owner: "sab regular customers ka mila ke kitna udhaar hai"
AI: Answers using a real aggregate query across all customers with outstanding balances — never sums only a partial recently-seen subset.

26. **Expiry-driven stock note mistaken for a sale**
Owner: "ye dawai expire ho gayi, bill mat banao, sirf note kar lo"
AI: Recognizes explicitly that no sale should be recorded here — this is inventory/expiry tracking, which this system doesn't model — and does not create a spurious zero-value entry.

27. **Ambiguous pronoun after two drafts discussed**
Owner: "ye bhi Ali sahab ka hai" (after two different pending drafts were just mentioned)
AI: If it's genuinely unclear which of the two drafts this refers to, asks rather than guessing.

# Common AI Mistakes

- Assuming "the usual"/"mahine wala" for a brand-new customer with no purchase history.
- Confusing strip vs. tablet quantity when the distinction changes the price materially.
- Logging a distributor/supplier-side stock return as a customer transaction.
- Treating a self-corrected voice quantity ("do... nahi teen") as the first-spoken number instead
  of the corrected one.
- Inferring a specific medicine from a vague symptom description without checking history or
  asking.
- Recording an expiry/inventory note as a sale.

# Best Practices

Lean on this business's own purchase history heavily for regular/chronic patients before asking
— that's how a real pharmacist would behave, remembering a regular's routine. Keep the
strip/tablet/box unit distinction precise, since it directly changes price. Recognize and
correctly route same-transaction corrections versus genuinely new sales. Never conflate
supplier-side stock activity (returns, expiry) with customer-facing sales.
