"""Customer-facing Shop quote PDF generation with an embedded Japanese font."""

from io import BytesIO
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (KeepTogether, Paragraph, SimpleDocTemplate,
                               Spacer, Table, TableStyle)


FONT_NAME = "BIZUDGothic"
FONT_PATH = Path(__file__).resolve().parent / "static" / "club" / "fonts" / "BIZ-UDGothicR.ttc"


def _register_font():
    if FONT_NAME not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(FONT_NAME, str(FONT_PATH), subfontIndex=0))


def _paragraph(value, style):
    # Paragraph treats markup specially, so customer-entered text must be escaped.
    from xml.sax.saxutils import escape
    return Paragraph(escape(str(value)).replace("\n", "<br/>"), style)


def build_quote_pdf(quote):
    """Return an A4 portrait quote with an embedded font and no cost data."""
    _register_font()
    items = list(quote.items.all())
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm,
        topMargin=14 * mm, bottomMargin=14 * mm,
        title=f"見積書 {quote.quote_number}", author="Play Design Tennis",
    )
    styles = getSampleStyleSheet()
    base = ParagraphStyle("Japanese", parent=styles["Normal"], fontName=FONT_NAME,
                          fontSize=9, leading=13, textColor=colors.HexColor("#20242A"))
    small = ParagraphStyle("JapaneseSmall", parent=base, fontSize=7.5, leading=10)
    heading = ParagraphStyle("JapaneseHeading", parent=base, fontSize=20, leading=26,
                             alignment=TA_CENTER, spaceAfter=5 * mm)
    section = ParagraphStyle("JapaneseSection", parent=base, fontSize=12, leading=16,
                             spaceBefore=4 * mm, spaceAfter=2 * mm)
    right = ParagraphStyle("JapaneseRight", parent=base, alignment=TA_RIGHT)

    story = [
        _paragraph("Play Design Tennis", ParagraphStyle("Brand", parent=base, fontSize=12)),
        _paragraph("お見積書", heading),
        Table([
            [_paragraph(f"見積番号  {quote.quote_number}", base),
             _paragraph(f"見積日  {quote.quote_date:%Y年%m月%d日}", right)],
            [_paragraph(f"お客様名  {quote.customer.display_name()} 様", base),
             _paragraph(f"有効期限  {quote.valid_until:%Y年%m月%d日}", right)],
        ], colWidths=[90 * mm, 73 * mm], style=TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), FONT_NAME),
            ("LINEBELOW", (0, -1), (-1, -1), 0.6, colors.HexColor("#68737D")),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm),
        ])),
        _paragraph("お見積内容", section),
    ]
    rows = [[_paragraph(label, small) for label in
             ("商品名・内容", "数量", "定価", "値引き", "販売価格", "明細金額")]]
    for item in items:
        rows.append([
            _paragraph(item.description, small), _paragraph(item.quantity, small),
            _paragraph(f"{item.list_price:,}円", small),
            _paragraph("-" if item.discount_rate is None else f"{item.discount_rate:g}% OFF", small),
            _paragraph(f"{item.sale_price:,}円", small), _paragraph(f"{item.line_total:,}円", small),
        ])
    story.append(Table(rows, repeatRows=1,
        colWidths=[72 * mm, 11 * mm, 20 * mm, 20 * mm, 20 * mm, 20 * mm],
        style=TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), FONT_NAME),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E9EDF0")),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#8B949C")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("LEFTPADDING", (0, 0), (-1, -1), 1.5 * mm),
            ("RIGHTPADDING", (0, 0), (-1, -1), 1.5 * mm),
            ("TOPPADDING", (0, 0), (-1, -1), 1.5 * mm),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5 * mm),
        ])))
    summary = Table([
        [_paragraph("定価合計", base), _paragraph(f"{quote.list_total:,}円", right)],
        [_paragraph("お値引き", base), _paragraph(f"▲{quote.discount_total:,}円", right)],
        [_paragraph("お見積合計", ParagraphStyle("TotalLabel", parent=base, fontSize=12)),
         _paragraph(f"{quote.total:,}円", ParagraphStyle("Total", parent=right, fontSize=12))],
    ], colWidths=[38 * mm, 35 * mm], hAlign="RIGHT", style=TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), FONT_NAME),
        ("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.HexColor("#20242A")),
        ("TOPPADDING", (0, 0), (-1, -1), 2 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2 * mm),
    ]))
    story.extend([
        Spacer(1, 4 * mm), summary,
        KeepTogether([_paragraph("備考", section), _paragraph(quote.note or "-", base)]),
        Spacer(1, 5 * mm),
        _paragraph(f"本見積の有効期限: {quote.valid_until:%Y年%m月%d日}", base),
    ])
    document.build(story)
    return buffer.getvalue()
