import unittest
import subprocess
from io import BytesIO
from unittest.mock import Mock, patch

from PIL import Image

from backend.app.services.document_text_extractor import (
    DocumentTextExtractionError,
    DocumentTextExtractor,
    OcrConfig,
)
from backend.app.services.pdf_text_extractor import PageText
from backend.tests.test_pdf_text_extractor import make_pdf


def image_bytes(image_format="PNG"):
    stream = BytesIO()
    Image.new("RGB", (40, 20), "white").save(stream, format=image_format)
    return stream.getvalue()


class DocumentTextExtractorTests(unittest.TestCase):
    def test_pdf_keeps_native_pages_and_ocr_only_runs_below_threshold(self):
        ocr = Mock()
        ocr.recognize.return_value = "OCR DA PAGINA DOIS"
        renderer = Mock(return_value=b"rendered page")
        extractor = DocumentTextExtractor(
            ocr_engine=ocr,
            pdf_renderer=renderer,
            config=OcrConfig(min_native_chars=8),
        )

        result = extractor.extract(make_pdf(["NATIVE OK", None]), "application/pdf")

        self.assertEqual([(p.number, p.source) for p in result.pages], [(1, "native"), (2, "ocr")])
        self.assertEqual(result.pages[0].text, "NATIVE OK")
        self.assertEqual(result.pages[1].text, "OCR DA PAGINA DOIS")
        renderer.assert_called_once_with(make_pdf(["NATIVE OK", None]), 2)
        ocr.recognize.assert_called_once_with(b"rendered page", "image/png")
        self.assertEqual(result.warnings, [])

    def test_pdf_renderer_produces_png_for_blank_page(self):
        ocr = Mock()
        ocr.recognize.return_value = "RENDER OK"
        result = DocumentTextExtractor(ocr_engine=ocr).extract(
            make_pdf([None]), "application/pdf"
        )
        self.assertEqual(result.pages[0].text, "RENDER OK")
        self.assertEqual(result.pages[0].source, "ocr")
        rendered = ocr.recognize.call_args.args[0]
        self.assertTrue(rendered.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_native_threshold_is_configurable_and_preserves_useful_native_text(self):
        ocr = Mock()
        ocr.recognize.return_value = "OCR"
        extractor = DocumentTextExtractor(
            ocr_engine=ocr,
            pdf_renderer=Mock(return_value=b"render"),
            config=OcrConfig(min_native_chars=4),
        )
        result = extractor.extract(make_pdf(["OK", "VALID TEXT"]), "application/pdf")
        self.assertEqual([page.source for page in result.pages], ["ocr", "native"])
        self.assertEqual(ocr.recognize.call_count, 1)

    def test_png_and_jpeg_each_become_one_ocr_page(self):
        ocr = Mock()
        ocr.recognize.return_value = "TEXTO RECONHECIDO"
        extractor = DocumentTextExtractor(ocr_engine=ocr)
        for image_format, media_type in (("PNG", "image/png"), ("JPEG", "image/jpeg")):
            with self.subTest(media_type=media_type):
                result = extractor.extract(image_bytes(image_format), media_type)
                self.assertEqual(result.pages, [PageText(1, "TEXTO RECONHECIDO", "ocr")])
        self.assertEqual(ocr.recognize.call_count, 2)

    def test_invalid_image_has_safe_error(self):
        with self.assertRaises(DocumentTextExtractionError) as raised:
            DocumentTextExtractor(ocr_engine=Mock()).extract(b"not an image", "image/png")
        self.assertEqual(raised.exception.code, "invalid_image")

    def test_ocr_timeout_and_unavailable_engine_are_safe_errors(self):
        from backend.app.services.document_text_extractor import (
            OcrTimeoutError,
            OcrUnavailableError,
            TesseractOcrEngine,
        )

        for engine_error, expected_code in (
            (OcrTimeoutError(), "ocr_timeout"),
            (OcrUnavailableError(), "ocr_unavailable"),
        ):
            with self.subTest(expected_code=expected_code):
                engine = Mock()
                engine.recognize.side_effect = engine_error
                extractor = DocumentTextExtractor(ocr_engine=engine)
                with self.assertRaises(DocumentTextExtractionError) as raised:
                    extractor.extract(image_bytes(), "image/png")
                self.assertEqual(raised.exception.code, expected_code)

        engine = TesseractOcrEngine(OcrConfig(timeout_seconds=0.01))
        with patch("backend.app.services.document_text_extractor.subprocess.run", side_effect=subprocess.TimeoutExpired("tesseract", 0.01)):
            with self.assertRaises(OcrTimeoutError):
                engine.recognize(b"png", "image/png")
        with patch("backend.app.services.document_text_extractor.subprocess.run", side_effect=FileNotFoundError()):
            with self.assertRaises(OcrUnavailableError):
                engine.recognize(b"png", "image/png")

    def test_image_pixel_limit_is_enforced(self):
        extractor = DocumentTextExtractor(
            ocr_engine=Mock(), config=OcrConfig(max_image_pixels=100)
        )
        with self.assertRaises(DocumentTextExtractionError) as raised:
            extractor.extract(image_bytes(), "image/png")
        self.assertEqual(raised.exception.code, "image_too_large")


if __name__ == "__main__":
    unittest.main()
