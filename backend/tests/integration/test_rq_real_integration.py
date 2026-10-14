"""Integração real RQ + Redis + PostgreSQL; isolada da base e fila normais."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from alembic import command
from alembic.config import Config
from redis import Redis
from rq import Queue
from rq.job import Job
from rq.registry import (
    CanceledJobRegistry,
    DeferredJobRegistry,
    FailedJobRegistry,
    FinishedJobRegistry,
    ScheduledJobRegistry,
    StartedJobRegistry,
)
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

from backend.app.api.app import create_app
from backend.app.auth import AuthSettings, hash_password
from backend.app.models.document import DocumentEntity
from backend.app.models.erp_submission import ERPSubmissionEntity
from backend.app.models.identity import User, Workspace, WorkspaceMembership
from backend.app.models.purchase_order import PurchaseOrderEntity, PurchaseOrderItem, Supplier
from backend.app.repositories.documents import SqlAlchemyDocumentRepository
from backend.app.repositories.erp_submissions import SqlAlchemyERPSubmissionRepository
from backend.app.repositories.purchase_orders import SqlAlchemyPurchaseOrderRepository
from backend.app.services.document_processing import DocumentProcessingConfig, DocumentProcessingService
from backend.app.services.document_queue import DocumentQueueConfig
from backend.app.services.document_recovery import DocumentRecoveryService
from backend.app.services.document_storage import DocumentFileStorage
from backend.tests.test_document_processing import StubProvider, reference_payload
from backend.tests.test_pdf_text_extractor import REFERENCE_TEXT, make_pdf


SKIP_REASON = "Ignorado: configure TEST_DATABASE_URL e TEST_REDIS_URL para a integração real."
REAL_SERVICES_CONFIGURED = bool(os.getenv("TEST_DATABASE_URL") and os.getenv("TEST_REDIS_URL"))


def _ocr_test_configured() -> bool:
    if os.getenv("DOCUFLOW_RUN_TESSERACT_TESTS") != "1":
        return False
    command = os.getenv("DOCUFLOW_TESSERACT_CMD", "tesseract")
    executable = shutil.which(command)
    if not executable:
        return False
    try:
        result = subprocess.run(
            [executable, "--list-langs"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    available = set(result.stdout.splitlines())
    required = set(os.getenv("DOCUFLOW_OCR_LANGUAGE", "por+eng").split("+"))
    return result.returncode == 0 and required.issubset(available)


OCR_SKIP_REASON = (
    "Ignorado: configure TEST_DATABASE_URL, TEST_REDIS_URL, "
    "DOCUFLOW_RUN_TESSERACT_TESTS=1 e os idiomas Tesseract configurados."
)


def _safe_test_database_url() -> str:
    value = os.environ["TEST_DATABASE_URL"]
    url = make_url(value)
    if (
        url.get_backend_name() != "postgresql"
        or url.host not in {"localhost", "127.0.0.1", "::1"}
        or url.database != "docuflow_test"
    ):
        raise RuntimeError("TEST_DATABASE_URL precisa apontar ao PostgreSQL local docuflow_test.")
    application_value = os.getenv("DATABASE_URL")
    if application_value:
        app_url = make_url(application_value)
        if (
            app_url.host == url.host
            and (app_url.port or 5432) == (url.port or 5432)
            and app_url.database == url.database
        ):
            raise RuntimeError("A base de teste não pode ser a mesma base DATABASE_URL.")
    return value


def _safe_test_redis_url() -> str:
    value = os.environ["TEST_REDIS_URL"]
    parsed = urlparse(value)
    if parsed.scheme not in {"redis", "rediss"} or parsed.hostname not in {
        "localhost", "127.0.0.1", "::1"
    }:
        raise RuntimeError("TEST_REDIS_URL precisa apontar para um Redis local.")
    return value


class EnqueueAfterPersistence:
    """Usa RQ real e verifica PostgreSQL/arquivo antes de publicar o job."""

    def __init__(self, queue, connection, sessions, storage_root, job_ids, *, worker_function=None):
        self.queue = queue
        self.connection = connection
        self.sessions = sessions
        self.storage_root = Path(storage_root)
        self.job_ids = job_ids
        self.persisted_before_enqueue = []
        self.crash_next = False
        self.worker_function = worker_function

    def enqueue(self, document_id: int, *, job_id: str) -> None:
        with self.sessions() as session:
            record = session.get(DocumentEntity, document_id)
            persisted = (
                record is not None
                and record.processing_status == "queued"
                and (self.storage_root / record.storage_key).is_file()
            )
        self.persisted_before_enqueue.append(persisted)
        if not persisted:
            raise AssertionError("O documento e o arquivo devem persistir antes do enqueue.")
        function_path = self.worker_function or (
            "backend.tests.integration.rq_worker.crash_after_claim"
            if self.crash_next
            else "backend.tests.integration.rq_worker.process_document_job"
        )
        self.crash_next = False
        self.queue.enqueue(
            function_path,
            document_id,
            job_id=job_id,
            job_timeout=30,
        )
        self.job_ids.add(job_id)


class ScopedRecoveryRepository:
    """Mantém o teste de recuperação restrito ao documento criado pelo próprio teste."""

    def __init__(self, repository, document_id: int):
        self.repository = repository
        self.document_id = document_id

    def list_stale_processing(self, *, cutoff):
        return [
            item for item in self.repository.list_stale_processing(cutoff=cutoff)
            if item.id == self.document_id
        ]

    def __getattr__(self, name):
        return getattr(self.repository, name)


class ERPTestQueue:
    """Publica somente o ID da solicitação em uma fila Redis exclusiva."""

    def __init__(self, queue, sessions, job_ids):
        self.queue = queue
        self.sessions = sessions
        self.job_ids = job_ids
        self.persisted_before_enqueue = []

    def enqueue(self, submission_id: int, *, job_id: str) -> None:
        with self.sessions() as session:
            submission = session.get(ERPSubmissionEntity, submission_id)
            persisted = submission is not None and submission.state == "queued"
        self.persisted_before_enqueue.append(persisted)
        if not persisted:
            raise AssertionError("A solicitação deve ser persistida antes do enqueue.")
        self.queue.enqueue(
            "backend.tests.integration.rq_worker.process_erp_submission_job",
            submission_id,
            job_id=job_id,
            job_timeout=30,
        )
        self.job_ids.add(job_id)


class UnusedERPQueue:
    """Impede que os testes toquem a fila ERP configurada para a aplicação normal."""

    def enqueue(self, submission_id: int, *, job_id: str) -> None:
        raise AssertionError("Use a fila ERP exclusiva do teste de integração.")


@unittest.skipUnless(REAL_SERVICES_CONFIGURED, SKIP_REASON)
class RealRQPostgreSQLIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database_url = _safe_test_database_url()
        cls.redis_url = _safe_test_redis_url()
        cls.engine = create_engine(cls.database_url, pool_pre_ping=True)
        with cls.engine.connect() as connection:
            if connection.scalar(select(1)) != 1:
                raise RuntimeError("PostgreSQL de teste não respondeu.")
        config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
        with cls.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        cls.sessions = sessionmaker(cls.engine, expire_on_commit=False)

        cls.redis = Redis.from_url(cls.redis_url)
        cls.redis.ping()
        cls.run_id = uuid4().hex
        cls.queue_name = f"docuflow_test_{cls.run_id}"
        cls.queue = Queue(cls.queue_name, connection=cls.redis)
        cls.created_job_ids = set()
        cls.tmp = tempfile.TemporaryDirectory(prefix="docuflow-rq-test-")
        cls.storage = DocumentFileStorage(cls.tmp.name)

        with cls.sessions.begin() as session:
            workspace = Workspace(slug=f"rq-{cls.run_id}", name="RQ integration test")
            user = User(
                email=f"rq-{cls.run_id}@example.test",
                password_hash=hash_password("test-only-password"),
            )
            session.add_all([workspace, user])
            session.flush()
            session.add(WorkspaceMembership(
                user_id=user.id, workspace_id=workspace.id, role="operator"
            ))
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
                total=1250,
                items=[
                    PurchaseOrderItem(
                        code="ARR-001", description="Arruela", quantity=10,
                        unit_price=85.50, line_total=855,
                    ),
                    PurchaseOrderItem(
                        code="PAR-002", description="Parafuso", quantity=5,
                        unit_price=79, line_total=395,
                    ),
                ],
            )
            session.add(order)
            session.flush()
            cls.workspace_id, cls.user_id, cls.order_reference = (
                workspace.id, user.id, order.reference
            )

        cls.documents = SqlAlchemyDocumentRepository(cls.sessions)
        cls.orders = SqlAlchemyPurchaseOrderRepository(cls.sessions)
        cls.rq_adapter = EnqueueAfterPersistence(
            cls.queue, cls.redis, cls.sessions, cls.tmp.name, cls.created_job_ids
        )
        provider = StubProvider(response=json.dumps(reference_payload(), ensure_ascii=False))
        processing = DocumentProcessingService(
            provider, config=DocumentProcessingConfig()
        )
        cls.app = create_app(
            document_processing_service=processing,
            order_repository=cls.orders,
            document_repository=cls.documents,
            document_storage=cls.storage,
            document_queue=cls.rq_adapter,
            erp_submission_queue=UnusedERPQueue(),
            session_factory=cls.sessions,
            auth_settings=AuthSettings("integration-test-secret-key-at-least-32-bytes"),
        )
        token, _ = cls.app.state.authentication.issue_access_token(cls.user_id)
        cls.client = TestClient(cls.app, headers={
            "Authorization": f"Bearer {token}",
            "X-Workspace-ID": str(cls.workspace_id),
        })

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "client"):
            cls.client.close()
        if hasattr(cls, "queue"):
            cls.queue.empty()
            for job_id in cls.created_job_ids:
                for registry_type in (
                    StartedJobRegistry, FinishedJobRegistry, FailedJobRegistry,
                    DeferredJobRegistry, ScheduledJobRegistry, CanceledJobRegistry,
                ):
                    registry = registry_type(cls.queue_name, connection=cls.redis)
                    cls.redis.zrem(registry.key, job_id)
                try:
                    Job.fetch(job_id, connection=cls.redis).delete(remove_from_queue=True)
                except Exception:
                    pass
            for registry_type in (
                StartedJobRegistry, FinishedJobRegistry, FailedJobRegistry,
                DeferredJobRegistry, ScheduledJobRegistry, CanceledJobRegistry,
            ):
                registry = registry_type(cls.queue_name, connection=cls.redis)
                if cls.redis.zcard(registry.key) == 0:
                    cls.redis.delete(registry.key)
            if cls.queue.count == 0:
                cls.redis.delete(cls.queue.key)
            for document_id in (
                getattr(cls, "uploaded_document_id", None),
                getattr(cls, "recovery_document_id", None),
            ):
                if document_id is not None:
                    cls.redis.delete(f"{cls.queue_name}:observed:{document_id}")
        if hasattr(cls, "tmp"):
            cls.tmp.cleanup()
        if hasattr(cls, "redis"):
            cls.redis.close()
        if hasattr(cls, "engine"):
            cls.engine.dispose()

    def _upload(self, *, suffix: str):
        response = self.client.post(
            "/documents/analyze",
            data={"purchase_order_reference": self.order_reference},
            files={
                "file": (
                    f"invoice-{suffix}.pdf",
                    make_pdf([REFERENCE_TEXT + f" {suffix}"]),
                    "application/pdf",
                )
            },
        )
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def _run_burst(self) -> None:
        self._run_burst_for_queue(self.queue_name)

    def _run_burst_for_queue(self, queue_name: str) -> None:
        environment = os.environ.copy()
        environment.update({
            "TEST_DATABASE_URL": self.database_url,
            "TEST_REDIS_URL": self.redis_url,
            "TEST_RQ_QUEUE_NAME": queue_name,
            "TEST_DOCUMENT_STORAGE_DIR": self.tmp.name,
        })
        result = subprocess.run(
            [sys.executable, "-m", "backend.tests.integration.rq_worker"],
            cwd=Path(__file__).parents[3],
            env=environment,
            capture_output=True,
            text=True,
            timeout=45,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(_ocr_test_configured(), OCR_SKIP_REASON)
    def test_real_rq_ocr_upload_uses_tesseract_and_validates_page_evidence(self):
        from io import BytesIO

        from PIL import Image, ImageDraw, ImageFont

        from backend.app.services.pdf_text_extractor import extract_pdf_text

        canvas = Image.new("RGB", (3000, 900), "white")
        draw = ImageDraw.Draw(canvas)
        font = ImageFont.load_default(size=56)
        for line_number, line in enumerate(REFERENCE_TEXT.splitlines()):
            draw.text((50, 35 + line_number * 90), line, fill="black", font=font)
        scan = BytesIO()
        canvas.save(scan, format="PDF", resolution=300)
        scanned_pdf = scan.getvalue()
        native_pages = extract_pdf_text(scanned_pdf).pages
        self.assertEqual([page.number for page in native_pages], [1])
        self.assertEqual(native_pages[0].text.strip(), "")

        ocr_run_id = uuid4().hex
        ocr_queue_name = f"{self.queue_name}_ocr_{ocr_run_id}"
        ocr_queue = Queue(ocr_queue_name, connection=self.redis)
        ocr_job_ids: set[str] = set()
        ocr_adapter = EnqueueAfterPersistence(
            ocr_queue,
            self.redis,
            self.sessions,
            self.tmp.name,
            ocr_job_ids,
            worker_function="backend.tests.integration.rq_worker.process_ocr_document_job",
        )
        previous_queue = self.app.state.document_lifecycle.document_queue
        self.app.state.document_lifecycle.document_queue = ocr_adapter
        prompt_key: str | None = None
        try:
            response = self.client.post(
                "/documents/analyze",
                data={"purchase_order_reference": self.order_reference},
                files={"file": ("scan.pdf", scanned_pdf, "application/pdf")},
            )
            self.assertEqual(response.status_code, 202, response.text)
            document_id = response.json()["document_id"]
            self.__class__.uploaded_document_id = document_id
            self.assertTrue(ocr_adapter.persisted_before_enqueue[-1])
            record = self.documents.get_by_id(document_id, self.workspace_id)
            self.assertEqual(record.processing_status, "queued")
            self.assertTrue(self.storage.path_for(record.storage_key).is_file())
            queued = Job.fetch(record.active_job_id, connection=self.redis)
            self.assertEqual(queued.args, (document_id,))
            self.assertEqual(
                queued.func_name,
                "backend.tests.integration.rq_worker.process_ocr_document_job",
            )

            prompt_key = f"{ocr_queue_name}:ocr-prompt:{document_id}"
            # The worker subprocess inherits OCR language, DPI, timeout, and executable settings.
            self._run_burst_for_queue(ocr_queue_name)

            completed = self.documents.get_by_id(document_id, self.workspace_id)
            self.assertEqual(completed.processing_status, "ready_for_review")
            self.assertEqual(completed.review_status, "pending")
            self.assertEqual(completed.final_status, "ready_for_review")
            self.assertEqual(completed.processing_attempts, 1)
            self.assertEqual(len(completed.page_texts), 1)
            self.assertEqual(completed.page_texts[0]["number"], 1)
            self.assertEqual(completed.page_texts[0]["source"], "ocr")
            self.assertIn("DOC-2026-1042", completed.page_texts[0]["text"])
            evidence = completed.extracted_document["document_number"]["evidence"]
            self.assertEqual(evidence, "Documento comercial: DOC-2026-1042")
            self.assertIn(evidence, completed.page_texts[0]["text"])
            self.assertEqual(completed.validation_result["findings"], [])

            # The marker is written by the child process from the prompt it actually received.
            marker = self.redis.get(prompt_key)
            self.assertIsNotNone(marker)
            prompt_pages = json.loads(marker)
            self.assertIn("Página 1 (origem: ocr)", prompt_pages)
            self.assertIn("DOC-2026-1042", prompt_pages["Página 1 (origem: ocr)"])
        finally:
            self.app.state.document_lifecycle.document_queue = previous_queue
            self._clean_test_queue(ocr_queue_name, ocr_job_ids)
            if prompt_key:
                self.redis.delete(prompt_key)

    def _clean_test_queue(self, queue_name: str, job_ids: set[str]) -> None:
        queue = Queue(queue_name, connection=self.redis)
        for job_id in job_ids:
            for registry_type in (
                StartedJobRegistry, FinishedJobRegistry, FailedJobRegistry,
                DeferredJobRegistry, ScheduledJobRegistry, CanceledJobRegistry,
            ):
                registry = registry_type(queue_name, connection=self.redis)
                self.redis.zrem(registry.key, job_id)
            try:
                Job.fetch(job_id, connection=self.redis).delete(remove_from_queue=True)
            except Exception:
                pass
        for registry_type in (
            StartedJobRegistry, FinishedJobRegistry, FailedJobRegistry,
            DeferredJobRegistry, ScheduledJobRegistry, CanceledJobRegistry,
        ):
            registry = registry_type(queue_name, connection=self.redis)
            if self.redis.zcard(registry.key) == 0:
                self.redis.delete(registry.key)
        if queue.count == 0:
            self.redis.delete(queue.key)

    def test_real_api_enqueue_burst_worker_duplicate_and_stuck_recovery(self):
        uploaded = self._upload(suffix="normal")
        self.__class__.uploaded_document_id = uploaded["document_id"]
        document_id = uploaded["document_id"]
        self.assertEqual(self.rq_adapter.persisted_before_enqueue, [True])
        active_id = self.documents.get_by_id(document_id, self.workspace_id).active_job_id
        queued = Job.fetch(active_id, connection=self.redis)
        self.assertEqual(queued.args, (document_id,))
        self.assertEqual(
            queued.func_name,
            "backend.tests.integration.rq_worker.process_document_job",
        )

        repeated_id = f"{self.run_id}-repeated"
        self.queue.enqueue(
            "backend.tests.integration.rq_worker.process_document_job",
            document_id,
            job_id=repeated_id,
            job_timeout=30,
        )
        self.created_job_ids.add(repeated_id)
        self._run_burst()

        first_result = self.documents.get_by_id(document_id, self.workspace_id)
        self.assertEqual(first_result.processing_status, "ready_for_review")
        self.assertEqual(first_result.final_status, "ready_for_review")
        self.assertEqual(first_result.processing_attempts, 1)
        self.assertEqual(
            self.redis.get(f"{self.queue_name}:observed:{document_id}"), b"processing"
        )

        self.rq_adapter.crash_next = True
        interrupted = self._upload(suffix="interrupted")
        self.__class__.recovery_document_id = interrupted["document_id"]
        recovery_id = self.documents.get_by_id(
            interrupted["document_id"], self.workspace_id
        ).active_job_id
        self._run_burst()
        stuck = self.documents.get_by_id(interrupted["document_id"], self.workspace_id)
        self.assertEqual(stuck.processing_status, "processing")
        self.assertEqual(stuck.processing_attempts, 1)
        self.assertIn(recovery_id, self.created_job_ids)

        recovery_config = DocumentQueueConfig(
            redis_url=self.redis_url,
            queue_name=self.queue_name,
            max_attempts=3,
            stale_after_seconds=30,
        )
        recovery = DocumentRecoveryService(
            ScopedRecoveryRepository(self.documents, interrupted["document_id"]),
            self.rq_adapter,
            recovery_config,
        )
        outcome = recovery.recover(
            now=stuck.processing_started_at + timedelta(seconds=31)
        )
        self.assertEqual(outcome, {"requeued": 1, "failed": 0, "skipped": 0})
        recovered = self.documents.get_by_id(interrupted["document_id"], self.workspace_id)
        self.assertEqual(recovered.processing_status, "queued")
        new_job_id = recovered.active_job_id
        self.assertNotEqual(new_job_id, recovery_id)
        self._run_burst()
        final = self.documents.get_by_id(interrupted["document_id"], self.workspace_id)
        self.assertEqual(final.processing_status, "ready_for_review")
        self.assertEqual(final.final_status, "ready_for_review")
        self.assertEqual(final.processing_attempts, 1)

    def test_real_rq_erp_submission_uses_approval_snapshot_and_duplicate_job_is_skipped(self):
        uploaded = self._upload(suffix="erp-approved")
        document_id = uploaded["document_id"]
        self.__class__.uploaded_document_id = document_id
        self._run_burst()
        with self.sessions.begin() as session:
            session.add(WorkspaceMembership(
                user_id=self.user_id, workspace_id=self.workspace_id, role="approver"
            ))
        approved = self.client.post(
            f"/documents/{document_id}/approve", json={"expected_version": 1}
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        approval = self.documents.current_approval(document_id, self.workspace_id, 1)
        self.assertIsNotNone(approval)
        approved_snapshot = approval.approved_document

        erp_queue_name = f"{self.queue_name}_erp_{uuid4().hex}"
        erp_queue = Queue(erp_queue_name, connection=self.redis)
        job_ids: set[str] = set()
        adapter = ERPTestQueue(erp_queue, self.sessions, job_ids)
        previous_queue = self.app.state.erp_submission.queue
        self.app.state.erp_submission.queue = adapter
        self.app.state.erp_submission_queue = adapter
        try:
            response = self.client.post(
                f"/documents/{document_id}/erp-submission",
                json={"expected_version": 1},
            )
            self.assertEqual(response.status_code, 202, response.text)
            submission_id = response.json()["id"]
            self.assertEqual(response.json()["state"], "queued")
            self.assertEqual(adapter.persisted_before_enqueue, [True])
            persisted = self.app.state.erp_submission_repository.get_by_id(
                submission_id, self.workspace_id
            )
            queued = Job.fetch(persisted.active_job_id, connection=self.redis)
            self.assertEqual(queued.args, (submission_id,))
            self.assertEqual(
                queued.func_name,
                "backend.tests.integration.rq_worker.process_erp_submission_job",
            )

            self._run_burst_for_queue(erp_queue_name)
            result = self.app.state.erp_submission_repository.get_by_id(
                submission_id, self.workspace_id
            )
            self.assertEqual(result.state, "succeeded")
            self.assertEqual(result.attempts, 1)
            self.assertTrue(result.erp_identifier.startswith("ERP-DEMO-"))
            duplicate_job_id = f"{self.run_id}-duplicate-erp"
            erp_queue.enqueue(
                "backend.tests.integration.rq_worker.process_erp_submission_job",
                submission_id,
                job_id=duplicate_job_id,
                job_timeout=30,
            )
            job_ids.add(duplicate_job_id)
            self._run_burst_for_queue(erp_queue_name)
            repeated = self.app.state.erp_submission_repository.get_by_id(
                submission_id, self.workspace_id
            )
            self.assertEqual(repeated.state, "succeeded")
            self.assertEqual(repeated.attempts, 1)
            self.assertEqual(
                self.documents.current_approval(document_id, self.workspace_id, 1).approved_document,
                approved_snapshot,
            )
            events = self.documents.audit_events(document_id, self.workspace_id)
            self.assertEqual(
                [event.event_type for event in events if event.event_type.startswith("erp_")],
                ["erp_submission", "erp_attempt", "erp_success"],
            )
        finally:
            self.app.state.erp_submission.queue = previous_queue
            self.app.state.erp_submission_queue = previous_queue
            self._clean_test_queue(erp_queue_name, job_ids)


if __name__ == "__main__":
    unittest.main()
