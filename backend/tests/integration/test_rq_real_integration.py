"""Integração real RQ + Redis + PostgreSQL; isolada da base e fila normais."""

import json
import os
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
from backend.app.models.identity import User, Workspace, WorkspaceMembership
from backend.app.models.purchase_order import PurchaseOrderEntity, PurchaseOrderItem, Supplier
from backend.app.repositories.documents import SqlAlchemyDocumentRepository
from backend.app.repositories.purchase_orders import SqlAlchemyPurchaseOrderRepository
from backend.app.services.document_processing import DocumentProcessingConfig, DocumentProcessingService
from backend.app.services.document_queue import DocumentQueueConfig
from backend.app.services.document_recovery import DocumentRecoveryService
from backend.app.services.document_storage import DocumentFileStorage
from backend.tests.test_document_processing import StubProvider, reference_payload
from backend.tests.test_pdf_text_extractor import REFERENCE_TEXT, make_pdf


SKIP_REASON = "Ignorado: configure TEST_DATABASE_URL e TEST_REDIS_URL para a integração real."
REAL_SERVICES_CONFIGURED = bool(os.getenv("TEST_DATABASE_URL") and os.getenv("TEST_REDIS_URL"))


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

    def __init__(self, queue, connection, sessions, storage_root, job_ids):
        self.queue = queue
        self.connection = connection
        self.sessions = sessions
        self.storage_root = Path(storage_root)
        self.job_ids = job_ids
        self.persisted_before_enqueue = []
        self.crash_next = False

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
        function_path = (
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
        environment = os.environ.copy()
        environment.update({
            "TEST_DATABASE_URL": self.database_url,
            "TEST_REDIS_URL": self.redis_url,
            "TEST_RQ_QUEUE_NAME": self.queue_name,
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


if __name__ == "__main__":
    unittest.main()
