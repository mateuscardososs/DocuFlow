import json
import unittest
from unittest.mock import patch

from backend.app.schemas import PurchaseOrder
from backend.app.services.document_processing import (
    DocumentProcessingConfig,
    DocumentProcessingService,
)
from backend.app.services.pdf_text_extractor import PdfExtractionError
from backend.app.services.structured_extraction import (
    ExtractionProviderError,
    StructuredExtractionError,
)
from backend.app.validation import validate_document
from backend.tests.test_pdf_text_extractor import REFERENCE_TEXT, make_pdf
from backend.tests.test_validation import purchase_order, reference_document


class StubProvider:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = 0

    def extract(self, *, prompt, output_schema):
        self.calls += 1
        if self.error:
            raise self.error
        return self.response


def reference_payload():
    data = reference_document()
    data["supplier_name"]["raw_value"] = "METALURGICA HORIZONTE LTDA."
    data["supplier_name"]["evidence"] = "METALURGICA HORIZONTE LTDA."
    data["issue_date"]["evidence"] = "Data de emissao: 10/10/2026"
    return data


class DocumentProcessingTests(unittest.TestCase):
    def build_service(self, response=None, error=None, max_pdf_bytes=None):
        provider = StubProvider(response=response, error=error)
        service = DocumentProcessingService(
            provider,
            config=DocumentProcessingConfig(
                pdf_max_bytes=max_pdf_bytes if max_pdf_bytes is not None else 10 * 1024 * 1024,
            ),
        )
        return service, provider

    def test_full_reference_flow_returns_review_result_without_pdf_bytes(self):
        pdf = make_pdf([REFERENCE_TEXT])
        order = PurchaseOrder.model_validate(purchase_order())
        service, provider = self.build_service(response=json.dumps(reference_payload(), ensure_ascii=False))

        with patch(
            "backend.app.services.document_processing.validate_document",
            wraps=validate_document,
        ) as commercial_validation:
            result = service.process(pdf, order)

        self.assertEqual(result.status, "ready_for_review")
        self.assertEqual(result.document.document_number.value, "DOC-2026-1042")
        self.assertEqual(result.pdf_warnings, [])
        self.assertIs(commercial_validation.call_args.args[0], result.document)
        self.assertIs(commercial_validation.call_args.args[1], order)
        self.assertEqual(provider.calls, 1)
        self.assertFalse(hasattr(result, "pdf_bytes"))

    def test_commercial_divergence_is_reflected_in_final_status(self):
        data = reference_payload()
        data["items"][0]["quantity"]["value"] = "11"
        service, _ = self.build_service(response=json.dumps(data, ensure_ascii=False))

        result = service.process(make_pdf([REFERENCE_TEXT]), PurchaseOrder.model_validate(purchase_order()))

        self.assertEqual(result.status, "divergent")
        self.assertEqual(result.validation.status, "divergent")
        self.assertTrue(any(f.code == "order_quantity_mismatch" for f in result.validation.findings))

    def test_pdf_warnings_are_preserved_in_application_result(self):
        service, _ = self.build_service(response=json.dumps(reference_payload(), ensure_ascii=False))

        result = service.process(
            make_pdf([REFERENCE_TEXT, None]),
            PurchaseOrder.model_validate(purchase_order()),
        )

        self.assertEqual(result.status, "ready_for_review")
        self.assertEqual(len(result.pdf_warnings), 1)
        self.assertIn("página 2", result.pdf_warnings[0])

    def test_invalid_pdf_error_is_preserved(self):
        service, provider = self.build_service(response=json.dumps(reference_payload()))
        with self.assertRaises(PdfExtractionError) as raised:
            service.process("arquivo inválido".encode(), PurchaseOrder.model_validate(purchase_order()))
        self.assertEqual(raised.exception.code, "invalid_pdf")
        self.assertEqual(provider.calls, 0)

    def test_pdf_size_limit_error_is_preserved(self):
        pdf = make_pdf([REFERENCE_TEXT])
        service, provider = self.build_service(
            response=json.dumps(reference_payload()),
            max_pdf_bytes=len(pdf) - 1,
        )
        with self.assertRaises(PdfExtractionError) as raised:
            service.process(pdf, PurchaseOrder.model_validate(purchase_order()))
        self.assertEqual(raised.exception.code, "file_too_large")
        self.assertEqual(provider.calls, 0)

    def test_provider_error_is_preserved(self):
        service, provider = self.build_service(error=ExtractionProviderError("falha simulada"))
        with self.assertRaises(StructuredExtractionError) as raised:
            service.process(make_pdf([REFERENCE_TEXT]), PurchaseOrder.model_validate(purchase_order()))
        self.assertEqual(raised.exception.code, "provider_error")
        self.assertEqual(provider.calls, 2)

    def test_invalid_evidence_is_preserved(self):
        data = reference_payload()
        data["supplier_name"]["evidence"] = "Trecho que não aparece"
        service, provider = self.build_service(response=json.dumps(data, ensure_ascii=False))
        with self.assertRaises(StructuredExtractionError) as raised:
            service.process(make_pdf([REFERENCE_TEXT]), PurchaseOrder.model_validate(purchase_order()))
        self.assertEqual(raised.exception.code, "evidence_not_found")
        self.assertEqual(provider.calls, 1)


if __name__ == "__main__":
    unittest.main()
