# Production Readiness

Pagination, media serving and artifact cleanup — the things that work fine on a
laptop with ten records and fail on a real deployment.

## Pagination

All list endpoints are paginated:

| Endpoint | Notes |
|---|---|
| `GET /api/customers/` | |
| `GET /api/customers/<id>/history/` | |
| `GET /api/notifications/` | |
| `GET /api/documents/deliveries/` | |

Query params: `limit` (default 200, max 500) and `offset`.

### The response shape does not break existing clients

`data` stays a plain array; paging information goes in a sibling `page` key:

```json
{
  "success": true,
  "data": [ ... ],
  "page": {"limit": 200, "offset": 0, "total": 512, "has_more": true}
}
```

DRF's default would have wrapped the rows in `{"results": [...]}`, breaking
every existing client on the day it shipped — for a change meant to be invisible
to them. A client that ignores `page` still works; it just sees the first page.

Offset/limit rather than cursor: these are small per-business lists, always
filtered by business and ordered deterministically, so the cost and stale-page
problems that justify cursor pagination don't arise.

### The mobile app pages through in full

`ApiClient.getAllPages()` follows `has_more` until the list is exhausted.

This matters more than it looks. Running balances, statement rows and report
totals are all computed **client-side from the complete ledger**, so a partial
history would not look like missing rows — it would look like **wrong money**.
Pagination bounds the size of any single response; fetching every page is what
keeps the figures right. A `maxPages` guard stops a server that always reported
`has_more` from looping forever.

## Media in production

Django only serves `MEDIA_URL` when `DEBUG` is on. With `DEBUG=False` every
`/media/` URL 404s.

That affects less than it used to:

- **Generated documents** are transient — rendered in memory, streamed to the
  Gateway, discarded. They never have a URL at all (see `document_delivery.md`).
- **Chat image uploads** are read server-side by the extraction worker. The app
  displays the local file it just picked, never the server's copy, so these need
  no public URL either.
- **Business logos** are the only media the app actually displays.

So instead of requiring nginx or an object-store bucket for one small image:

### `GET /api/business/logo/file/`

Authenticated, streams the logo bytes for the caller's own business. This is
also the tenant-safe answer — a public `/media/` path is guessable and
unauthenticated, whereas this can only ever return the caller's own logo. The
path is checked for containment under `MEDIA_ROOT` before anything is read.

`BusinessSerializer` exposes `has_logo` so the app knows whether to fetch at all.
`logo_url` is still returned for local development.

The Flutter side uses `BusinessLogo`, which fetches through `ApiClient` and
renders with `Image.memory` — the same pattern already used for QR codes and
document previews.

**When to revisit:** if media volume or traffic grows, move `/media/` behind
nginx or object storage and serve signed URLs. Nothing in the app depends on
how the bytes arrive, only on the endpoint.

## Cleanup

```bash
python manage.py cleanup_artifacts            # run daily
python manage.py cleanup_artifacts --dry-run  # report only
```

Removes:

- **Finished jobs** (`done`/`failed`) older than 7 days. `queued` and
  `processing` rows are never removed regardless of age — work that hasn't run
  is not garbage.
- **Expired undo tokens** — one-shot 5-minute revert tokens that can never be
  used again.
- **Orphaned uploads** — files under `media/uploads/` that no `ExtractionJob`
  references *and* are older than 7 days. Checked against the database rather
  than by age alone, so a file a queued job still needs is never deleted out
  from under it.

**Business data is never touched.** Sales, payments, customers, chat history and
delivery audit rows are permanent records. A test asserts this.

### Scheduling

Windows (Task Scheduler):

```
schtasks /create /tn "AccountantCleanup" /tr "D:\...\venv\Scripts\python.exe D:\...\accountant_backend\manage.py cleanup_artifacts" /sc daily /st 03:00
```

Linux (cron):

```
0 3 * * * cd /path/to/accountant_backend && ../venv/bin/python manage.py cleanup_artifacts
```

Also worth scheduling: `python manage.py expire_subscriptions` (daily), which
flips lapsed subscriptions to `expired`.

## Testing

```bash
python manage.py test apps.accounts    # 21 pagination / media / cleanup tests
python manage.py test                  # 99 total
```

Covered specifically: paging through returns every row **exactly once**,
ordering is stable across pages, `data` is still an array, limits are capped,
nonsense parameters fall back to defaults, a referenced upload survives
cleanup, and business data is untouched.
