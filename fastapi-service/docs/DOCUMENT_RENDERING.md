# Document Rendering Service

FastAPI is the **only** document renderer on the platform. Django owns the
business data and decides *what* a document says; this service turns a payload
into bytes. Nothing else — not Django, not the mobile app — renders invoices,
receipts, statements or reports.

That single-renderer rule is the point: when the phone rendered its own PDFs,
there were two independent layout definitions for the same invoice, guaranteed
to drift apart.

## Endpoint

### `POST /documents/render`

Requires the `X-Internal-Key` header.

```json
{
  "doc_type": "invoice",       // invoice | receipt | statement | report
  "format":   "image",         // pdf | image   (default: pdf)
  "business_id": 1,
  "payload": { ... }           // built by Django from the database
}
```

**Returns the raw file bytes** — `image/png` or `application/pdf` — not JSON and
not a URL.

Response headers:

| Header | Meaning |
|---|---|
| `X-Document-Type` | Echo of `doc_type` |
| `X-Document-Format` | **The format actually produced** |
| `X-Document-Format-Requested` | The format that was asked for |

> **Record `X-Document-Format`, not what you requested.** A bill too long to be
> readable as an image is delivered as a PDF instead (see below), and the
> delivery record has to reflect what the customer actually received.

Errors use the platform envelope with `RENDER_FAILED` (422) or `RENDER_ERROR` (500).

### `GET /documents/formats`

Returns which formats each `doc_type` supports, so callers can offer only the
choices that work instead of discovering a limitation via a 422 after the owner
has already tapped Send.

```json
{"invoice": ["pdf","image"], "receipt": ["pdf","image"],
 "statement": ["pdf"], "report": ["pdf"]}
```

## Formats

| Document | Default | Also available |
|---|---|---|
| Invoice / Bill | **Image** | PDF |
| Receipt | **Image** | PDF |
| Statement | **PDF** | — |
| Report | **PDF** | — |

Bills default to an image because customers read them inline in WhatsApp without
downloading anything. Statements and reports are multi-page by nature and are
PDF-only — requesting an image for one is refused rather than truncated to page
one, because silently dropping rows from a financial statement is unacceptable.

### One template, both formats

The image is produced by **rasterizing the PDF**, not by a separate drawing
routine:

```
bill_image.html ──xhtml2pdf──> PDF ──pypdfium2──> PNG ──autocrop──> bytes
```

So the two formats cannot show different figures. A separate image code path
would be a second layout to keep in sync by hand.

Bills use `templates/bill_image.html`, a narrow (100 mm) layout sized for a
phone screen — an A4 page rasterized for WhatsApp would be mostly white space.

### Automatic PDF fallback

A bill spanning more than `MAX_IMAGE_PAGES` (2) pages is returned as a **PDF**
even when an image was requested. Stacking many pages produces an extremely tall
PNG, and WhatsApp downscales large images hard enough to make the figures
unreadable — an unreadable bill is worse than a PDF the customer taps to open.
The substitution is reported in `X-Document-Format`, never silent.

Within the 2-page limit, pages are **stacked vertically into one image** rather
than truncated.

## Nothing is stored

This service writes no files. Rendered documents are transient artifacts:
Django streams the bytes to the WhatsApp Gateway and drops them. The database
is the permanent business record — if a customer deletes a document, the owner
regenerates it from the stored data.

Returning bytes rather than a file path is what makes that true structurally:
there is no file to forget to clean up, and no URL that can outlive the send.
A test (`test_nothing_is_written_to_disk`) pins this.

## Language

**English only.** Urdu and Roman Urdu are for talking to the AI assistant;
official documents stay English. Do not add RTL/Urdu font handling here.

## Safety

- **Autoescape is on.** Business names, customer names and item names are user-
  or OCR-supplied, and xhtml2pdf interprets markup and resource references while
  building the PDF. Unescaped, an item name containing `<img src="file:///...">`
  read off a photographed bill would be acted on rather than printed — in a
  document that is then sent to a customer. Tests assert escaping on the
  rendered HTML, which is where it actually happens.
- **Row cap.** Payloads over `MAX_TABLE_ROWS` (2000) rows are refused.
- **Internal key required** on every endpoint.

## Deprecated: `POST /pdf/generate`

Writes PDFs to `PDF_STORAGE_DIR` and returns a URL. This conflicts with
documents being transient — the files accumulate forever and each needs a public
URL to be useful. Still mounted only so Django's existing PDF job keeps working;
it is removed once Django is switched to `/documents/render`.

## Running and testing

```bash
cd fastapi-service
../venv/Scripts/python.exe -m uvicorn main:app --port 8001

# tests
../venv/Scripts/python.exe -m pytest tests -q
```

Render a bill image by hand:

```bash
curl -X POST http://localhost:8001/documents/render \
  -H "X-Internal-Key: $FASTAPI_INTERNAL_KEY" \
  -H "Content-Type: application/json" \
  -d '{"doc_type":"invoice","format":"image","business_id":1,"payload":{
        "business_name":"Hamza Traders","currency_code":"PKR","invoice_no":42,
        "customer_name":"Ali Raza","customer_phone":"923001112222","date":"2026-07-30",
        "line_items":[{"item_name":"Rice 5kg","quantity":"2","rate":"1500.00","amount":"3000.00"}],
        "subtotal":"3000.00","amount_received":"1000.00","balance_after":"2000.00"}}' \
  --output bill.png
```

## Dependencies

`pypdfium2` does the rasterizing. PyMuPDF was the original choice but its
compiled extension fails to load on this project's Python 3.14 (`ImportError:
DLL load failed while importing _extra`) across multiple versions. pypdfium2 is
an equivalent self-contained wheel with bundled PDFium binaries and no system
packages to install — same architecture, working runtime.
