import json
import unittest
from unittest.mock import patch

from backend.app.integrations.ollama_extraction import OllamaConfig, OllamaExtractionProvider
from backend.app.schemas import PurchaseOrder
from backend.app.services.structured_extraction import (
    ExtractionProviderError,
    ExtractionTimeoutError,
    StructuredExtractionConfig,
    StructuredExtractionError,
    StructuredExtractionService,
)
from backend.app.validation import validate_document
from backend.tests.test_validation import PAGE_TEXT, purchase_order, reference_document


class StubProvider:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = 0
        self.last_prompt = None
        self.last_schema = None

    def extract(self, *, prompt, output_schema):
        self.calls += 1
        self.last_prompt = prompt
        self.last_schema = output_schema
        if self.error:
            raise self.error
        return self.response


class StructuredExtractionTests(unittest.TestCase):
    def test_ollama_config_reads_model_url_and_timeout_from_environment(self):
        with patch.dict(
            "os.environ",
            {
                "DOCUFLOW_OLLAMA_MODEL": "gemma-local",
                "DOCUFLOW_OLLAMA_BASE_URL": "http://ollama:11434",
                "DOCUFLOW_OLLAMA_TIMEOUT_SECONDS": "17.5",
            },
            clear=False,
        ):
            config = OllamaConfig.from_environment()

        self.assertEqual(config.model, "gemma-local")
        self.assertEqual(config.base_url, "http://ollama:11434")
        self.assertEqual(config.timeout_seconds, 17.5)

    def service(self, response=None, error=None, max_attempts=2):
        provider = StubProvider(response=response, error=error)
        service = StructuredExtractionService(
            provider,
            config=StructuredExtractionConfig(max_attempts=max_attempts),
        )
        return service, provider

    def test_valid_response_uses_existing_schema_and_business_validation(self):
        response = json.dumps(reference_document(), ensure_ascii=False)
        service, provider = self.service(response=response)
        document = service.extract(PAGE_TEXT)

        self.assertEqual(document.document_number.value, "DOC-2026-1042")
        self.assertEqual(provider.calls, 1)
        self.assertIn("Página 1", provider.last_prompt)
        self.assertIn("Não invente", provider.last_prompt)
        self.assertEqual(provider.last_schema, type(document).model_json_schema())
        report = validate_document(document, PurchaseOrder.model_validate(purchase_order()), PAGE_TEXT)
        self.assertEqual(report.status, "ready_for_review")

    def test_invalid_json_returns_clear_error_without_retry(self):
        service, provider = self.service(response="{json quebrado")
        with self.assertRaises(StructuredExtractionError) as raised:
            service.extract(PAGE_TEXT)
        self.assertEqual(raised.exception.code, "invalid_json")
        self.assertEqual(provider.calls, 1)

    def test_missing_schema_field_is_rejected(self):
        data = reference_document()
        del data["document_number"]
        service, provider = self.service(response=json.dumps(data))
        with self.assertRaises(StructuredExtractionError) as raised:
            service.extract(PAGE_TEXT)
        self.assertEqual(raised.exception.code, "invalid_schema")
        self.assertIn("document_number", str(raised.exception))
        self.assertEqual(provider.calls, 1)

    def test_missing_business_value_as_null_reaches_business_validation(self):
        data = reference_document()
        data["document_number"] = {
            "raw_value": None,
            "value": None,
            "page": None,
            "evidence": None,
        }
        service, _ = self.service(response=json.dumps(data))
        document = service.extract(PAGE_TEXT)
        report = validate_document(document, PurchaseOrder.model_validate(purchase_order()), PAGE_TEXT)
        self.assertEqual(report.status, "blocked")
        self.assertTrue(any(finding.code == "missing_required_field" for finding in report.findings))

    def test_evidence_page_must_exist(self):
        data = reference_document()
        data["supplier_name"]["page"] = 2
        service, _ = self.service(response=json.dumps(data))
        with self.assertRaises(StructuredExtractionError) as raised:
            service.extract(PAGE_TEXT)
        self.assertEqual(raised.exception.code, "evidence_page_not_found")

    def test_evidence_must_appear_on_indicated_page(self):
        data = reference_document()
        data["supplier_name"]["evidence"] = "Trecho inventado pelo modelo"
        service, _ = self.service(response=json.dumps(data))
        with self.assertRaises(StructuredExtractionError) as raised:
            service.extract(PAGE_TEXT)
        self.assertEqual(raised.exception.code, "evidence_not_found")

    def test_timeout_retries_only_up_to_configured_attempt_limit(self):
        service, provider = self.service(error=ExtractionTimeoutError(), max_attempts=3)
        with self.assertRaises(StructuredExtractionError) as raised:
            service.extract(PAGE_TEXT)
        self.assertEqual(raised.exception.code, "provider_timeout")
        self.assertEqual(provider.calls, 3)

    def test_provider_error_retries_only_up_to_configured_attempt_limit(self):
        service, provider = self.service(error=ExtractionProviderError(), max_attempts=2)
        with self.assertRaises(StructuredExtractionError) as raised:
            service.extract(PAGE_TEXT)
        self.assertEqual(raised.exception.code, "provider_error")
        self.assertEqual(provider.calls, 2)

    def test_ollama_adapter_uses_schema_model_and_explicit_timeout(self):
        class Response:
            def read(self):
                return json.dumps({"message": {"content": "{}"}}).encode()

            def close(self):
                pass

        adapter = OllamaExtractionProvider(
            OllamaConfig(model="modelo-local", base_url="http://localhost:11434", timeout_seconds=4.5)
        )
        schema = {"type": "object"}
        with patch("backend.app.integrations.ollama_extraction.urlopen", return_value=Response()) as mocked:
            self.assertEqual(adapter.extract(prompt="instruções", output_schema=schema), "{}")

        request = mocked.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(body["model"], "modelo-local")
        self.assertEqual(body["format"], schema)
        self.assertFalse(body["stream"])
        self.assertEqual(mocked.call_args.kwargs["timeout"], 4.5)

    def test_ollama_adapter_maps_socket_timeout_to_provider_timeout(self):
        adapter = OllamaExtractionProvider(OllamaConfig(model="modelo-local", timeout_seconds=2.0))
        with patch("backend.app.integrations.ollama_extraction.urlopen", side_effect=TimeoutError):
            with self.assertRaises(ExtractionTimeoutError):
                adapter.extract(prompt="instruções", output_schema={})


if __name__ == "__main__":
    unittest.main()
