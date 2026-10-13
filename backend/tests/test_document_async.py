"""Contratos do fluxo assíncrono sem Redis nem Ollama reais."""

import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.auth import AuthSettings, hash_password
from backend.app.models.base import Base
from backend.app.models.document import DocumentApprovalEntity, DocumentEntity
from backend.app.models.identity import User, Workspace, WorkspaceMembership
from backend.app.models.purchase_order import PurchaseOrderEntity, PurchaseOrderItem, Supplier
from backend.app.repositories.documents import SqlAlchemyDocumentRepository
from backend.app.repositories.purchase_orders import SqlAlchemyPurchaseOrderRepository
from backend.app.services.document_processing import DocumentProcessingConfig, DocumentProcessingService
from backend.app.services.document_queue import DocumentQueueConfig, RQDocumentQueue
from backend.app.services.document_recovery import DocumentRecoveryService
from backend.app.services.document_storage import DocumentFileStorage
from backend.app.services.document_worker import DocumentJobError, DocumentWorkerService
from backend.tests.test_api import StubProvider
from backend.tests.test_document_processing import reference_payload
from backend.tests.test_pdf_text_extractor import REFERENCE_TEXT, make_pdf


class RecordingQueue:
    def __init__(self, session_factory, storage_root):
        self.session_factory = session_factory
        self.storage_root = Path(storage_root)
        self.calls = []
        self.error = None

    def enqueue(self, document_id, *, job_id):
        with self.session_factory() as session:
            record = session.get(DocumentEntity, document_id)
            assert record is not None
            assert record.processing_status == "queued"
            assert (self.storage_root / record.storage_key).is_file()
        self.calls.append((document_id, job_id))
        if self.error:
            raise self.error


class AsyncDocumentApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        with self.sessions.begin() as session:
            workspace = Workspace(slug="demo", name="Demo")
            user = User(email="operator@example.test", password_hash=hash_password("test-password"))
            session.add_all([workspace, user])
            session.flush()
            session.add(WorkspaceMembership(user_id=user.id, workspace_id=workspace.id, role="operator"))
            supplier = Supplier(workspace_id=workspace.id, name="Metalúrgica Horizonte Ltda.", identifier="FORN-0001")
            session.add(supplier)
            session.flush()
            session.add(PurchaseOrderEntity(
                workspace_id=workspace.id, reference="PC-2026-0001", supplier_id=supplier.id,
                currency="BRL", total=1250,
                items=[
                    PurchaseOrderItem(code="ARR-001", description="Arruela", quantity=10, unit_price=85.50, line_total=855),
                    PurchaseOrderItem(code="PAR-002", description="Parafuso", quantity=5, unit_price=79, line_total=395),
                ],
            ))
            self.workspace_id, self.user_id = workspace.id, user.id

        self.storage = DocumentFileStorage(Path(self.tmp.name))
        self.queue = RecordingQueue(self.sessions, self.tmp.name)
        self.provider = StubProvider(response=json.dumps(reference_payload(), ensure_ascii=False))
        self.processing = DocumentProcessingService(self.provider, config=DocumentProcessingConfig())
        from backend.app.api.app import create_app

        self.app = create_app(
            document_processing_service=self.processing,
            order_repository=SqlAlchemyPurchaseOrderRepository(self.sessions),
            document_repository=SqlAlchemyDocumentRepository(self.sessions),
            document_storage=self.storage,
            document_queue=self.queue,
            session_factory=self.sessions,
            auth_settings=AuthSettings("test-only-secret-key-with-at-least-32-bytes"),
        )
        token, _ = self.app.state.authentication.issue_access_token(self.user_id)
        self.client = TestClient(self.app, headers={
            "Authorization": f"Bearer {token}", "X-Workspace-ID": str(self.workspace_id)
        })

    def tearDown(self):
        self.client.close()
        self.engine.dispose()
        self.tmp.cleanup()

    def _upload(self, content=None):
        return self.client.post(
            "/documents/analyze", data={"purchase_order_reference": "PC-2026-0001"},
            files={"file": ("invoice.pdf", content or make_pdf([REFERENCE_TEXT]), "application/pdf")},
        )

    def test_upload_stores_document_before_enqueue_and_returns_202(self):
        response = self._upload()

        self.assertEqual(response.status_code, 202, response.text)
        body = response.json()
        self.assertEqual(body["state"], "queued")
        self.assertEqual(body["processing_status"], "queued")
        self.assertEqual(len(self.queue.calls), 1)
        self.assertEqual(self.queue.calls[0][0], body["document_id"])
        self.assertEqual(self.provider.calls, 0)

    def test_upload_accepts_png_and_jpeg_with_private_internal_extensions(self):
        for image_format, media_type, filename, suffix in (
            ("PNG", "image/png", "scan.png", ".png"),
            ("JPEG", "image/jpeg", "scan.jpg", ".jpg"),
        ):
            with self.subTest(media_type=media_type):
                image = BytesIO()
                Image.new("RGB", (30, 20), "white").save(image, format=image_format)
                response = self.client.post(
                    "/documents/analyze",
                    data={"purchase_order_reference": "PC-2026-0001"},
                    files={"file": (filename, image.getvalue(), media_type)},
                )
                self.assertEqual(response.status_code, 202, response.text)
                with self.sessions() as session:
                    record = session.get(DocumentEntity, response.json()["document_id"])
                    self.assertEqual(record.content_type, media_type)
                    self.assertTrue(record.storage_key.endswith(suffix))
                    self.assertTrue(self.storage.path_for(record.storage_key).is_file())

    def test_enqueue_failure_keeps_document_for_retry(self):
        self.queue.error = RuntimeError("redis password=private")

        response = self._upload()

        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private", response.text)
        record_id = response.json()["document_id"]
        with self.sessions() as session:
            record = session.get(DocumentEntity, record_id)
            self.assertEqual(record.processing_status, "failed")
            self.assertIn("não foi iniciado", record.processing_error)
            self.assertTrue(self.storage.path_for(record.storage_key).is_file())

    def test_reprocess_requeues_failed_document_without_creating_another_record(self):
        self.queue.error = RuntimeError("Redis unavailable")
        failed = self._upload()
        self.queue.error = None
        record_id = failed.json()["document_id"]

        response = self.client.post(f"/documents/{record_id}/reprocess")

        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["state"], "queued")
        self.assertEqual(response.json()["document_id"], record_id)
        with self.sessions() as session:
            self.assertEqual(session.scalar(select(DocumentEntity.id)), record_id)
            record = session.get(DocumentEntity, record_id)
            self.assertEqual(record.processing_attempts, 0)
            self.assertEqual(record.processing_status, "queued")

    def test_reprocess_is_workspace_scoped(self):
        self.queue.error = RuntimeError("Redis unavailable")
        record_id = self._upload().json()["document_id"]
        with self.sessions.begin() as session:
            other = Workspace(slug="other", name="Other")
            session.add(other)
            session.flush()
            session.add(WorkspaceMembership(user_id=self.user_id, workspace_id=other.id, role="operator"))
            other_id = other.id

        response = self.client.post(
            f"/documents/{record_id}/reprocess", headers={"X-Workspace-ID": str(other_id)}
        )

        self.assertEqual(response.status_code, 404)

    def test_reprocess_requires_operator_role(self):
        self.queue.error = RuntimeError("Redis unavailable")
        record_id = self._upload().json()["document_id"]
        with self.sessions.begin() as session:
            approver = User(
                email="approver@example.test", password_hash=hash_password("test-password")
            )
            session.add(approver)
            session.flush()
            session.add(WorkspaceMembership(
                user_id=approver.id, workspace_id=self.workspace_id, role="approver"
            ))
            approver_id = approver.id
        token, _ = self.app.state.authentication.issue_access_token(approver_id)
        approver_client = TestClient(self.app, headers={
            "Authorization": f"Bearer {token}", "X-Workspace-ID": str(self.workspace_id)
        })
        self.addCleanup(approver_client.close)

        response = approver_client.post(f"/documents/{record_id}/reprocess")

        self.assertEqual(response.status_code, 403)

    def test_worker_loads_by_id_validates_and_ignores_duplicate_after_completion(self):
        uploaded = self._upload()
        record_id = uploaded.json()["document_id"]
        with self.sessions() as session:
            record = session.get(DocumentEntity, record_id)
            job_id = record.active_job_id
        worker = DocumentWorkerService(
            self.app.state.document_repository,
            self.app.state.order_repository,
            self.storage,
            self.processing,
            DocumentQueueConfig("redis://unused", max_attempts=3),
        )

        result = worker.process(document_id=record_id, job_id=job_id)
        repeated = worker.process(document_id=record_id, job_id=job_id)

        self.assertEqual(result, "ready_for_review")
        self.assertEqual(repeated, "skipped")
        self.assertEqual(self.provider.calls, 1)
        with self.sessions() as session:
            record = session.get(DocumentEntity, record_id)
            self.assertEqual(record.processing_status, "ready_for_review")
            self.assertEqual(record.processing_attempts, 1)
            self.assertEqual(record.validation_result["status"], "ready_for_review")

    def test_exhausted_attempts_fail_safely(self):
        from backend.app.services.structured_extraction import ExtractionProviderError

        uploaded = self._upload()
        record_id = uploaded.json()["document_id"]
        with self.sessions() as session:
            job_id = session.get(DocumentEntity, record_id).active_job_id
        self.provider.error = ExtractionProviderError("private credentials=hidden")
        worker = DocumentWorkerService(
            self.app.state.document_repository, self.app.state.order_repository,
            self.storage, self.processing, DocumentQueueConfig("redis://unused", max_attempts=2),
        )

        for _ in range(2):
            with self.assertRaises(DocumentJobError):
                worker.process(document_id=record_id, job_id=job_id)
        worker.mark_exhausted(document_id=record_id, job_id=job_id)

        with self.sessions() as session:
            record = session.get(DocumentEntity, record_id)
            self.assertEqual(record.processing_status, "failed")
            self.assertIn("provedor de extração", record.processing_error)
            self.assertNotIn("credentials", record.processing_error)
            self.assertEqual(record.processing_attempts, 2)

    def test_provider_timeout_is_recorded_and_retried_under_attempt_limit(self):
        from backend.app.services.structured_extraction import (
            ExtractionTimeoutError,
        )

        record_id = self._upload().json()["document_id"]
        with self.sessions() as session:
            job_id = session.get(DocumentEntity, record_id).active_job_id
        self.provider.error = ExtractionTimeoutError("provider internal timeout")
        worker = DocumentWorkerService(
            self.app.state.document_repository, self.app.state.order_repository,
            self.storage, self.processing, DocumentQueueConfig("redis://unused", max_attempts=2),
        )

        for _ in range(2):
            with self.assertRaises(DocumentJobError):
                worker.process(document_id=record_id, job_id=job_id)
        worker.mark_exhausted(document_id=record_id, job_id=job_id)

        with self.sessions() as session:
            record = session.get(DocumentEntity, record_id)
            self.assertEqual(record.processing_attempts, 2)
            self.assertEqual(record.processing_status, "failed")
            self.assertIn("Tempo limite", record.processing_error)
            self.assertNotIn("provider internal", record.processing_error)

    def test_recovery_replaces_job_id_so_interrupted_job_cannot_claim_document(self):
        from datetime import datetime, timedelta, timezone

        uploaded = self._upload()
        record_id = uploaded.json()["document_id"]
        with self.sessions.begin() as session:
            record = session.get(DocumentEntity, record_id)
            old_job_id = record.active_job_id
            record.processing_status = "processing"
            record.processing_attempts = 1
            record.processing_started_at = datetime.now(timezone.utc) - timedelta(hours=1)
        config = DocumentQueueConfig("redis://unused", stale_after_seconds=60)
        recovery = DocumentRecoveryService(self.app.state.document_repository, self.queue, config)
        recovered = recovery.recover()
        with self.sessions() as session:
            new_job_id = session.get(DocumentEntity, record_id).active_job_id

        worker = DocumentWorkerService(
            self.app.state.document_repository, self.app.state.order_repository,
            self.storage, self.processing, config,
        )
        old_job = worker.process(document_id=record_id, job_id=old_job_id)
        new_job = worker.process(document_id=record_id, job_id=new_job_id)

        self.assertEqual(recovered["requeued"], 1)
        self.assertNotEqual(old_job_id, new_job_id)
        self.assertEqual(old_job, "skipped")
        self.assertEqual(new_job, "ready_for_review")

    def test_recovery_handles_legacy_processing_without_a_started_timestamp(self):
        record_id = self._upload().json()["document_id"]
        with self.sessions.begin() as session:
            record = session.get(DocumentEntity, record_id)
            record.processing_status = "processing"
            record.processing_started_at = None
        config = DocumentQueueConfig("redis://unused", stale_after_seconds=60)
        recovery = DocumentRecoveryService(self.app.state.document_repository, self.queue, config)

        result = recovery.recover()

        self.assertEqual(result["requeued"], 1)
        with self.sessions() as session:
            self.assertEqual(session.get(DocumentEntity, record_id).processing_status, "queued")

    def test_approved_snapshot_is_unchanged_by_worker_or_reprocess(self):
        uploaded = self._upload()
        document_id = uploaded.json()["document_id"]
        with self.sessions() as session:
            record = session.get(DocumentEntity, document_id)
            job_id = record.active_job_id
        worker = DocumentWorkerService(
            self.app.state.document_repository, self.app.state.order_repository,
            self.storage, self.processing, DocumentQueueConfig("redis://unused"),
        )
        worker.process(document_id=document_id, job_id=job_id)
        with self.sessions.begin() as session:
            membership = session.scalar(select(WorkspaceMembership).where(
                WorkspaceMembership.user_id == self.user_id,
                WorkspaceMembership.workspace_id == self.workspace_id,
            ))
            session.delete(membership)
            session.flush()
            session.add_all([
                WorkspaceMembership(user_id=self.user_id, workspace_id=self.workspace_id, role="operator"),
                WorkspaceMembership(user_id=self.user_id, workspace_id=self.workspace_id, role="approver"),
            ])
        approved = self.client.post(f"/documents/{document_id}/approve", json={"expected_version": 1})
        self.assertEqual(approved.status_code, 200, approved.text)
        with self.sessions.begin() as session:
            snapshot = session.scalar(select(DocumentApprovalEntity).where(
                DocumentApprovalEntity.document_id == document_id
            )).approved_document
            record = session.get(DocumentEntity, document_id)
            record.processing_status = "failed"
            active_job_id = record.active_job_id
        self.assertEqual(worker.process(document_id=document_id, job_id=active_job_id), "skipped")
        rejected = self.client.post(f"/documents/{document_id}/reprocess")
        self.assertEqual(rejected.status_code, 409)
        with self.sessions() as session:
            approval = session.scalar(select(DocumentApprovalEntity).where(
                DocumentApprovalEntity.document_id == document_id
            ))
            self.assertEqual(approval.approved_document, snapshot)


class DocumentWorkerTests(unittest.TestCase):
    def test_worker_requires_the_persisted_job_id_and_ignores_repeated_jobs(self):
        from backend.app.services.document_worker import DocumentWorkerService

        repository = Mock()
        repository.claim_for_processing.return_value = None
        worker = DocumentWorkerService(repository, Mock(), Mock(), Mock())

        result = worker.process(document_id=41, job_id="duplicate-job")

        self.assertEqual(result, "skipped")
        repository.claim_for_processing.assert_called_once_with(
            document_id=41, job_id="duplicate-job", max_attempts=3
        )

    def test_rq_job_contains_only_document_id_and_has_timeout_and_retry_limit(self):
        from backend.app.services import document_queue

        with patch.object(document_queue, "Queue") as queue_type:
            queue = RQDocumentQueue(
                DocumentQueueConfig(
                    "redis://unused", job_timeout_seconds=23, max_attempts=4
                ),
                connection=Mock(),
            )
            queue.enqueue(41, job_id="rq-job-41")

        call = queue_type.return_value.enqueue.call_args
        self.assertEqual(call.args, ("backend.app.worker.process_document_job", 41))
        self.assertEqual(call.kwargs["job_id"], "rq-job-41")
        self.assertEqual(call.kwargs["job_timeout"], 23)
        self.assertEqual(call.kwargs["retry"].max, 3)


if __name__ == "__main__":
    unittest.main()
