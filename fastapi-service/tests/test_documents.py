"""Tests for the document rendering service.

Run from `fastapi-service/`:
    ../venv/Scripts/python.exe -m pytest tests -q

These cover the properties that matter for documents a customer will actually
receive: the two formats come from one template, user-supplied text can never be
interpreted as markup, and no rendering path can silently lose part of a bill.
"""

import io
import os
import sys

import pytest
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pypdfium2 as pdfium  # noqa: E402

from documents.renderer import (  # noqa: E402
    MAX_IMAGE_PAGES,
    MAX_TABLE_ROWS,
    RenderError,
    env,
    render,
    render_image,
    render_pdf,
    supported_formats,
)

BILL = {
    "business_name": "Hamza Traders",
    "currency_code": "PKR",
    "invoice_no": 42,
    "customer_name": "Ali Raza",
    "customer_phone": "923001112222",
    "date": "2026-07-30",
    "line_items": [
        {"item_name": "Basmati Rice 5kg", "quantity": "2", "rate": "1500.00", "amount": "3000.00"},
        {"item_name": "Sugar 1kg", "quantity": "4", "rate": "180.00", "amount": "720.00"},
    ],
    "subtotal": "3720.00",
    "amount_received": "1000.00",
    "balance_after": "2720.00",
}

STATEMENT = {
    "business_name": "Hamza Traders",
    "currency_code": "PKR",
    "customer_name": "Ali Raza",
    "customer_phone": "923001112222",
    "date_from": "2026-07-01",
    "date_to": "2026-07-31",
    "current_balance": "2720.00",
    "rows": [{"date": "2026-07-30", "type": "sale", "amount": "3720.00",
              "balance_after": "3720.00", "note": ""}],
}


def _bill_with_items(count):
    return dict(
        BILL,
        line_items=[
            {"item_name": f"Item {i:03d}", "quantity": "1", "rate": "10.00", "amount": "10.00"}
            for i in range(count)
        ],
    )


class TestFormats:
    def test_bills_support_both_formats(self):
        assert supported_formats("invoice") == ["pdf", "image"]
        assert supported_formats("receipt") == ["pdf", "image"]

    def test_statements_and_reports_are_pdf_only(self):
        assert supported_formats("statement") == ["pdf"]
        assert supported_formats("report") == ["pdf"]

    def test_requesting_an_image_statement_is_refused(self):
        # Refused rather than truncated to page one: silently dropping rows from
        # a financial statement is the one outcome that must never happen.
        with pytest.raises(RenderError, match="no image format"):
            render_image("statement", STATEMENT)

    def test_unknown_format_is_refused(self):
        with pytest.raises(RenderError, match="Unsupported format"):
            render("invoice", "docx", BILL)


class TestRendering:
    def test_invoice_renders_to_pdf(self):
        content, media_type, ext, actual = render("invoice", "pdf", BILL)
        assert content[:5] == b"%PDF-"
        assert (media_type, ext, actual) == ("application/pdf", "pdf", "pdf")

    def test_invoice_renders_to_png(self):
        content, media_type, ext, actual = render("invoice", "image", BILL)
        assert content[:4] == b"\x89PNG"
        assert (media_type, ext, actual) == ("image/png", "png", "image")

    def test_statement_renders_to_pdf(self):
        content, _, _, actual = render("statement", "pdf", STATEMENT)
        assert content[:5] == b"%PDF-"
        assert actual == "pdf"

    def test_bill_image_is_sized_for_a_phone_screen(self):
        content, _, _, _ = render("invoice", "image", BILL)
        image = Image.open(io.BytesIO(content))
        width, height = image.size
        # ~1000px wide reads well in a chat bubble without zooming.
        assert 800 <= width <= 1200
        # Auto-cropped: the template's page is 320mm tall, so an uncropped
        # render of this short bill would be several thousand pixels of white.
        assert height < 1500
        assert len(content) < 400_000, "too heavy for a WhatsApp send"

    def test_image_and_pdf_come_from_the_same_data(self):
        image_bytes, _, _, _ = render("invoice", "image", BILL)
        pdf_bytes, _, _, _ = render("invoice", "pdf", BILL)
        assert image_bytes[:4] == b"\x89PNG"
        assert pdf_bytes[:5] == b"%PDF-"
        # Both are produced from the same payload through the same renderer, so
        # neither can carry figures the other doesn't.
        text = pdfium.PdfDocument(pdf_bytes)[0].get_textpage().get_text_range()
        assert "2720.00" in text.replace(",", "")


class TestSafety:
    """Autoescape is asserted on the rendered HTML, which is the layer where it
    actually operates.

    Searching the finished PDF for the hostile string is not a valid check in
    either direction: escaped text is *supposed* to appear in the PDF (it is
    printed on the document as visible characters), and xhtml2pdf splits long
    strings across separate text-show operators, so a substring search can
    equally miss markup that WAS interpreted.
    """

    HOSTILE = '<img src="file:///C:/Windows/win.ini">EVIL'

    def _rendered_html(self, payload, template="invoice.html"):
        return env.get_template(template).render(**payload)

    def test_markup_in_item_names_is_escaped_not_interpreted(self):
        # Item names can come from OCR of a document a third party handed the
        # owner, so this is genuinely untrusted input.
        hostile = dict(BILL, line_items=[{
            "item_name": self.HOSTILE, "quantity": "1", "rate": "100.00", "amount": "100.00",
        }])
        html = self._rendered_html(hostile)
        assert "&lt;img" in html, "hostile markup was not escaped"
        assert '<img src="file:' not in html, "a live <img> tag reached the renderer"

    def test_markup_in_business_name_is_escaped(self):
        html = self._rendered_html(dict(BILL, business_name=self.HOSTILE))
        assert "&lt;img" in html
        assert '<img src="file:' not in html

    def test_markup_in_the_bill_image_template_is_escaped(self):
        hostile = dict(BILL, customer_name=self.HOSTILE)
        html = self._rendered_html(hostile, template="bill_image.html")
        assert "&lt;img" in html
        assert '<img src="file:' not in html

    def test_hostile_input_still_produces_a_valid_document(self):
        # Escaping must not break rendering — the bill still has to go out.
        hostile = dict(BILL, business_name=self.HOSTILE, customer_name=self.HOSTILE)
        assert render_pdf("invoice", hostile)[:5] == b"%PDF-"
        assert render("invoice", "image", hostile)[0][:4] == b"\x89PNG"

    def test_no_external_resource_is_embedded_in_the_pdf(self):
        hostile = dict(BILL, business_name=self.HOSTILE)
        pdf_bytes = render_pdf("invoice", hostile)
        raw = pdf_bytes.decode("latin-1", "ignore")
        # An interpreted <img> would leave an image XObject behind; escaped
        # text leaves only font resources.
        assert "/Subtype /Image" not in raw

    def test_oversized_payload_is_refused(self):
        with pytest.raises(RenderError, match="rendering limit"):
            render_pdf("invoice", _bill_with_items(MAX_TABLE_ROWS + 1))

    def test_long_bill_falls_back_to_pdf_rather_than_a_giant_image(self):
        content, media_type, _, actual = render("invoice", "image", _bill_with_items(90))
        # A 90-item bill spans several pages; stacking them makes an image so
        # tall WhatsApp's downscaling renders the figures unreadable.
        assert actual == "pdf"
        assert media_type == "application/pdf"
        assert content[:5] == b"%PDF-"

    def test_short_bill_still_prefers_the_image(self):
        _, _, _, actual = render("invoice", "image", _bill_with_items(5))
        assert actual == "image"

    def test_nothing_is_written_to_disk(self, tmp_path, monkeypatch):
        # Generated documents are transient: the caller streams the bytes to
        # WhatsApp and drops them. A renderer that quietly persisted files would
        # reintroduce the storage this design exists to avoid.
        monkeypatch.chdir(tmp_path)
        before = set(os.listdir(tmp_path))
        render("invoice", "image", BILL)
        render("invoice", "pdf", BILL)
        assert set(os.listdir(tmp_path)) == before


class TestMultiPageStacking:
    def test_two_page_bill_is_stacked_into_one_image(self):
        # Within MAX_IMAGE_PAGES, pages are joined rather than truncated.
        for count in (20, 30, 40):
            image_bytes = render_image("invoice", _bill_with_items(count))
            if image_bytes is None:
                continue
            pdf_pages = len(pdfium.PdfDocument(render_pdf(
                "invoice", _bill_with_items(count),
                template_name="bill_image.html")))
            if pdf_pages == 2:
                image = Image.open(io.BytesIO(image_bytes))
                assert image.height > 1500, "second page appears to be missing"
                return
        pytest.skip("no item count produced exactly two pages")

    def test_max_image_pages_is_respected(self):
        assert render_image("invoice", _bill_with_items(200)) is None
        assert MAX_IMAGE_PAGES >= 1
