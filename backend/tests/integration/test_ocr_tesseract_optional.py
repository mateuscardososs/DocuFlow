import os
import shutil
import unittest

from backend.app.services.document_text_extractor import OcrConfig, TesseractOcrEngine


@unittest.skipUnless(
    os.getenv("DOCUFLOW_RUN_TESSERACT_TESTS") == "1" and shutil.which(os.getenv("DOCUFLOW_TESSERACT_CMD", "tesseract")),
    "configure Tesseract and set DOCUFLOW_RUN_TESSERACT_TESTS=1 to run local OCR smoke test",
)
class TesseractOptionalIntegrationTests(unittest.TestCase):
    def test_real_tesseract_recognizes_png_and_scanned_pdf(self):
        # A focused executable check; OCR quality is not treated as commercial validation.
        from io import BytesIO
        from PIL import Image, ImageDraw, ImageFont

        from backend.app.services.document_text_extractor import DocumentTextExtractor

        canvas = Image.new("RGB", (900, 240), "white")
        ImageDraw.Draw(canvas).text(
            (30, 60), "DOCUFLOW OCR TEST", fill="black", font=ImageFont.load_default(size=72)
        )
        png = BytesIO()
        canvas.save(png, format="PNG")

        image_result = DocumentTextExtractor(
            ocr_engine=TesseractOcrEngine(OcrConfig.from_environment())
        ).extract(png.getvalue(), "image/png")
        self.assertEqual(image_result.pages[0].number, 1)
        self.assertEqual(image_result.pages[0].source, "ocr")
        self.assertIn("DOCUFLOW", image_result.pages[0].text.upper())

        scanned_pdf = BytesIO()
        canvas.save(scanned_pdf, format="PDF", resolution=300)
        pdf_result = DocumentTextExtractor(
            ocr_engine=TesseractOcrEngine(OcrConfig.from_environment())
        ).extract(scanned_pdf.getvalue(), "application/pdf")
        self.assertEqual(len(pdf_result.pages), 1)
        self.assertEqual(pdf_result.pages[0].number, 1)
        self.assertEqual(pdf_result.pages[0].source, "ocr")
        self.assertIn("DOCUFLOW", pdf_result.pages[0].text.upper())


if __name__ == "__main__":
    unittest.main()
