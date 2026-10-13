import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.auth import AuthSettings, hash_password
from backend.app.models.base import Base
from backend.app.repositories.documents import SqlAlchemyDocumentRepository
from backend.app.models.identity import User, Workspace, WorkspaceMembership
from backend.app.repositories.purchase_orders import SqlAlchemyPurchaseOrderRepository
from backend.app.seed_reference_order import seed_reference_order
from backend.app.schemas import PurchaseOrder
from backend.app.services.document_processing import (
    DocumentProcessingConfig,
    DocumentProcessingService,
)
from backend.app.services.document_storage import DocumentFileStorage
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


def create_client(test_case, *, response=None, provider_error=None, max_upload_bytes=10 * 1024 * 1024):
    from backend.app.api.app import create_app

    provider = StubProvider(
        response=json.dumps(reference_payload(), ensure_ascii=False) if response is None else response,
        error=provider_error,
    )
    service = DocumentProcessingService(
        provider,
        config=DocumentProcessingConfig(pdf_max_bytes=max_upload_bytes),
    )
    process = Mock(wraps=service.process_with_pages)
    service.process_with_pages = process
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    storage_dir = tempfile.TemporaryDirectory()
    seed_reference_order(session_factory)
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "demo"))
        user = User(email="operator@example.test", password_hash=hash_password("test-password"))
        session.add(user)
        session.flush()
        session.add(
            WorkspaceMembership(user_id=user.id, workspace_id=workspace.id, role="operator")
        )
        workspace_id = workspace.id
    app = create_app(
        document_processing_service=service,
        order_repository=SqlAlchemyPurchaseOrderRepository(session_factory),
        document_repository=SqlAlchemyDocumentRepository(session_factory),
        document_storage=DocumentFileStorage(Path(storage_dir.name)),
        session_factory=session_factory,
        auth_settings=AuthSettings("test-only-secret-key-with-at-least-32-bytes"),
        max_upload_bytes=max_upload_bytes,
    )
    token, _ = app.state.authentication.issue_access_token(user.id)
    client = TestClient(
        app,
        headers={
            "Authorization": f"Bearer {token}",
            "X-Workspace-ID": str(workspace_id),
        },
    )
    test_case.addCleanup(engine.dispose)
    test_case.addCleanup(storage_dir.cleanup)
    test_case.addCleanup(client.close)
    return client, provider, process


class ApiTests(unittest.TestCase):
    def test_health_check_returns_ok(self):
        client, _, _ = create_client(self)

        response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_valid_upload_calls_processing_service_and_returns_typed_result(self):
        client, provider, process = create_client(self)
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
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(engine, expire_on_commit=False)
        storage_dir = tempfile.TemporaryDirectory()
        seed_reference_order(session_factory)
        with session_factory.begin() as session:
            workspace = session.scalar(select(Workspace).where(Workspace.slug == "demo"))
            user = User(email="operator@example.test", password_hash=hash_password("test-password"))
            session.add(user)
            session.flush()
            session.add(
                WorkspaceMembership(user_id=user.id, workspace_id=workspace.id, role="operator")
            )
            workspace_id = workspace.id
        app = create_app(
            extraction_provider=provider,
            order_repository=SqlAlchemyPurchaseOrderRepository(session_factory),
            document_repository=SqlAlchemyDocumentRepository(session_factory),
            document_storage=DocumentFileStorage(Path(storage_dir.name)),
            session_factory=session_factory,
            auth_settings=AuthSettings("test-only-secret-key-with-at-least-32-bytes"),
        )
        token, _ = app.state.authentication.issue_access_token(user.id)
        client = TestClient(
            app,
            headers={
                "Authorization": f"Bearer {token}",
                "X-Workspace-ID": str(workspace_id),
            },
        )
        self.addCleanup(engine.dispose)
        self.addCleanup(storage_dir.cleanup)
        self.addCleanup(client.close)

        response = client.post(
            "/documents/analyze",
            data={"purchase_order_reference": "PC-2026-0001"},
            files={"file": ("documento.pdf", make_pdf([REFERENCE_TEXT]), "application/pdf")},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(provider.calls, 1)

    def test_default_repository_uses_database_url_and_sqlalchemy(self):
        from backend.app.api.app import create_app
        from backend.app.repositories.purchase_orders import SqlAlchemyPurchaseOrderRepository

        with patch.dict(os.environ, {"DATABASE_URL": "sqlite://"}):
            app = create_app(
                extraction_provider=StubProvider(),
                auth_settings=AuthSettings("test-only-secret-key-with-at-least-32-bytes"),
            )

        self.addCleanup(app.state.database_engine.dispose)
        self.assertIsInstance(app.state.order_repository, SqlAlchemyPurchaseOrderRepository)
        self.assertEqual(app.state.database_engine.url.get_backend_name(), "sqlite")
        self.assertEqual(inspect(app.state.database_engine).get_table_names(), [])

    def test_commercial_divergence_is_returned_without_approval(self):
        data = reference_payload()
        data["items"][0]["quantity"]["value"] = "11"
        client, _, _ = create_client(self, response=json.dumps(data, ensure_ascii=False))

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
        client, provider, process = create_client(self)

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
        client, provider, process = create_client(self, max_upload_bytes=len(pdf) - 1)

        response = client.post(
            "/documents/analyze",
            data={"purchase_order_reference": "PC-2026-0001"},
            files={"file": ("documento.pdf", pdf, "application/pdf")},
        )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(provider.calls, 0)
        process.assert_not_called()

    def test_invalid_pdf_content_returns_clear_client_error(self):
        client, provider, process = create_client(self)

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
        client, provider, process = create_client(self)

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
            self,
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
        client, _, process = create_client(self)
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
