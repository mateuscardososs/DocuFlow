import json
import unittest
from unittest.mock import Mock

from fastapi.testclient import TestClient

from backend.app.schemas import PurchaseOrder
from backend.app.services.document_processing import (
    DocumentProcessingConfig,
    DocumentProcessingService,
)
from backend.tests.test_document_processing import reference_payload
from backend.tests.test_pdf_text_extractor import REFERENCE_TEXT, make_pdf


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


def create_client(*, response=None, provider_error=None, max_upload_bytes=10 * 1024 * 1024):
    from backend.app.api.app import create_app

    provider = StubProvider(
        response=json.dumps(reference_payload(), ensure_ascii=False) if response is None else response,
        error=provider_error,
    )
    service = DocumentProcessingService(
        provider,
        config=DocumentProcessingConfig(pdf_max_bytes=max_upload_bytes),
    )
    process = Mock(wraps=service.process)
    service.process = process
    app = create_app(
        document_processing_service=service,
        max_upload_bytes=max_upload_bytes,
    )
    return TestClient(app), provider, process


class ApiTests(unittest.TestCase):
    def test_health_check_returns_ok(self):
        client, _, _ = create_client()

        response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_valid_upload_calls_processing_service_and_returns_typed_result(self):
        client, provider, process = create_client()
        pdf = make_pdf([REFERENCE_TEXT])

        response = client.post(
            "/documents/analyze",
            data={"purchase_order_reference": "PC-2026-0001"},
            files={"file": ("documento.pdf", pdf, "application/pdf")},
        )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "ready_for_review")
        self.assertEqual(body["document"]["document_number"]["value"], "DOC-2026-1042")
        self.assertEqual(body["validation"]["status"], "ready_for_review")
        self.assertEqual(body["pdf_warnings"], [])
        self.assertNotIn("pdf_bytes", body)
        self.assertEqual(provider.calls, 1)
        process.assert_called_once()
        passed_pdf, passed_order = process.call_args.args
        self.assertEqual(passed_pdf, pdf)
        self.assertIsInstance(passed_order, PurchaseOrder)
        self.assertEqual(passed_order.reference, "PC-2026-0001")

    def test_extraction_provider_can_be_injected_without_ollama(self):
        from backend.app.api.app import create_app

        provider = StubProvider(response=json.dumps(reference_payload(), ensure_ascii=False))
        client = TestClient(create_app(extraction_provider=provider))

        response = client.post(
            "/documents/analyze",
            data={"purchase_order_reference": "PC-2026-0001"},
            files={"file": ("documento.pdf", make_pdf([REFERENCE_TEXT]), "application/pdf")},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(provider.calls, 1)

    def test_commercial_divergence_is_returned_without_approval(self):
        data = reference_payload()
        data["items"][0]["quantity"]["value"] = "11"
        client, _, _ = create_client(response=json.dumps(data, ensure_ascii=False))

        response = client.post(
            "/documents/analyze",
            data={"purchase_order_reference": "PC-2026-0001"},
            files={"file": ("documento.pdf", make_pdf([REFERENCE_TEXT]), "application/pdf")},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "divergent")
        self.assertTrue(
            any(
                finding["code"] == "order_quantity_mismatch"
                for finding in response.json()["validation"]["findings"]
            )
        )

    def test_unsupported_media_type_is_rejected_before_processing(self):
        client, provider, process = create_client()

        response = client.post(
            "/documents/analyze",
            data={"purchase_order_reference": "PC-2026-0001"},
            files={"file": ("document.txt", b"texto", "text/plain")},
        )

        self.assertEqual(response.status_code, 415)
        self.assertEqual(provider.calls, 0)
        process.assert_not_called()

    def test_oversized_upload_is_rejected_before_processing(self):
        pdf = make_pdf([REFERENCE_TEXT])
        client, provider, process = create_client(max_upload_bytes=len(pdf) - 1)

        response = client.post(
            "/documents/analyze",
            data={"purchase_order_reference": "PC-2026-0001"},
            files={"file": ("documento.pdf", pdf, "application/pdf")},
        )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(provider.calls, 0)
        process.assert_not_called()

    def test_invalid_pdf_content_returns_clear_client_error(self):
        client, provider, process = create_client()

        response = client.post(
            "/documents/analyze",
            data={"purchase_order_reference": "PC-2026-0001"},
            files={"file": ("documento.pdf", b"nao e um PDF", "application/pdf")},
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("PDF", response.json()["detail"])
        self.assertEqual(provider.calls, 0)
        process.assert_called_once()

    def test_unknown_purchase_order_is_not_processed(self):
        client, provider, process = create_client()

        response = client.post(
            "/documents/analyze",
            data={"purchase_order_reference": "PC-NAO-EXISTE"},
            files={"file": ("documento.pdf", make_pdf([REFERENCE_TEXT]), "application/pdf")},
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(provider.calls, 0)
        process.assert_not_called()

    def test_provider_failure_returns_safe_processing_error(self):
        from backend.app.services.structured_extraction import ExtractionProviderError

        client, _, process = create_client(
            provider_error=ExtractionProviderError("detalhe interno confidencial")
        )

        response = client.post(
            "/documents/analyze",
            data={"purchase_order_reference": "PC-2026-0001"},
            files={"file": ("documento.pdf", make_pdf([REFERENCE_TEXT]), "application/pdf")},
        )

        self.assertEqual(response.status_code, 502)
        self.assertNotIn("detalhe interno confidencial", response.text)
        self.assertIn("provedor de extração", response.json()["detail"])
        process.assert_called_once()

    def test_unexpected_processing_error_does_not_leak_details(self):
        client, _, process = create_client()
        process.side_effect = RuntimeError("stack trace ou segredo")

        response = client.post(
            "/documents/analyze",
            data={"purchase_order_reference": "PC-2026-0001"},
            files={"file": ("documento.pdf", make_pdf([REFERENCE_TEXT]), "application/pdf")},
        )

        self.assertEqual(response.status_code, 500)
        self.assertNotIn("stack trace ou segredo", response.text)


if __name__ == "__main__":
    unittest.main()
