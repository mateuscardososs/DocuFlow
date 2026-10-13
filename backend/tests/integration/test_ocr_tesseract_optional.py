import os
import shutil
import unittest

from backend.app.services.document_text_extractor import OcrConfig, TesseractOcrEngine


@unittest.skipUnless(
    os.getenv("DOCUFLOW_RUN_TESSERACT_TESTS") == "1" and shutil.which(os.getenv("DOCUFLOW_TESSERACT_CMD", "tesseract")),
    "configure Tesseract and set DOCUFLOW_RUN_TESSERACT_TESTS=1 to run local OCR smoke test",
)
class TesseractOptionalIntegrationTests(unittest.TestCase):
    def test_real_tesseract_recognizes_local_png(self):
        # A focused executable check; OCR quality is not treated as commercial validation.
        from io import BytesIO
        from PIL import Image, ImageDraw, ImageFont

        canvas = Image.new("RGB", (500, 120), "white")
        draw = ImageDraw.Draw(canvas)
        draw.text((15, 30), "DOCUFLOW OCR TEST", fill="black", font=ImageFont.load_default(size=36))
        output = BytesIO()
        canvas.save(output, format="PNG")
        text = TesseractOcrEngine(OcrConfig.from_environment()).recognize(
            output.getvalue(), "image/png"
        )
        self.assertIn("DOCUFLOW", text.upper())


if __name__ == "__main__":
    unittest.main()
