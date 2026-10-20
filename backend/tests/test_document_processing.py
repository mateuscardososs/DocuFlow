import json
import unittest
from unittest.mock import Mock, patch

from backend.app.schemas import PurchaseOrder
from backend.app.services.document_processing import (
    DocumentProcessingConfig,
    DocumentProcessingService,
)
from backend.app.services.document_text_extractor import DocumentTextExtractor
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
        self.prompts = []

    def extract(self, *, prompt, output_schema):
        self.calls += 1
        self.prompts.append(prompt)
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
        ocr = Mock()
        ocr.recognize.return_value = ""
        processing_config = DocumentProcessingConfig(
            pdf_max_bytes=max_pdf_bytes if max_pdf_bytes is not None else 10 * 1024 * 1024,
        )
        service = DocumentProcessingService(
            provider,
            config=processing_config,
            text_extractor=DocumentTextExtractor(
                ocr_engine=ocr, max_bytes=processing_config.pdf_max_bytes
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

    def test_case_without_document_number_stays_blocked_when_order_reference_is_misassigned(self):
        case_06_text = REFERENCE_TEXT.replace("Documento comercial: DOC-2026-1042\n", "")
        data = reference_payload()
        data["document_number"] = {
            "raw_value": "Pedido de compra: PC-2026-0001",
            "value": "PC-2026-0001",
            "page": 1,
            "evidence": "Pedido de compra: PC-2026-0001",
        }
        service, _ = self.build_service(response=json.dumps(data, ensure_ascii=False))

        result = service.process(
            make_pdf([case_06_text]), PurchaseOrder.model_validate(purchase_order())
        )

        self.assertEqual(result.status, "blocked")
        self.assertEqual(
            result.document.document_number.model_dump(),
            {"raw_value": None, "value": None, "page": None, "evidence": None},
        )
        self.assertTrue(any(
            finding.code == "missing_required_field" and "document_number" in finding.message
            for finding in result.validation.findings
        ))

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

    def test_ocr_page_source_reaches_structured_extraction_and_evidence_validation(self):
        provider = StubProvider(response=json.dumps(reference_payload(), ensure_ascii=False))
        ocr = Mock()
        ocr.recognize.return_value = REFERENCE_TEXT
        service = DocumentProcessingService(
            provider,
            text_extractor=DocumentTextExtractor(ocr_engine=ocr),
        )

        result, pages = service.process_with_pages(
            make_pdf([None]), PurchaseOrder.model_validate(purchase_order())
        )

        self.assertEqual(pages[0].source, "ocr")
        self.assertIn("Página 1 (origem: ocr)", provider.prompts[0])
        self.assertEqual(result.status, "ready_for_review")

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
