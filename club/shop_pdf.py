"""Customer-facing Shop quote PDF generation.

The PDF uses the PDF standard Japanese CID font and Unicode CMap.  It therefore
does not depend on a font installed on the application host and keeps Japanese
as text (not an image).
"""


PAGE_WIDTH = 595
PAGE_HEIGHT = 842


def _pdf_text(value):
    return str(value).encode("utf-16-be").hex().upper()


def _text(x, y, value, size=9, font="F1"):
    return f"BT /{font} {size} Tf 1 0 0 1 {x} {y} Tm <{_pdf_text(value)}> Tj ET"


def _line(x1, y1, x2, y2, width="0.5"):
    return f"{width} w {x1} {y1} m {x2} {y2} l S"


def _trim(value, length):
    value = str(value).replace("\r", " ").replace("\n", " ")
    return value if len(value) <= length else value[:length - 1] + "…"


def _pdf_objects(stream):
    # A ToUnicode map makes copied/extracted text Unicode as well as selecting
    # the standard Japanese glyphs through UniJIS-UTF16-H for display.
    to_unicode = b"""/CIDInit /ProcSet findresource begin
12 dict begin begincmap
/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def
/CMapName /Adobe-Identity-UCS def /CMapType 2 def
1 begincodespacerange <0000> <FFFF> endcodespacerange
1 beginbfrange <0000> <FFFF> <0000> endbfrange
endcmap CMapName currentdict /CMap defineresource pop end end
"""
    return [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R /F2 7 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type0 /BaseFont /HeiseiKakuGo-W5 /Encoding /UniJIS-UTF16-H /DescendantFonts [6 0 R] /ToUnicode 8 0 R >>",
        b"<< /Type /Font /Subtype /CIDFontType0 /BaseFont /HeiseiKakuGo-W5 /CIDSystemInfo << /Registry (Adobe) /Ordering (Japan1) /Supplement 5 >> /DW 1000 >>",
        b"<< /Type /Font /Subtype /Type0 /BaseFont /HeiseiMin-W3 /Encoding /UniJIS-UTF16-H /DescendantFonts [9 0 R] /ToUnicode 8 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(to_unicode) + to_unicode + b"endstream",
        b"<< /Type /Font /Subtype /CIDFontType0 /BaseFont /HeiseiMin-W3 /CIDSystemInfo << /Registry (Adobe) /Ordering (Japan1) /Supplement 5 >> /DW 1000 >>",
    ]


def build_quote_pdf(quote):
    """Return an A4 portrait, single-page quote with no internal cost data."""
    items = list(quote.items.all())
    row_height = max(24, min(38, 380 // max(len(items), 1)))
    commands = ["0 G 0 g"]
    commands.extend([
        _text(44, 802, "Play Design Tennis", 13),
        _text(238, 770, "お見積書", 22),
        _line(44, 752, 551, 752, "1"),
        _text(52, 727, f"見積番号  {quote.quote_number}", 10),
        _text(330, 727, f"見積日  {quote.quote_date:%Y年%m月%d日}", 10),
        _text(330, 706, f"有効期限  {quote.valid_until:%Y年%m月%d日}", 10),
        _text(52, 692, f"お客様名  {_trim(quote.customer.display_name(), 28)} 様", 11),
        _text(44, 650, "お見積内容", 13),
    ])
    columns = (44, 279, 319, 379, 435, 493, 551)
    table_top = 630
    commands.extend(["0.92 g 44 608 507 22 re f 0 g"])
    headers = ((50, "商品名・内容"), (283, "数量"), (323, "定価"),
               (383, "値引き"), (439, "販売価格"), (497, "明細金額"))
    for x, label in headers:
        commands.append(_text(x, 614, label, 8))
    y = 608
    for item in items:
        bottom = y - row_height
        commands.extend([
            _text(50, bottom + row_height // 2 - 3, _trim(item.description, 25), 8),
            _text(290, bottom + row_height // 2 - 3, item.quantity, 8),
            _text(323, bottom + row_height // 2 - 3, f"{item.list_price:,}円", 8),
            _text(383, bottom + row_height // 2 - 3,
                  "―" if item.discount_rate is None else f"{item.discount_rate:g}% OFF", 8),
            _text(439, bottom + row_height // 2 - 3, f"{item.sale_price:,}円", 8),
            _text(497, bottom + row_height // 2 - 3, f"{item.line_total:,}円", 8),
            _line(44, bottom, 551, bottom),
        ])
        y = bottom
    commands.extend([_line(44, table_top, 551, table_top), _line(44, y, 551, y)])
    for x in columns:
        commands.append(_line(x, table_top, x, y))

    summary_y = min(y - 28, 340)
    commands.extend([
        _text(350, summary_y, "定価合計", 10), _text(468, summary_y, f"{quote.list_total:,}円", 10),
        _text(350, summary_y - 24, "お値引き", 10), _text(468, summary_y - 24, f"▲{quote.discount_total:,}円", 10),
        _line(344, summary_y - 37, 551, summary_y - 37),
        _text(350, summary_y - 62, "お見積合計", 13), _text(460, summary_y - 62, f"{quote.total:,}円", 13),
        _text(44, 156, "備考", 10), _line(44, 148, 551, 148),
        _text(52, 130, _trim(quote.note or "―", 52), 9),
        _text(44, 82, f"本見積の有効期限: {quote.valid_until:%Y年%m月%d日}", 9),
    ])
    stream = "\n".join(commands).encode("ascii")
    objects = _pdf_objects(stream)
    result = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(result)
    result.extend(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode())
    result.extend(f"trailer << /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
    return bytes(result)
