def _pdf_text(value):
    return str(value).encode("utf-16-be").hex().upper()


def build_quote_pdf(quote):
    """Build a dependency-free PDF using a standard Japanese CID font."""
    lines = [
        "Play Design Tennis", "お見積書", f"見積番号: {quote.quote_number}",
        f"お客様名: {quote.customer.display_name()} 様",
        f"見積日: {quote.quote_date:%Y年%m月%d日}", f"有効期限: {quote.valid_until:%Y年%m月%d日}",
        "お見積内容",
    ]
    for item in quote.items.all():
        rate = "―" if item.discount_rate is None else f"{item.discount_rate:g}% OFF"
        lines.extend([
            item.description, f"数量 {item.quantity}  定価 {item.list_price:,}円",
            f"販売価格 {item.sale_price:,}円  値引き {rate}  値引額 {item.discount_amount:,}円",
            f"金額 {item.line_total:,}円",
        ])
    lines.extend([
        f"定価合計 {quote.list_total:,}円", f"お値引き ▲{quote.discount_total:,}円",
        f"お見積合計 {quote.total:,}円", f"備考: {quote.note or '―'}",
    ])
    commands = ["BT", "/F1 11 Tf", "50 790 Td", "16 TL"]
    for index, line in enumerate(lines):
        if index:
            commands.append("T*")
        commands.append(f"<{_pdf_text(line)}> Tj")
    commands.append("ET")
    stream = "\n".join(commands).encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type0 /BaseFont /HeiseiMin-W3 /Encoding /UniJIS-UTF16-H /DescendantFonts [6 0 R] >>",
        b"<< /Type /Font /Subtype /CIDFontType0 /BaseFont /HeiseiMin-W3 /CIDSystemInfo << /Registry (Adobe) /Ordering (Japan1) /Supplement 2 >> >>",
    ]
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
