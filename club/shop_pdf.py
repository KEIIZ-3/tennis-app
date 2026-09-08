"""Customer-facing Shop quote PDF generation with an embedded Japanese font."""

from io import BytesIO
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (Image, KeepTogether, Paragraph, SimpleDocTemplate,
                               Spacer, Table, TableStyle)


FONT_NAME = "BIZUDGothic"
FONT_PATH = Path(__file__).resolve().parent / "static" / "club" / "fonts" / "BIZ-UDGothicR.ttc"
SEAL_PATH = Path(__file__).resolve().parent / "static" / "club" / "images" / "play-design-tennis-seal.png"
NAVY = colors.HexColor("#17365D")
BLUE = colors.HexColor("#2F75B5")
PALE_BLUE = colors.HexColor("#EAF2F8")


def _register_font():
    if FONT_NAME not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(FONT_NAME, str(FONT_PATH), subfontIndex=0))


def _paragraph(value, style):
    # Paragraph treats markup specially, so customer-entered text must be escaped.
    from xml.sax.saxutils import escape
    return Paragraph(escape(str(value)).replace("\n", "<br/>"), style)


def _seal_image():
    """Return the unmodified company seal at a restrained size, if installed."""
    if not SEAL_PATH.is_file():
        return None
    seal = Image(str(SEAL_PATH))
    seal._restrictSize(40 * mm, 40 * mm)
    return seal


def _total_paragraph_styles(base, right):
    return (
        ParagraphStyle("TotalLabel", parent=base, fontSize=12, textColor=colors.white),
        ParagraphStyle("Total", parent=right, fontSize=12, textColor=colors.white),
    )


def build_quote_pdf(quote):
    """Return an A4 portrait quote with an embedded font and no cost data."""
    _register_font()
    items = list(quote.items.all())
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=12 * mm, bottomMargin=12 * mm,
        title=f"見積書 {quote.quote_number}", author="Play Design Tennis",
    )
    styles = getSampleStyleSheet()
    base = ParagraphStyle("Japanese", parent=styles["Normal"], fontName=FONT_NAME,
                          fontSize=9, leading=13, textColor=colors.HexColor("#263746"))
    small = ParagraphStyle("JapaneseSmall", parent=base, fontSize=7.2, leading=9.5)
    heading = ParagraphStyle("JapaneseHeading", parent=base, fontSize=24, leading=28,
                             textColor=NAVY, alignment=TA_LEFT)
    english = ParagraphStyle("EnglishHeading", parent=base, fontSize=8, leading=11,
                             textColor=BLUE, alignment=TA_LEFT)
    section = ParagraphStyle("JapaneseSection", parent=base, fontSize=12, leading=16,
                             spaceBefore=4 * mm, spaceAfter=2 * mm)
    right = ParagraphStyle("JapaneseRight", parent=base, alignment=TA_RIGHT)
    total_label, total_amount = _total_paragraph_styles(base, right)

    title_block = [_paragraph("見積書", heading), _paragraph("ESTIMATE", english)]
    metadata = Table([
        [_paragraph("見積番号", small), _paragraph(quote.quote_number, right)],
        [_paragraph("見積日", small), _paragraph(f"{quote.quote_date:%Y年%m月%d日}", right)],
        [_paragraph("有効期限", small), _paragraph(f"{quote.valid_until:%Y年%m月%d日}", right)],
    ], colWidths=[22 * mm, 45 * mm], style=TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), FONT_NAME),
        ("TEXTCOLOR", (0, 0), (0, -1), BLUE),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5 * mm),
    ]))
    issuer_contents = [_paragraph("発行者", small),
                       _paragraph("Play Design Tennis", ParagraphStyle(
                           "Brand", parent=base, fontSize=12, textColor=NAVY))]
    seal = _seal_image()
    issuer = Table([[issuer_contents, seal or ""]], colWidths=[52 * mm, 40 * mm],
                   style=TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                     ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                     ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story = [
        Table([[title_block, metadata]], colWidths=[108 * mm, 72 * mm], style=TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, -1), 1.5, NAVY),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm),
        ])),
        Spacer(1, 4 * mm),
        Table([[
            [_paragraph("お客様名", small),
             _paragraph(f"{quote.purchaser_name} 様", ParagraphStyle(
                 "Customer", parent=base, fontSize=14, leading=19, textColor=NAVY))],
            issuer,
        ]], colWidths=[88 * mm, 92 * mm], style=TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (0, 0), 0.7, BLUE),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2 * mm),
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
        colWidths=[76 * mm, 12 * mm, 23 * mm, 21 * mm, 24 * mm, 24 * mm],
        style=TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), FONT_NAME),
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#AAB9C7")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, PALE_BLUE]),
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
        [_paragraph("お見積合計", total_label), _paragraph(f"{quote.total:,}円", total_amount)],
    ], colWidths=[43 * mm, 39 * mm], hAlign="RIGHT", style=TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), FONT_NAME),
        ("BACKGROUND", (0, -1), (-1, -1), NAVY),
        ("TEXTCOLOR", (0, -1), (-1, -1), colors.white),
        ("LINEABOVE", (0, -1), (-1, -1), 0.8, NAVY),
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
