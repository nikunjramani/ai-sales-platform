"""Quotation PDF renderer using ReportLab (BSD licence, pure Python, no system libraries).

- Deterministic: ReportLab's invariant mode removes timestamps and random IDs, so the same
  version always gives byte-identical files and a retried render overwrites nothing new.
- Faithful: amounts are printed exactly as stored in the version (already rounded by the M1
  calculator); nothing is recalculated here.
- Safe with untrusted text: customer references and descriptions are escaped before ReportLab
  reads its mini-markup, so text like "<b>" or "&" is printed, never interpreted.
- Limited fonts: the built-in Helvetica covers Windows-1252 (Western European) text only. Other
  characters raise UnrenderableContent rather than turning into boxes or question marks.
  Embedding a Unicode font is a recorded gap before real customer data.
- Validity: the customer document prints "Valid until <UTC time>" (M6); previews do not.
- Seller (phase 2, v2): logo, legal name, address and tax ID from the business profile, when
  set; otherwise only the business name, exactly as in v1.
- Customer-facing: internal evidence (price list IDs, stock checks, content hash details) stays
  in the portal; the PDF shows prices, totals and a short document reference.
"""

from datetime import UTC
from decimal import Decimal
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Flowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.platypus import Image as PdfImage

from sales_platform.domain.documents import (
    RENDERER_VERSION,
    DocumentSource,
    UnrenderableContent,
)
from sales_platform.domain.documents import printable_text as _text

_PREVIEW_MARK = "PREVIEW - NOT AN OFFER"


def _percent(rate: str) -> str:
    value = (Decimal(rate) * 100).normalize()
    return f"{value:f}%"


class ReportLabQuotationRenderer:
    renderer = RENDERER_VERSION

    def __init__(self) -> None:
        styles = getSampleStyleSheet()
        self._title = styles["Title"]
        self._body = styles["BodyText"]
        self._small = ParagraphStyle("small", parent=self._body, fontSize=8, leading=10)
        self._cell = ParagraphStyle("cell", parent=self._body, fontSize=9, leading=11)
        self._number = ParagraphStyle("number", parent=self._cell, alignment=TA_RIGHT)

    def render(self, source: DocumentSource, *, preview: bool) -> bytes:
        content = source.content
        currency = _text(content["currency"], "currency")
        lines = content["lines"]
        if not isinstance(lines, list) or not lines:
            raise UnrenderableContent("The version has no lines")
        tenant = _text(source.tenant_name, "business name")
        reference = (
            "not given"
            if source.customer_reference is None
            else _text(source.customer_reference, "customer reference")
        )

        def cell(value: object, field: str, style: ParagraphStyle | None = None) -> Paragraph:
            return Paragraph(escape(_text(value, field)), style or self._cell)

        rows: list[list[object]] = [
            [
                cell("#", "header"),
                cell("Item", "header"),
                cell("Quantity", "header", self._number),
                cell(f"Unit price ({currency})", "header", self._number),
                cell(f"Amount ({currency})", "header", self._number),
            ]
        ]
        for position, line in enumerate(lines, start=1):
            assert isinstance(line, dict)
            rows.append(
                [
                    cell(position, "position"),
                    cell(f"{line['sku']} - {line['description']}", "item"),
                    cell(f"{line['quantity']} {line['unit']}", "quantity", self._number),
                    cell(line["unit_price"], "unit price", self._number),
                    cell(line["line_total"], "amount", self._number),
                ]
            )
        policy = content["policy"]
        assert isinstance(policy, dict)
        totals = [
            ("Subtotal", content["subtotal"]),
            (f"Tax ({_percent(str(policy['tax_rate']))})", content["tax"]),
            (f"Total ({currency})", content["total"]),
        ]
        for label, amount in totals:
            bold = Paragraph(f"<b>{escape(_text(amount, 'total'))}</b>", self._number)
            rows.append(["", "", "", cell(label, "label", self._number), bold])

        table = Table(rows, colWidths=[10 * mm, 76 * mm, 26 * mm, 32 * mm, 30 * mm], repeatRows=1)
        table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.black),
                    ("LINEBELOW", (0, len(lines)), (-1, len(lines)), 0.5, colors.grey),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
                ]
            )
        )
        status = (
            "Preview of an unapproved version"
            if preview or source.approved_at is None
            else f"Approved on {source.approved_at.date().isoformat()}"
        )
        validity = (
            None
            if preview or source.valid_until is None
            else "Valid until "
            + source.valid_until.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
            + " UTC"
        )
        story = [
            *self._seller(source, tenant),
            Paragraph(
                f"<b>Quotation {escape(source.number)}</b>, version {source.version}", self._body
            ),
            Paragraph(f"Your reference: {escape(reference)}", self._body),
            Paragraph(escape(status), self._body),
            *([] if validity is None else [Paragraph(f"<b>{escape(validity)}</b>", self._body)]),
            Spacer(1, 6 * mm),
            table,
            Spacer(1, 6 * mm),
            Paragraph(
                f"All amounts are in {escape(currency)}. Availability and delivery dates are "
                "confirmed when the order is placed.",
                self._small,
            ),
            Paragraph(f"Document reference: {escape(source.content_sha256[:16])}", self._small),
        ]
        if preview:
            story.insert(0, Paragraph(f"<b>{_PREVIEW_MARK}</b>", self._body))

        def decorate(canvas: Canvas, document: SimpleDocTemplate) -> None:
            canvas.saveState()
            canvas.setFont("Helvetica", 8)
            canvas.drawRightString(
                A4[0] - 18 * mm,
                10 * mm,
                f"{source.number} v{source.version} - page {canvas.getPageNumber()}",
            )
            if preview:
                canvas.setFont("Helvetica-Bold", 40)
                canvas.setFillColor(colors.Color(0.8, 0.1, 0.1, alpha=0.25))
                canvas.translate(A4[0] / 2, A4[1] / 2)
                canvas.rotate(45)
                canvas.drawCentredString(0, 0, _PREVIEW_MARK)
            canvas.restoreState()

        buffer = BytesIO()
        document = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            leftMargin=18 * mm,
            rightMargin=18 * mm,
            topMargin=18 * mm,
            bottomMargin=18 * mm,
            title=f"Quotation {source.number} v{source.version}",
            author=tenant if source.seller is None else _text(source.seller.legal_name, "name"),
            invariant=1,
        )
        document.build(story, onFirstPage=decorate, onLaterPages=decorate)
        return buffer.getvalue()

    def _seller(self, source: DocumentSource, tenant: str) -> list[Flowable]:
        """The header: logo, legal name, address and tax ID (phase 2), or just the name."""
        seller = source.seller
        if seller is None:
            return [Paragraph(escape(tenant), self._title)]
        header: list[Flowable] = []
        if seller.logo_png is not None:
            logo = PdfImage(BytesIO(seller.logo_png), width=60 * mm, height=24 * mm, kind="bound")
            logo.hAlign = "LEFT"
            header.append(logo)
        header.append(Paragraph(escape(_text(seller.legal_name, "legal name")), self._title))
        for line in seller.address_lines:
            header.append(Paragraph(escape(_text(line, "address")), self._body))
        if seller.tax_id:
            label = _text(seller.tax_id_label or "Tax ID", "tax ID label")
            header.append(
                Paragraph(f"{escape(label)}: {escape(_text(seller.tax_id, 'tax ID'))}", self._body)
            )
        header.append(Spacer(1, 3 * mm))
        return header
