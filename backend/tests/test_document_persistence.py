import json
import stat
import tempfile
import unittest
from io import BytesIO
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from unittest.mock import Mock

from PIL import Image

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.auth import AuthSettings, hash_password
from backend.app.models.base import Base
from backend.app.models.document import DocumentEntity
from backend.app.models.identity import User, Workspace, WorkspaceMembership
from backend.app.models.purchase_order import PurchaseOrderEntity, PurchaseOrderItem, Supplier
from backend.app.repositories.documents import SqlAlchemyDocumentRepository
from backend.app.repositories.purchase_orders import SqlAlchemyPurchaseOrderRepository
from backend.app.services.document_storage import DocumentFileStorage
from backend.app.services.document_processing import (
    DocumentProcessingConfig,
    DocumentProcessingService,
)
from backend.app.services.document_text_extractor import DocumentTextExtractor
from backend.tests.test_api import StubProvider
from backend.tests.test_document_processing import reference_payload
from backend.tests.test_pdf_text_extractor import REFERENCE_TEXT, make_pdf
from backend.tests.inline_queue import InlineDocumentQueue


JWT_SECRET = "test-only-secret-key-with-at-least-32-bytes"


class StubOcr:
    def recognize(self, image: bytes, media_type: str) -> str:
        return REFERENCE_TEXT


class DocumentPersistenceApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False)
        with self.session_factory.begin() as session:
            workspace = Workspace(slug="demo", name="Workspace demo")
            user = User(
                email="operator@example.test",
                password_hash=hash_password("test-password"),
            )
            session.add_all([workspace, user])
            session.flush()
            session.add(
                WorkspaceMembership(user_id=user.id, workspace_id=workspace.id, role="operator")
            )
            supplier = Supplier(
                workspace_id=workspace.id,
                name="Metalúrgica Horizonte Ltda.",
                identifier="FORN-0001",
            )
            session.add(supplier)
            session.flush()
            order = PurchaseOrderEntity(
                workspace_id=workspace.id,
                reference="PC-2026-0001",
                supplier_id=supplier.id,
                currency="BRL",
                total=Decimal("1250.00"),
                items=[
                    PurchaseOrderItem(
                        code="ARR-001",
                        description="Arruela zincada M8",
                        quantity=Decimal("10"),
                        unit_price=Decimal("85.50"),
                        line_total=Decimal("855.00"),
                    ),
                    PurchaseOrderItem(
                        code="PAR-002",
                        description="Parafuso sextavado M8",
                        quantity=Decimal("5"),
                        unit_price=Decimal("79.00"),
                        line_total=Decimal("395.00"),
                    ),
                ],
            )
            session.add(order)
            self.workspace_id = workspace.id
            self.user_id = user.id

        self.provider = StubProvider(response=json.dumps(reference_payload(), ensure_ascii=False))
        processing = DocumentProcessingService(
            self.provider,
            config=DocumentProcessingConfig(),
            text_extractor=DocumentTextExtractor(ocr_engine=StubOcr()),
        )
        self.process = Mock(wraps=processing.process_with_pages)
        processing.process_with_pages = self.process
        from backend.app.api.app import create_app

        queue = InlineDocumentQueue()
        self.app = create_app(
            document_processing_service=processing,
            order_repository=SqlAlchemyPurchaseOrderRepository(self.session_factory),
            document_repository=SqlAlchemyDocumentRepository(self.session_factory),
            document_storage=DocumentFileStorage(Path(self.temp_dir.name)),
            document_queue=queue,
            session_factory=self.session_factory,
            auth_settings=AuthSettings(JWT_SECRET),
        )
        queue.app = self.app
        token, _ = self.app.state.authentication.issue_access_token(self.user_id)
        self.client = TestClient(
            self.app,
            headers={
                "Authorization": f"Bearer {token}",
                "X-Workspace-ID": str(self.workspace_id),
            },
        )

    def tearDown(self):
        self.client.close()
        self.engine.dispose()
        self.temp_dir.cleanup()

    def _upload(self, *, name="invoice.pdf", content=None, reference="PC-2026-0001", workspace_id=None):
        headers = {}
        if workspace_id is not None:
            headers["X-Workspace-ID"] = str(workspace_id)
        return self.client.post(
            "/documents/analyze",
            headers=headers,
            data={"purchase_order_reference": reference},
            files={"file": (name, make_pdf([REFERENCE_TEXT]) if content is None else content, "application/pdf")},
        )

    def _document(self, document_id):
        with self.session_factory() as session:
            return session.get(DocumentEntity, document_id)

    def test_storage_rejects_paths_outside_private_root(self):
        storage = DocumentFileStorage(Path(self.temp_dir.name))

        with self.assertRaises(ValueError):
            storage.path_for("../../outside.pdf")

    def test_successful_upload_persists_file_and_processing_result(self):
        content = make_pdf([REFERENCE_TEXT])

        response = self._upload(name="../../nota original.pdf", content=content)

        self.assertEqual(response.status_code, 202, response.text)
        body = response.json()
        self.assertFalse(body["duplicate"])
        self.assertEqual(body["state"], "queued")
        record = self._document(body["document_id"])
        stored_file = Path(self.temp_dir.name) / record.storage_key
        self.assertTrue(stored_file.is_file())
        self.assertNotEqual(stored_file.name, "nota original.pdf")
        self.assertEqual(stored_file.read_bytes(), content)
        self.assertEqual(stat.S_IMODE(stored_file.stat().st_mode), 0o600)
        self.assertEqual(record.original_filename, "nota original.pdf")
        self.assertEqual(record.size_bytes, len(content))
        self.assertEqual(record.sha256, sha256(content).hexdigest())
        self.assertEqual(record.processing_status, "ready_for_review")
        self.assertEqual(record.review_status, "pending")
        self.assertEqual(record.page_texts[0]["number"], 1)
        self.assertEqual(record.page_texts[0]["source"], "native")
        self.assertIn("METALURGICA HORIZONTE", record.page_texts[0]["text"])
        self.assertEqual(record.extracted_document["document_number"]["value"], "DOC-2026-1042")
        self.assertEqual(record.pdf_warnings, [])
        self.assertEqual(record.validation_result["status"], "ready_for_review")
        self.assertNotIn("storage_key", body)
        self.assertNotIn("pdf_bytes", body)

    def test_identical_upload_is_reused_inside_workspace(self):
        content = make_pdf([REFERENCE_TEXT])
        first = self._upload(content=content)
        second = self._upload(name="renamed.pdf", content=content)

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertTrue(second.json()["duplicate"])
        self.assertEqual(second.json()["document_id"], first.json()["document_id"])
        self.assertEqual(self.provider.calls, 1)
        with self.session_factory() as session:
            count = session.scalar(select(func.count()).select_from(DocumentEntity))
        self.assertEqual(count, 1)

    def test_scanned_image_is_processed_by_worker_and_origin_is_persisted(self):
        image = BytesIO()
        Image.new("RGB", (40, 20), "white").save(image, format="PNG")
        response = self.client.post(
            "/documents/analyze",
            data={"purchase_order_reference": "PC-2026-0001"},
            files={"file": ("scan.png", image.getvalue(), "image/png")},
        )

        self.assertEqual(response.status_code, 202, response.text)
        record = self._document(response.json()["document_id"])
        self.assertEqual(record.processing_status, "ready_for_review")
        self.assertEqual(record.page_texts[0]["source"], "ocr")
        self.assertIn("DOC-2026-1042", record.page_texts[0]["text"])

    def test_same_hash_can_be_registered_in_another_workspace(self):
        with self.session_factory.begin() as session:
            workspace = Workspace(slug="branch", name="Filial")
            session.add(workspace)
            session.flush()
            session.add(
                WorkspaceMembership(
                    user_id=self.user_id,
                    workspace_id=workspace.id,
                    role="operator",
                )
            )
            supplier = Supplier(
                workspace_id=workspace.id,
                name="Metalúrgica Horizonte Ltda.",
                identifier="FORN-0001",
            )
            session.add(supplier)
            session.flush()
            session.add(
                PurchaseOrderEntity(
                    workspace_id=workspace.id,
                    reference="PC-2026-0001",
                    supplier_id=supplier.id,
                    currency="BRL",
                    total=Decimal("1250.00"),
                    items=[
                        PurchaseOrderItem(
                            code="ARR-001", description="Arruela", quantity=10,
                            unit_price=Decimal("85.50"), line_total=Decimal("855.00"),
                        ),
                        PurchaseOrderItem(
                            code="PAR-002", description="Parafuso", quantity=5,
                            unit_price=Decimal("79.00"), line_total=Decimal("395.00"),
                        ),
                    ],
                )
            )
            branch_id = workspace.id

        content = make_pdf([REFERENCE_TEXT])
        first = self._upload(content=content)
        second = self._upload(content=content, workspace_id=branch_id)

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202, second.text)
        self.assertFalse(second.json()["duplicate"])
        self.assertNotEqual(second.json()["document_id"], first.json()["document_id"])

    def test_invalid_pdf_is_retained_as_failed_attempt(self):
        response = self._upload(content=b"not a PDF")

        self.assertEqual(response.status_code, 202)
        self.assertIn("document_id", response.json())
        record = self._document(response.json()["document_id"])
        self.assertEqual(record.processing_status, "failed")
        self.assertIn("PDF", record.processing_error)
        self.assertTrue((Path(self.temp_dir.name) / record.storage_key).is_file())

    def test_provider_failure_is_recorded_without_internal_details(self):
        from backend.app.services.structured_extraction import ExtractionProviderError

        self.provider.error = ExtractionProviderError("private provider traceback")

        response = self._upload()

        self.assertEqual(response.status_code, 202)
        self.assertIn("document_id", response.json())
        self.assertNotIn("private provider traceback", response.text)
        record = self._document(response.json()["document_id"])
        self.assertEqual(record.processing_status, "failed")
        self.assertIn("provedor de extração", record.processing_error)
        self.assertTrue((Path(self.temp_dir.name) / record.storage_key).is_file())

    def test_list_detail_and_download_are_workspace_scoped(self):
        content = make_pdf([REFERENCE_TEXT])
        uploaded = self._upload(content=content)
        document_id = uploaded.json()["document_id"]

        listing = self.client.get("/documents")
        detail = self.client.get(f"/documents/{document_id}")
        download = self.client.get(f"/documents/{document_id}/download")

        self.assertEqual(listing.status_code, 200)
        self.assertEqual([row["id"] for row in listing.json()], [document_id])
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["id"], document_id)
        self.assertNotIn("storage_key", detail.json())
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.content, content)

        with self.session_factory.begin() as session:
            other_workspace = Workspace(slug="other", name="Outro")
            session.add(other_workspace)
            session.flush()
            session.add(
                WorkspaceMembership(
                    user_id=self.user_id,
                    workspace_id=other_workspace.id,
                    role="operator",
                )
            )
            other_workspace_id = other_workspace.id
        denied = self.client.get(
            f"/documents/{document_id}/download",
            headers={"X-Workspace-ID": str(other_workspace_id)},
        )
        self.assertEqual(denied.status_code, 404)
        self.assertNotIn("invoice.pdf", denied.text)
        branch_listing = self.client.get(
            "/documents", headers={"X-Workspace-ID": str(other_workspace_id)}
        )
        branch_detail = self.client.get(
            f"/documents/{document_id}",
            headers={"X-Workspace-ID": str(other_workspace_id)},
        )
        self.assertEqual(branch_listing.status_code, 200)
        self.assertEqual(branch_listing.json(), [])
        self.assertEqual(branch_detail.status_code, 404)

    def test_listing_requires_workspace_membership(self):
        response = self.client.get("/documents", headers={"X-Workspace-ID": "99999"})

        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
