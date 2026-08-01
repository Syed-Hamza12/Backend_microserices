# Business Date, Timezone & Trust Controls

## Business timezone

`TIME_ZONE = Asia/Karachi` (override with `DJANGO_TIME_ZONE`).

Timestamps are still **stored in UTC** (`USE_TZ = True`); this only decides how
they are interpreted as business dates.

It was UTC, which put the day boundary at 05:00 Pakistan time. A sale entered at
1am appeared on the previous day's report and printed **yesterday's date on the
customer's invoice** — the kind of discrepancy that makes an owner stop trusting
the numbers.

Everything derived from a date now follows the business calendar:

| Place | Mechanism |
|---|---|
| AI "today's sales" | `timezone.localdate()` + `timestamp__date` |
| Invoice / receipt dates | `_display_date()` → `localtime()` |
| Statement & report ranges | `_parse_range()` → `make_aware()` |
| Monthly usage quota | `timezone.localdate().replace(day=1)` |

That last one needed an explicit fix — `timezone.now().date()` is a *UTC* date,
so plan quotas were resetting five hours late every month.

## The accounting date

`ActivityEntry.timestamp` is the **business date**. `created_at` is the system
clock, for auditing. The two are never mixed.

### Owners can name a date in chat

> "Record this sale on 25 July" · "Create invoice for yesterday" · "Bill dated 1 August"

The AI puts it in `draft_bill.date` and **the server resolves it**
(`apps/sales/business_date.py`). The model is given today's business date and
asked for an absolute `YYYY-MM-DD`, but whatever it returns — absolute or
relative — is re-resolved here.

That indirection is the point: a model that fumbles "yesterday" would otherwise
put a sale on the wrong day of the ledger and silently shift every balance after
it. Date arithmetic is not something to delegate.

Accepted: `today`, `yesterday`, `N days ago`, `last monday`, `YYYY-MM-DD`, and a
`date`/`datetime`. Anything else is **refused, not guessed**.

### Three supported scenarios

| Scenario | Meaning |
|---|---|
| **Past** | Backdated entry — catching up on paperwork |
| **Today** | Default when no date is given |
| **Future** | A planned/scheduled bill |

### Rules, enforced on every path

- **±365 days.** Beyond that in either direction is almost always a mistyped
  year: backwards it rewrites the ledger behind it, forwards it sits un-matured
  and invisible for years.
- **"kal" is refused as ambiguous.** In Urdu it means *both* yesterday and
  tomorrow. With entries datable in both directions, picking one is a coin flip
  that lands money on the wrong side of today, so the AI asks instead.
- Applied identically to the AI path, the manual form, and AI-proposed edits —
  the manual form is where most entries come from, so the rule can't live only
  on the AI side.

## Balances with future-dated entries

**`current_balance` is what is owed *today*.** Future-dated entries are walked
(so their own `balance_after` is correct) but do not move it.

This is the single most important rule here. Before it, adding a 5,000 sale
dated 15 August made the customer's balance read 6,000 on 31 July — a figure
that flowed into the dashboard, the customer list, every statement's "Current
Balance", and `previous_balance` on the next AI draft. The owner would chase a
customer for a bill that isn't due.

**`projected_balance`** is where the balance lands once everything scheduled has
matured. Equal to `current_balance` when nothing is future-dated. Stored rather
than computed, so listing customers needs no per-row query and a scheduled bill
is never invisible between creation and its date.

```
Today = 31 Jul
  31 Jul  sale 1000   balance_after 1000
  15 Aug  sale 5000   balance_after 6000  (projected)

current_balance   = 1000   ← what the dashboard shows
projected_balance = 6000
```

### Maturity job — required, not optional

```bash
python manage.py apply_matured_entries      # run daily
```

`current_balance` is only written by `recalculate_balances`, which runs on
write. Nothing writes on the day a future entry matures, so without this command
a scheduled bill would sit in the ledger and **never become owed** — worse than
not supporting future dates at all.

Recalculates only customers with entries dated within the last couple of days or
later, so it stays cheap. Each customer runs in its own transaction: one failure
must not roll back corrections already made for everyone else.

Schedule it alongside `cleanup_artifacts` and `expire_subscriptions`.

## Statements and reports from the AI

`draft_document` lets the owner say *"statement for Ali from 1 July to 31 July"*.
Distinct from `document_ready`, which requires a URL the model was handed and
must never invent — this is a *request*, confirmed by the owner, then built by
the server from the ledger and handed to the existing document pipeline.

`POST /api/chat/draft/<id>/confirm-document/` re-resolves both dates
server-side, re-validates the customer against the business, rejects a reversed
range, and carries the same single-claim guard as the other confirms.

## The owner always sees the date

The draft bill card shows the accounting date on every draft — including when
the AI didn't name one, where it reads *"31 Jul 2026 · today"*. A backdated or
scheduled draft is labelled as such. Confirming is always an informed act;
discovering the date afterwards is how ledgers get disputed.

## Audit

`EntryChangeLog` records, for every AI-created entry: the resolved
`business_date`, and `was_backdated` / `was_scheduled` flags. "Which date did the
AI put this on, and was it moved off today?" is answerable from the audit trail
alone.

### Time-of-day and ordering

A chosen date combines with the **current clock time**
(`to_entry_timestamp()`). Entries sort by `(timestamp, id)`, so three sales
backdated in one sitting keep the order they were entered rather than all
landing at midnight.

Backdating inserts into the middle of the ledger; `recalculate_balances()`
already walks from `opening_balance` and renumbers everything after the
insertion point, so balances stay correct with no extra work.

## Delivery status is honest

`DocumentDelivery.STATUS_CHOICES`:

| Value | Meaning |
|---|---|
| `pending` | Queued |
| `sending` | Being rendered / sent |
| `accepted` | **WhatsApp accepted the message** |
| `failed` | Did not go out |

There is deliberately **no `delivered` or `read`**. Baileys returns once WhatsApp
has accepted the message and exposes no receipt we act on. The old `sent` status
(and the app's "Sent on WhatsApp") claimed more than the system can observe; the
app now says *"Accepted by WhatsApp — delivery to the customer is not confirmed
here."*

Migration `0005` converts existing `sent` rows.

## Document quota

`POST /api/documents/send/` now calls `enforce_feature_gate(business,
"whatsapp_send")`, consuming the monthly cap exactly as an ad-hoc text send
does. `HasFeature` only answers *"is this feature on the plan"* — it never
touches `UsageCounter`, so document sends, the main product action, were
unmetered while text reminders were counted.

A failed send **refunds** the slot (`_fail()` → `refund_feature_usage`).

## Editing an entry whose document was already sent

`ActivityEntrySerializer.document_sent` is true once a delivery for that entry
reached `accepted`. The app warns before saving such an edit — the customer is
holding a bill that will stop matching the books — with **Cancel** /
**Continue editing**, then offers **Resend updated document**.

A `failed` delivery does not count as sent.

## Concurrent edits (multi-device)

Optimistic locking on the row's own `updated_at`. The client sends
`expected_updated_at`; if the stored value has moved on, the edit is refused
with **409 `ENTRY_MODIFIED`** instead of overwriting.

Chosen over row locking deliberately: `select_for_update` is a **no-op on
SQLite**, so a lock-based approach would look correct and do nothing on the
database this actually runs on. A timestamp comparison behaves identically on
both engines, and needs no schema change.

The field is **optional**, so existing clients keep working — they simply don't
get the protection until they send it.

> Comparison tolerance is 1ms, not one second. Rounding to whole seconds — the
> obvious shortcut — defeats the guard entirely, because two devices saving
> within the same second is precisely the collision being caught. This was
> caught by a test that expected 409 and got 200.

## Tests

```bash
python manage.py test apps.sales.test_business_date   # 16
python manage.py test                                 # 116
```

Covering: timezone boundary at 1am Karachi, every relative phrase, future and
absurd-date rejection, backdated insertion keeping balances correct, `created_at`
staying the system clock, stale-edit rejection preserving the first write, and
`document_sent` reflecting only accepted deliveries.
