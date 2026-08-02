"""Document rendering: HTML template -> PDF bytes -> (optionally) image bytes.

Two rules shape this module:

1. **Nothing is written to disk.** Rendered documents are transient artifacts
   that Django streams straight to the WhatsApp Gateway and then drops; the
   database is the permanent business record. Returning bytes rather than a
   file path is what makes that true by construction — there is no file to
   forget to clean up, and no URL that could outlive the send.

2. **One template per document, both formats from it.** The image is produced by
   rasterizing the very PDF that would otherwise be sent, so the two formats
   cannot drift apart. A separate image-drawing code path would be a second
   layout definition to keep in sync by hand.
"""

import io
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pypdfium2 as pdfium
from jinja2 import Environment, FileSystemLoader, select_autoescape
from PIL import Image
from xhtml2pdf import pisa

from ._icon_assets import ICONS

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = BASE_DIR / "templates"

# autoescape is essential, not cosmetic: business names, customer names and item
# names are user- or OCR-supplied, and xhtml2pdf interprets markup and resource
# references while building the PDF. Unescaped, an item name containing HTML
# (say an <img src="file:///..."> read off a photographed bill) would be acted
# on rather than printed — in a document that then gets sent to a customer.
def _commas(value):
    """Groups a plain numeric string's integer part with thousands commas.

    Documents receive figures as plain decimal strings (Django owns the
    money math and must be able to round-trip them through `Decimal()`);
    comma grouping is purely presentational, so it lives here rather than
    in the payload.
    """
    try:
        sign, digits = ("-", str(-Decimal(value))) if Decimal(value) < 0 else ("", str(value))
        whole, _, frac = digits.partition(".")
        grouped = f"{int(whole):,}"
        return f"{sign}{grouped}.{frac}" if frac else f"{sign}{grouped}"
    except (InvalidOperation, ValueError, TypeError):
        return value


env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
)
env.filters["commas"] = _commas
for _name, _b64 in ICONS.items():
    env.globals[f"icon_{_name}"] = _b64

# A4 layouts, used for the PDF format.
TEMPLATE_FOR_DOC_TYPE = {
    "invoice": "invoice.html",
    "receipt": "receipt.html",
    "statement": "statement.html",
    "report": "report.html",
}

# Compact, narrow layouts used only for the image format — an A4 page
# rasterized for WhatsApp would be mostly white space around a short bill.
IMAGE_TEMPLATE_FOR_DOC_TYPE = {
    "invoice": "bill_image.html",
    "receipt": "bill_image.html",
}

# Rasterization scale. The image template's page is 100mm (~283pt) wide, so 3.5x
# yields a ~990px-wide PNG: sharp on a phone screen, small enough to send.
IMAGE_SCALE = 3.5

# Padding kept around the auto-cropped content, in rasterized pixels.
IMAGE_PADDING = 28

# A bill spanning more pages than this is delivered as a PDF instead of an
# image. Stacking many pages produces a single enormously tall PNG, and WhatsApp
# downscales large images hard enough to make the figures unreadable — an
# unreadable bill is worse than a PDF the customer has to tap to open.
MAX_IMAGE_PAGES = 2

# Guard against a payload that would render into an enormous document.
MAX_TABLE_ROWS = 2000


class RenderError(Exception):
    """Raised when a document cannot be produced from the given input."""


def supported_formats(doc_type: str) -> list[str]:
    formats = []
    if doc_type in TEMPLATE_FOR_DOC_TYPE:
        formats.append("pdf")
    if doc_type in IMAGE_TEMPLATE_FOR_DOC_TYPE:
        formats.append("image")
    return formats


def _check_payload_size(payload: dict) -> None:
    for key in ("line_items", "rows", "customer_rows"):
        value = payload.get(key)
        if isinstance(value, list) and len(value) > MAX_TABLE_ROWS:
            raise RenderError(
                f"{key} has {len(value)} rows, which exceeds the {MAX_TABLE_ROWS}-row rendering limit."
            )


def render_pdf(doc_type: str, payload: dict, *, template_name: str | None = None) -> bytes:
    """Renders a document to PDF bytes."""
    _check_payload_size(payload)

    name = template_name or TEMPLATE_FOR_DOC_TYPE.get(doc_type)
    if not name or not (TEMPLATE_DIR / name).exists():
        raise RenderError(f"No template available for doc_type={doc_type}.")

    html = env.get_template(name).render(**payload)

    buffer = io.BytesIO()
    result = pisa.CreatePDF(html, dest=buffer)
    if result.err:
        raise RenderError("Failed to render the document to PDF.")

    return buffer.getvalue()


def _autocrop(image: Image.Image) -> Image.Image:
    """Trims the blank page margin left below short content.

    The image template uses a fixed page height (xhtml2pdf has no auto-height
    page), so a three-line bill would otherwise arrive as a tall strip of white.
    Cropping to the ink and re-adding even padding makes the output size follow
    the content.
    """
    greyscale = image.convert("L")
    # getbbox() finds non-zero pixels, so invert first: on a white page the
    # *ink* is what's near zero.
    inverted = Image.eval(greyscale, lambda px: 255 - px)
    bbox = inverted.getbbox()
    if not bbox:
        return image

    left, top, right, bottom = bbox
    width, height = image.size
    return image.crop(
        (
            max(0, left - IMAGE_PADDING),
            max(0, top - IMAGE_PADDING),
            min(width, right + IMAGE_PADDING),
            min(height, bottom + IMAGE_PADDING),
        )
    )


def _stack_pages(pages: list[Image.Image]) -> Image.Image:
    """Joins multiple rendered pages into one tall image.

    Only reachable if a bill overflows its page. Stacking rather than taking
    page one keeps the alternative from being silent truncation — dropping half
    a financial document a customer is about to receive is the one outcome that
    must not happen quietly.
    """
    if len(pages) == 1:
        return pages[0]

    width = max(page.width for page in pages)
    total_height = sum(page.height for page in pages)
    canvas = Image.new("RGB", (width, total_height), "white")
    offset = 0
    for page in pages:
        canvas.paste(page, (0, offset))
        offset += page.height
    return canvas


def render_image(doc_type: str, payload: dict) -> bytes | None:
    """Renders a document to PNG bytes by rasterizing its PDF.

    Returns None when the content is too long to make a usable image, so the
    caller can fall back to PDF.
    """
    template_name = IMAGE_TEMPLATE_FOR_DOC_TYPE.get(doc_type)
    if not template_name:
        raise RenderError(
            f"doc_type={doc_type} has no image format. "
            f"Multi-page documents are sent as PDF so nothing is truncated."
        )

    pdf_bytes = render_pdf(doc_type, payload, template_name=template_name)

    document = pdfium.PdfDocument(pdf_bytes)
    try:
        page_count = len(document)
        if page_count > MAX_IMAGE_PAGES:
            return None
        pages = [
            _autocrop(document[index].render(scale=IMAGE_SCALE).to_pil().convert("RGB"))
            for index in range(page_count)
        ]
    finally:
        document.close()

    if not pages:
        raise RenderError("The rendered document had no pages.")

    output = io.BytesIO()
    # PNG rather than JPEG: these are text and table rules, where JPEG's
    # ringing artefacts around glyph edges are clearly visible at this size.
    #
    # Default compression, not optimize=True: measured on a typical bill, the
    # optimizer costs ~63ms against ~17ms and saves 0.5KB out of 78KB. Paying
    # 4x the encode time for 0.6% is a bad trade on a send path a person is
    # waiting on.
    _stack_pages(pages).save(output, format="PNG")
    return output.getvalue()


def render(doc_type: str, output_format: str, payload: dict) -> tuple[bytes, str, str, str]:
    """Renders a document.

    Returns `(bytes, media_type, file_extension, actual_format)`. `actual_format`
    can differ from the requested one: a bill too long to be readable as an
    image is produced as a PDF instead. The caller is told which it got rather
    than the substitution happening invisibly — the delivery record has to
    reflect what the customer actually received.
    """
    if output_format == "pdf":
        return render_pdf(doc_type, payload), "application/pdf", "pdf", "pdf"

    if output_format == "image":
        image_bytes = render_image(doc_type, payload)
        if image_bytes is not None:
            return image_bytes, "image/png", "png", "image"
        return render_pdf(doc_type, payload), "application/pdf", "pdf", "pdf"

    raise RenderError(f"Unsupported format={output_format}.")
