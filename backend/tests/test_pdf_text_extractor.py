import unittest
from io import BytesIO

from pypdf import PdfReader, PdfWriter

from backend.app.services.pdf_text_extractor import PdfExtractionError, extract_pdf_text


def make_pdf(pages):
    """Cria PDF mínimo local; None representa uma página sem texto."""
    escaped_pages = []
    for page in pages:
        if page is None:
            escaped_pages.append(None)
            continue
        lines = page.split("\n")
        commands = ["BT", "/F1 12 Tf", "50 740 Td"]
        for index, line in enumerate(lines):
            safe_line = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            if index:
                commands.append("0 -16 Td")
            commands.append(f"({safe_line}) Tj")
        commands.append("ET")
        escaped_pages.append("\n".join(commands).encode("ascii"))

    objects = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    page_ids = [3 + (index * 2) for index in range(len(pages))]
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    for index, content in enumerate(escaped_pages):
        page_id = page_ids[index]
        content_id = page_id + 1
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {3 + 2 * len(pages)} 0 R >> >> "
            f"/Contents {content_id} 0 R >>".encode()
        )
        stream = content or b""
        objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for object_number, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{object_number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(offsets)}\n".encode())
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode())
    pdf.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode()
    )
    return bytes(pdf)


def encrypted(pdf_bytes):
    reader = PdfReader(BytesIO(pdf_bytes))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt("senha")
    result = BytesIO()
    writer.write(result)
    return result.getvalue()


REFERENCE_TEXT = """METALURGICA HORIZONTE LTDA.
Identificador: FORN-0001
Documento comercial: DOC-2026-1042
Data de emissao: 10/10/2026
Pedido de compra: PC-2026-0001
Moeda: BRL
ARR-001 | Arruela zincada M8 | 10 | R$ 85,50 | R$ 855,00
PAR-002 | Parafuso sextavado M8 | 5 | R$ 79,00 | R$ 395,00
Total do documento: R$ 1.250,00"""


class PdfTextExtractorTests(unittest.TestCase):
    def test_extracts_reference_document_text_on_page_one(self):
        result = extract_pdf_text(make_pdf([REFERENCE_TEXT]))
        self.assertEqual([page.number for page in result.pages], [1])
        self.assertIn("DOC-2026-1042", result.pages[0].text)
        self.assertIn("R$ 1.250,00", result.pages[0].text)
        self.assertEqual(result.warnings, [])

    def test_keeps_text_split_in_page_order(self):
        result = extract_pdf_text(make_pdf(["PRIMEIRA PAGINA", "SEGUNDA PAGINA"]))
        self.assertEqual([page.number for page in result.pages], [1, 2])
        self.assertIn("PRIMEIRA PAGINA", result.pages[0].text)
        self.assertIn("SEGUNDA PAGINA", result.pages[1].text)

    def test_invalid_pdf_has_clear_error(self):
        with self.assertRaises(PdfExtractionError) as raised:
            extract_pdf_text(b"nao e um pdf")
        self.assertEqual(raised.exception.code, "invalid_pdf")
        self.assertIn("PDF", str(raised.exception))

    def test_password_protected_pdf_is_rejected(self):
        with self.assertRaises(PdfExtractionError) as raised:
            extract_pdf_text(encrypted(make_pdf(["CONTEUDO CONFIDENCIAL"])))
        self.assertEqual(raised.exception.code, "password_required")

    def test_more_than_five_pages_is_rejected(self):
        with self.assertRaises(PdfExtractionError) as raised:
            extract_pdf_text(make_pdf(["PAGINA"] * 6))
        self.assertEqual(raised.exception.code, "too_many_pages")

    def test_file_size_limit_is_configurable(self):
        content = make_pdf(["PEQUENO"])
        with self.assertRaises(PdfExtractionError) as raised:
            extract_pdf_text(content, max_bytes=len(content) - 1)
        self.assertEqual(raised.exception.code, "file_too_large")
        self.assertEqual(len(extract_pdf_text(content, max_bytes=len(content)).pages), 1)

    def test_empty_page_is_preserved_and_has_warning(self):
        result = extract_pdf_text(make_pdf(["COM TEXTO", None, "TEXTO FINAL"]))
        self.assertEqual([page.number for page in result.pages], [1, 2, 3])
        self.assertEqual(result.pages[1].text, "")
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("página 2", result.warnings[0])


if __name__ == "__main__":
    unittest.main()
