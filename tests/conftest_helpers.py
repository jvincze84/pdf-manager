"""PDF generators shared by the tests and seed.py (no app import)."""
import pymupdf


def text_pdf(*lines: str) -> bytes:
    """PDF with a real text layer. Base-14 font: Latin-1 only (é, ö, ü, á ok; ő, ű not)."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "\n".join(lines), fontname="helv", fontsize=12)
    data = doc.tobytes()
    doc.close()
    return data


def scan_pdf(*lines: str) -> bytes:
    """Image-only PDF (no text layer) - only findable after OCR."""
    src = pymupdf.open()
    page = src.new_page()
    page.insert_text((72, 100), "\n".join(lines), fontname="helv", fontsize=22)
    pix = page.get_pixmap(dpi=150)
    out = pymupdf.open()
    p = out.new_page()
    p.insert_image(p.rect, stream=pix.tobytes("png"))
    data = out.tobytes(deflate=True)
    src.close()
    out.close()
    return data


def scan_pdf_pages(*pages: str) -> bytes:
    """Multi-page image-only PDF, one text per page."""
    out = pymupdf.open()
    for text in pages:
        one = pymupdf.open(stream=scan_pdf(text), filetype="pdf")
        out.insert_pdf(one)
        one.close()
    data = out.tobytes(deflate=True)
    out.close()
    return data
