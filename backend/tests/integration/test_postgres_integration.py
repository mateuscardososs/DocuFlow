"""Integration checks that run only against the dedicated local PostgreSQL test DB."""

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.models.identity import User, Workspace, WorkspaceMembership
from backend.app.models.commercial_policy import WorkspaceCommercialPolicyEntity
from backend.app.models.document import (
    DocumentApprovalEntity,
    DocumentAuditEventEntity,
    DocumentEntity,
    DocumentReviewRevisionEntity,
)
from backend.app.models.erp_submission import ERPSubmissionEntity
from backend.app.repositories.erp_submissions import SqlAlchemyERPSubmissionRepository
from backend.app.integrations.erp import DemoERPClient
from backend.app.auth import hash_password
from backend.app.schemas import PurchaseOrder, ReferenceDocument
from backend.app.models.purchase_order import (
    PurchaseOrderEntity,
    PurchaseOrderImportAuditEntity,
    PurchaseOrderItem,
    Supplier,
)
from backend.app.repositories.purchase_orders import SqlAlchemyPurchaseOrderRepository
from backend.app.repositories.documents import SqlAlchemyDocumentRepository
from backend.app.repositories.dashboard import SqlAlchemyDashboardRepository
from backend.app.repositories.commercial_policies import (
    PolicyVersionConflict,
    SqlAlchemyCommercialPolicyRepository,
)
from backend.app.seed_reference_order import seed_reference_order
from backend.app.services.document_processing import DocumentProcessingResult
from backend.app.services.document_review import DocumentReviewService
from backend.app.services.erp_submission import ERPSubmissionWorkerService
from backend.app.validation import ValidationReport
from backend.tests.test_document_processing import reference_payload
from backend.tests.test_pdf_text_extractor import REFERENCE_TEXT


def _test_database_url() -> str:
    value = os.getenv("TEST_DATABASE_URL")
    if not value:
        raise RuntimeError("Defina TEST_DATABASE_URL para executar os testes PostgreSQL.")

    url = make_url(value)
    if (
        url.get_backend_name() != "postgresql"
        or url.host not in {"localhost", "127.0.0.1", "::1"}
        or url.database != "docuflow_test"
    ):
        raise RuntimeError(
            "Os testes exigem PostgreSQL local no banco dedicado chamado docuflow_test."
        )

    application_url = os.getenv("DATABASE_URL")
    if application_url:
        application = make_url(application_url)
        same_target = (
            application.host == url.host
            and (application.port or 5432) == (url.port or 5432)
            and application.database == url.database
        )
        if same_target:
            raise RuntimeError(
                "TEST_DATABASE_URL precisa apontar para uma base diferente de DATABASE_URL."
            )
    return value


class PostgreSQLIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(_test_database_url(), pool_pre_ping=True)
        cls.session_factory = sessionmaker(cls.engine, expire_on_commit=False)
        config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
        inspector = inspect(cls.engine)
        cls.legacy_references = []
        cls.legacy_suppliers = []
        cls.legacy_items = []
        cls.pre_document_references = []
        cls.pre_document_suppliers = []
        cls.pre_document_items = []
        if {"purchase_orders", "suppliers", "purchase_order_items"}.issubset(
            set(inspector.get_table_names())
        ):
            has_workspace_scope = "workspace_id" in {
                column["name"] for column in inspector.get_columns("purchase_orders")
            }
            has_documents = "documents" in inspector.get_table_names()
            with cls.engine.connect() as connection:
                if not has_workspace_scope:
                    cls.legacy_references = connection.scalars(
                        text("SELECT reference FROM purchase_orders ORDER BY id")
                    ).all()
                    cls.legacy_suppliers = connection.execute(
                        text("SELECT id, identifier FROM suppliers ORDER BY id")
                    ).all()
                    cls.legacy_items = connection.execute(
                        text("SELECT id, purchase_order_id, code FROM purchase_order_items ORDER BY id")
                    ).all()
                elif not has_documents:
                    cls.pre_document_references = connection.execute(
                        text("SELECT id, reference, workspace_id FROM purchase_orders ORDER BY id")
                    ).all()
                    cls.pre_document_suppliers = connection.execute(
                        text("SELECT id, identifier, workspace_id FROM suppliers ORDER BY id")
                    ).all()
                    cls.pre_document_items = connection.execute(
                        text("SELECT id, purchase_order_id, code FROM purchase_order_items ORDER BY id")
                    ).all()
        with cls.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        with cls.engine.connect() as connection:
            cls.demo_workspace_id = connection.scalar(
                select(Workspace.id).where(Workspace.slug == "demo")
            )
        cls.repository = SqlAlchemyPurchaseOrderRepository(cls.session_factory)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "engine"):
            cls.engine.dispose()

    def test_initial_migration_is_applied_and_constraints_exist(self):
        inspector = inspect(self.engine)
        self.assertTrue(
            {"suppliers", "purchase_orders", "purchase_order_items", "alembic_version"}
            .issubset(set(inspector.get_table_names()))
        )
        with self.engine.connect() as connection:
            revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
        self.assertEqual(revision, "20261020_0010")
        self.assertIn(
            "uq_purchase_orders_workspace_reference",
            {item["name"] for item in inspector.get_unique_constraints("purchase_orders")},
        )
        self.assertTrue(inspector.get_foreign_keys("purchase_orders"))
        self.assertIn("documents", inspector.get_table_names())
        document_columns = {column["name"] for column in inspector.get_columns("documents")}
        self.assertTrue({"processing_attempts", "processing_started_at", "active_job_id"}.issubset(document_columns))
        self.assertTrue(
            {"document_review_revisions", "document_approvals", "document_audit_events"}
            .issubset(set(inspector.get_table_names()))
        )
        self.assertIn("erp_submissions", inspector.get_table_names())
        self.assertIn("purchase_order_import_audits", inspector.get_table_names())
        self.assertIn("workspace_commercial_policy_versions", inspector.get_table_names())
        self.assertIn(
            "uq_workspace_policy_version",
            {item["name"] for item in inspector.get_unique_constraints("workspace_commercial_policy_versions")},
        )
        self.assertIn(
            "uq_erp_submission_workspace_document_version",
            {item["name"] for item in inspector.get_unique_constraints("erp_submissions")},
        )
        self.assertIn(
            "uq_documents_workspace_sha256",
            {item["name"] for item in inspector.get_unique_constraints("documents")},
        )

    def test_commercial_policy_versions_persist_author_currency_values_and_history(self):
        suffix = uuid4().hex
        workspace = Workspace(
            slug=f"policy-history-{suffix}", name="Histórico de política de integração"
        )
        user = User(email=f"policy-{suffix}@example.test", password_hash="not-a-login")
        with Session(self.engine) as session:
            session.add_all([workspace, user])
            session.flush()
            workspace_id, actor_id = workspace.id, user.id
            session.commit()
        repository = SqlAlchemyCommercialPolicyRepository(self.session_factory)
        first = repository.update(
            workspace_id=workspace_id,
            expected_version=0,
            actor_user_id=actor_id,
            currency_tolerances={"BRL": "0.05", "USD": "1.25"},
        )
        second = repository.update(
            workspace_id=workspace_id,
            expected_version=1,
            actor_user_id=actor_id,
            currency_tolerances={"BRL": "0.10"},
        )
        self.assertEqual((first.version, second.version), (1, 2))
        history = repository.history(workspace_id)
        self.assertEqual([row.version for row in history[:2]], [2, 1])
        self.assertEqual(history[1].currency_tolerances, {"BRL": "0.05", "USD": "1.25"})
        self.assertEqual(history[1].created_by_user_id, actor_id)
        self.assertIsNotNone(history[1].created_at)

    def test_concurrent_commercial_policy_updates_use_optimistic_version(self):
        suffix = uuid4().hex
        workspace = Workspace(slug=f"policy-race-{suffix}", name="Concorrência política")
        user = User(email=f"policy-race-{suffix}@example.test", password_hash="not-a-login")
        with Session(self.engine) as session:
            session.add_all([workspace, user])
            session.flush()
            workspace_id, actor_id = workspace.id, user.id
            session.commit()
        repository = SqlAlchemyCommercialPolicyRepository(self.session_factory)

        def update(currency: str):
            try:
                result = repository.update(
                    workspace_id=workspace_id,
                    expected_version=0,
                    actor_user_id=actor_id,
                    currency_tolerances={currency: "0.05"},
                )
                return ("saved", result.version)
            except PolicyVersionConflict:
                return ("conflict", None)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(update, ["BRL", "USD"]))
        self.assertCountEqual([result[0] for result in results], ["saved", "conflict"])
        self.assertEqual(len(repository.history(workspace_id)), 1)
        self.assertEqual(repository.current(workspace_id).version, 1)

    def test_migration_keeps_legacy_orders_in_demo_workspace(self):
        with self.engine.connect() as connection:
            for reference in self.legacy_references:
                workspace_id = connection.scalar(
                    select(PurchaseOrderEntity.workspace_id).where(
                        PurchaseOrderEntity.reference == reference
                    )
                )
                self.assertEqual(workspace_id, self.demo_workspace_id)
            for supplier_id, identifier in self.legacy_suppliers:
                stored = connection.execute(
                    select(Supplier.identifier, Supplier.workspace_id).where(
                        Supplier.id == supplier_id
                    )
                ).one()
                self.assertEqual(stored, (identifier, self.demo_workspace_id))
            for item_id, order_id, code in self.legacy_items:
                stored = connection.execute(
                    select(PurchaseOrderItem.code, PurchaseOrderEntity.workspace_id)
                    .join(PurchaseOrderEntity)
                    .where(PurchaseOrderItem.id == item_id, PurchaseOrderEntity.id == order_id)
                ).one()
                self.assertEqual(stored, (code, self.demo_workspace_id))
            count = connection.scalar(select(text("count(*)")).select_from(PurchaseOrderEntity))
        self.assertGreaterEqual(count, len(self.legacy_references))

    def test_documents_migration_preserves_existing_orders_suppliers_and_items(self):
        with self.engine.connect() as connection:
            for order_id, reference, workspace_id in self.pre_document_references:
                row = connection.execute(
                    select(PurchaseOrderEntity.reference, PurchaseOrderEntity.workspace_id).where(
                        PurchaseOrderEntity.id == order_id
                    )
                ).one()
                self.assertEqual(row, (reference, workspace_id))
            for supplier_id, identifier, workspace_id in self.pre_document_suppliers:
                row = connection.execute(
                    select(Supplier.identifier, Supplier.workspace_id).where(
                        Supplier.id == supplier_id
                    )
                ).one()
                self.assertEqual(row, (identifier, workspace_id))
            for item_id, order_id, code in self.pre_document_items:
                row = connection.execute(
                    select(PurchaseOrderItem.code, PurchaseOrderEntity.id)
                    .join(PurchaseOrderEntity)
                    .where(PurchaseOrderItem.id == item_id)
                ).one()
                self.assertEqual(row, (code, order_id))

    def test_test_url_guard_rejects_application_database_even_with_other_credentials(self):
        with patch.dict(
            os.environ,
            {
                "TEST_DATABASE_URL": "postgresql+psycopg://other:password@localhost/docuflow_test",
                "DATABASE_URL": "postgresql+psycopg://docuflow:password@localhost:5432/docuflow_test",
            },
        ):
            with self.assertRaisesRegex(RuntimeError, "base diferente"):
                _test_database_url()

    def test_creates_queries_and_persists_decimal_values(self):
        suffix = uuid4().hex
        reference = f"IT-{suffix}"
        with Session(self.engine) as session:
            supplier = Supplier(
                workspace_id=self.demo_workspace_id,
                name="Fornecedor de integração",
                identifier=f"FORN-{suffix}",
            )
            order = PurchaseOrderEntity(
                workspace_id=self.demo_workspace_id,
                reference=reference,
                supplier=supplier,
                currency="BRL",
                total=Decimal("12.34"),
                items=[
                    PurchaseOrderItem(
                        code="ITEM-1",
                        description="Peça de teste",
                        quantity=Decimal("1.250"),
                        unit_price=Decimal("9.87"),
                        line_total=Decimal("12.34"),
                    )
                ],
            )
            session.add(order)
            session.commit()
            order_id = order.id

        with Session(self.engine) as session:
            stored = session.get(PurchaseOrderEntity, order_id)
            self.assertEqual(stored.total, Decimal("12.34"))
            self.assertEqual(stored.items[0].quantity, Decimal("1.250"))
            self.assertEqual(stored.items[0].unit_price, Decimal("9.87"))
            self.assertEqual(stored.items[0].line_total, Decimal("12.34"))

        found = self.repository.get_by_reference(reference, self.demo_workspace_id)
        self.assertIsNotNone(found)
        self.assertEqual(found.total, "12.34")
        self.assertEqual(found.items[0].quantity, "1.250")
        self.assertIsNone(
            self.repository.get_by_reference(f"MISSING-{suffix}", self.demo_workspace_id)
        )

    def test_csv_import_persists_decimals_idempotently_and_isolates_workspaces(self):
        seed_reference_order(self.session_factory)
        user = User(
            email=f"csv-operator-{uuid4().hex}@example.test",
            password_hash=hash_password("integration-test-password"),
        )
        with Session(self.engine) as session:
            session.add(user)
            session.flush()
            actor_id = user.id
            session.add(WorkspaceMembership(
                user_id=actor_id,
                workspace_id=self.demo_workspace_id,
                role="operator",
            ))
            session.commit()

        reference = f"PC-PG-{uuid4().hex[:12]}"
        imported_order = PurchaseOrder.model_validate({
            "reference": reference,
            "supplier_name": "Metalúrgica Horizonte Ltda.",
            "supplier_identifier": "FORN-0001",
            "currency": "BRL",
            "total": "12.50",
            "items": [{
                "code": "DEC-01",
                "description": "Item decimal",
                "quantity": "1.250",
                "unit_price": "10.00",
                "line_total": "12.50",
            }],
        })
        created, ignored = self.repository.import_orders(
            [imported_order],
            workspace_id=self.demo_workspace_id,
            actor_user_id=actor_id,
            total_rows=1,
            duplicate_references=set(),
            error_count=0,
        )
        self.assertEqual(created, [reference])
        self.assertEqual(ignored, [])
        repeated, repeated_duplicates = self.repository.import_orders(
            [imported_order],
            workspace_id=self.demo_workspace_id,
            actor_user_id=actor_id,
            total_rows=1,
            duplicate_references={reference},
            error_count=0,
        )
        self.assertEqual(repeated, [])
        self.assertEqual(repeated_duplicates, [reference])
        stored = self.repository.get_by_reference(reference, self.demo_workspace_id)
        self.assertEqual(stored.items[0].quantity, "1.250")
        self.assertEqual(stored.items[0].line_total, "12.50")

        with Session(self.engine) as session:
            audits = session.execute(
                select(
                    PurchaseOrderImportAuditEntity.workspace_id,
                    PurchaseOrderImportAuditEntity.imported_orders,
                    PurchaseOrderImportAuditEntity.ignored_duplicates,
                ).where(
                    PurchaseOrderImportAuditEntity.actor_user_id == actor_id
                )
            ).all()
            foreign_workspace = Workspace(
                slug=f"csv-other-{uuid4().hex}", name="Outro workspace"
            )
            session.add(foreign_workspace)
            session.flush()
            foreign_workspace_id = foreign_workspace.id
            session.commit()
        self.assertEqual(len(audits), 2)
        self.assertEqual(audits[0].workspace_id, self.demo_workspace_id)
        self.assertEqual(audits[0].imported_orders, 1)
        self.assertEqual(audits[1].ignored_duplicates, 1)
        self.assertIsNone(
            self.repository.get_supplier_name_by_identifier(
                "FORN-0001", foreign_workspace_id
            )
        )
        self.assertIsNone(self.repository.get_by_reference(reference, foreign_workspace_id))

    def test_purchase_order_is_not_queryable_from_another_workspace(self):
        suffix = uuid4().hex
        with Session(self.engine) as session:
            workspace = Workspace(slug=f"other-{suffix}", name="Outro workspace")
            session.add(workspace)
            session.flush()
            supplier = Supplier(
                workspace_id=workspace.id,
                name="Fornecedor isolado",
                identifier=f"FORN-OTHER-{suffix}",
            )
            session.add(supplier)
            session.flush()
            session.add(
                PurchaseOrderEntity(
                    workspace_id=workspace.id,
                    reference=f"PC-OTHER-{suffix}",
                    supplier_id=supplier.id,
                    currency="BRL",
                    total=Decimal("1.00"),
                )
            )
            session.commit()
            other_workspace_id = workspace.id

        self.assertIsNotNone(
            self.repository.get_by_reference(f"PC-OTHER-{suffix}", other_workspace_id)
        )
        self.assertIsNone(
            self.repository.get_by_reference(f"PC-OTHER-{suffix}", self.demo_workspace_id)
        )

    def test_foreign_keys_reject_missing_supplier_and_order(self):
        suffix = uuid4().hex
        with Session(self.engine) as session:
            with self.assertRaises(IntegrityError):
                with session.begin_nested():
                    session.add(
                        PurchaseOrderEntity(
                            reference=f"NO-SUPPLIER-{suffix}",
                            workspace_id=self.demo_workspace_id,
                            supplier_id=999_999_999,
                            currency="BRL",
                            total=Decimal("1.00"),
                        )
                    )
                    session.flush()
            session.rollback()

            supplier = Supplier(
                workspace_id=self.demo_workspace_id,
                name="Fornecedor",
                identifier=f"FK-{suffix}",
            )
            session.add(supplier)
            session.flush()
            with self.assertRaises(IntegrityError):
                with session.begin_nested():
                    session.add(
                        PurchaseOrderItem(
                            purchase_order_id=999_999_999,
                            code="ITEM",
                            description="Sem pedido",
                            quantity=Decimal("1"),
                            unit_price=Decimal("1.00"),
                            line_total=Decimal("1.00"),
                        )
                    )
                    session.flush()
            session.rollback()

    def test_order_cannot_use_supplier_from_another_workspace(self):
        suffix = uuid4().hex
        with Session(self.engine) as session:
            other_workspace = Workspace(slug=f"tenant-{suffix}", name="Outro workspace")
            session.add(other_workspace)
            session.flush()
            supplier = Supplier(
                workspace_id=self.demo_workspace_id,
                name="Fornecedor demo",
                identifier=f"TENANT-{suffix}",
            )
            session.add(supplier)
            session.flush()

            with self.assertRaises(IntegrityError):
                with session.begin_nested():
                    session.add(
                        PurchaseOrderEntity(
                            workspace_id=other_workspace.id,
                            reference=f"CROSS-TENANT-{suffix}",
                            supplier_id=supplier.id,
                            currency="BRL",
                            total=Decimal("1.00"),
                        )
                    )
                    session.flush()

    def test_document_repository_persists_and_deduplicates_per_workspace(self):
        seed_reference_order(self.session_factory)
        with Session(self.engine) as session:
            demo_order_id = session.scalar(
                select(PurchaseOrderEntity.id).where(
                    PurchaseOrderEntity.workspace_id == self.demo_workspace_id,
                    PurchaseOrderEntity.reference == "PC-2026-0001",
                )
            )
            suffix = uuid4().hex
            other_workspace = Workspace(slug=f"docs-{suffix}", name="Workspace documentos")
            session.add(other_workspace)
            session.flush()
            other_supplier = Supplier(
                workspace_id=other_workspace.id,
                name="Fornecedor dos documentos",
                identifier=f"FORN-DOC-{suffix}",
            )
            session.add(other_supplier)
            session.flush()
            other_order = PurchaseOrderEntity(
                workspace_id=other_workspace.id,
                reference=f"PC-DOC-{suffix}",
                supplier_id=other_supplier.id,
                currency="BRL",
                total=Decimal("1.00"),
                items=[
                    PurchaseOrderItem(
                        code="DOC-1",
                        description="Item documento",
                        quantity=Decimal("1"),
                        unit_price=Decimal("1.00"),
                        line_total=Decimal("1.00"),
                    )
                ],
            )
            session.add(other_order)
            session.flush()
            other_workspace_id = other_workspace.id
            other_order_id = other_order.id
            session.commit()

        repository = SqlAlchemyDocumentRepository(self.session_factory)
        checksum = uuid4().hex + uuid4().hex
        document, duplicate = repository.create(
            workspace_id=self.demo_workspace_id,
            purchase_order_id=demo_order_id,
            original_filename="nota.pdf",
            size_bytes=123,
            content_type="application/pdf",
            sha256=checksum,
            storage_key=f"{self.demo_workspace_id}/internal.pdf",
        )
        same, duplicate_again = repository.create(
            workspace_id=self.demo_workspace_id,
            purchase_order_id=demo_order_id,
            original_filename="outro-nome.pdf",
            size_bytes=123,
            content_type="application/pdf",
            sha256=checksum,
            storage_key=f"{self.demo_workspace_id}/should-not-exist.pdf",
        )
        separate, duplicate_other_workspace = repository.create(
            workspace_id=other_workspace_id,
            purchase_order_id=other_order_id,
            original_filename="nota.pdf",
            size_bytes=123,
            content_type="application/pdf",
            sha256=checksum,
            storage_key=f"{other_workspace_id}/internal.pdf",
        )

        self.assertFalse(duplicate)
        self.assertTrue(duplicate_again)
        self.assertEqual(same.id, document.id)
        self.assertFalse(duplicate_other_workspace)
        self.assertNotEqual(separate.id, document.id)
        result = DocumentProcessingResult(
            document=ReferenceDocument.model_validate(reference_payload()),
            pdf_warnings=["A página 2 não contém texto utilizável."],
            validation=ValidationReport(status="ready_for_review", findings=[]),
            status="ready_for_review",
        )
        repository.save_result(
            document_id=document.id,
            workspace_id=self.demo_workspace_id,
            result=result,
            page_texts=[{"number": 1, "text": "Texto do PDF", "source": "native"}],
        )
        stored = repository.get_by_id(document.id, self.demo_workspace_id)
        self.assertEqual(stored.sha256, checksum)
        self.assertEqual(stored.processing_status, "ready_for_review")
        self.assertEqual(stored.review_status, "pending")
        self.assertEqual(
            stored.page_texts,
            [{"number": 1, "text": "Texto do PDF", "source": "native"}],
        )
        self.assertEqual(stored.pdf_warnings, ["A página 2 não contém texto utilizável."])
        self.assertEqual(stored.validation_result["status"], "ready_for_review")
        self.assertEqual(stored.extracted_document["document_number"]["value"], "DOC-2026-1042")
        self.assertIsNone(repository.get_by_id(document.id, other_workspace_id))

    def test_async_job_claim_is_idempotent_and_uses_attempt_counter(self):
        seed_reference_order(self.session_factory)
        order_id = self.repository.get_entity_id_by_reference("PC-2026-0001", self.demo_workspace_id)
        repository = SqlAlchemyDocumentRepository(self.session_factory)
        document, duplicate = repository.create(
            workspace_id=self.demo_workspace_id,
            purchase_order_id=order_id,
            original_filename="queue.pdf",
            size_bytes=123,
            content_type="application/pdf",
            sha256=uuid4().hex + uuid4().hex,
            storage_key=f"{self.demo_workspace_id}/{uuid4().hex}.pdf",
        )
        self.assertFalse(duplicate)
        self.assertTrue(repository.set_job_id(
            document_id=document.id, workspace_id=self.demo_workspace_id, job_id="pg-job-first"
        ))

        claimed = repository.claim_for_processing(
            document_id=document.id, job_id="pg-job-first", max_attempts=3
        )
        duplicate_claim = repository.claim_for_processing(
            document_id=document.id, job_id="pg-job-duplicate", max_attempts=3
        )

        self.assertEqual(claimed.processing_status, "processing")
        self.assertEqual(claimed.processing_attempts, 1)
        self.assertIsNone(duplicate_claim)
        repository.record_attempt_error(
            document_id=document.id,
            job_id="pg-job-first",
            safe_message="Falha temporária simulada.",
        )
        repository.fail_job(
            document_id=document.id,
            job_id="pg-job-first",
            safe_message="Falha final segura.",
        )
        failed = repository.get_by_id(document.id, self.demo_workspace_id)
        self.assertEqual(failed.processing_status, "failed")
        self.assertEqual(failed.processing_error, "Falha temporária simulada.")

    def test_stale_document_can_be_requeued_without_old_job_claiming_it(self):
        seed_reference_order(self.session_factory)
        order_id = self.repository.get_entity_id_by_reference("PC-2026-0001", self.demo_workspace_id)
        repository = SqlAlchemyDocumentRepository(self.session_factory)
        document, _ = repository.create(
            workspace_id=self.demo_workspace_id,
            purchase_order_id=order_id,
            original_filename="stale.pdf",
            size_bytes=123,
            content_type="application/pdf",
            sha256=uuid4().hex + uuid4().hex,
            storage_key=f"{self.demo_workspace_id}/{uuid4().hex}.pdf",
        )
        repository.set_job_id(
            document_id=document.id, workspace_id=self.demo_workspace_id, job_id="stale-old-job"
        )
        repository.claim_for_processing(
            document_id=document.id, job_id="stale-old-job", max_attempts=3
        )
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
        with self.session_factory.begin() as session:
            stored = session.get(DocumentEntity, document.id)
            stored.processing_started_at = cutoff - timedelta(minutes=1)

        stale = repository.list_stale_processing(cutoff=cutoff)
        self.assertTrue(any(item.id == document.id for item in stale))
        self.assertTrue(repository.requeue_stale(
            document_id=document.id, cutoff=cutoff, job_id="stale-new-job"
        ))
        self.assertIsNone(repository.claim_for_processing(
            document_id=document.id, job_id="stale-old-job", max_attempts=3
        ))
        self.assertEqual(repository.claim_for_processing(
            document_id=document.id, job_id="stale-new-job", max_attempts=3
        ).processing_status, "processing")

    def test_supplier_order_and_item_uniqueness_constraints(self):
        suffix = uuid4().hex
        with Session(self.engine) as session:
            supplier = Supplier(
                workspace_id=self.demo_workspace_id,
                name="Fornecedor",
                identifier=f"UNIQUE-{suffix}",
            )
            session.add(supplier)
            session.flush()
            order = PurchaseOrderEntity(
                workspace_id=self.demo_workspace_id,
                reference=f"UNIQUE-{suffix}",
                supplier=supplier,
                currency="BRL",
                total=Decimal("2.00"),
                items=[
                    PurchaseOrderItem(
                        code="DUPLICATE",
                        description="Item",
                        quantity=Decimal("1"),
                        unit_price=Decimal("1.00"),
                        line_total=Decimal("1.00"),
                    )
                ],
            )
            session.add(order)
            session.commit()

            with self.assertRaises(IntegrityError):
                with session.begin_nested():
                    session.add(
                        Supplier(
                            workspace_id=self.demo_workspace_id,
                            name="Outro",
                            identifier=f"UNIQUE-{suffix}",
                        )
                    )
                    session.flush()
            session.rollback()

            with self.assertRaises(IntegrityError):
                with session.begin_nested():
                    session.add(
                        PurchaseOrderEntity(
                            workspace_id=self.demo_workspace_id,
                            reference=f"UNIQUE-{suffix}",
                            supplier_id=supplier.id,
                            currency="BRL",
                            total=Decimal("1.00"),
                        )
                    )
                    session.flush()
            session.rollback()

            stored_order = session.scalar(
                select(PurchaseOrderEntity).where(PurchaseOrderEntity.reference == f"UNIQUE-{suffix}")
            )
            with self.assertRaises(IntegrityError):
                with session.begin_nested():
                    stored_order.items.append(
                        PurchaseOrderItem(
                            code="DUPLICATE",
                            description="Item duplicado",
                            quantity=Decimal("1"),
                            unit_price=Decimal("1.00"),
                            line_total=Decimal("1.00"),
                        )
                    )
                    session.flush()

    def test_reference_seed_is_repeatable(self):
        existed_before = (
            self.repository.get_by_reference("PC-2026-0001", self.demo_workspace_id) is not None
        )
        inserted = seed_reference_order(self.session_factory)
        inserted_again = seed_reference_order(self.session_factory)

        self.assertEqual(inserted, not existed_before)
        self.assertFalse(inserted_again)
        order = self.repository.get_by_reference("PC-2026-0001", self.demo_workspace_id)
        self.assertIsNotNone(order)
        self.assertEqual(order.supplier_identifier, "FORN-0001")
        self.assertEqual([item.code for item in order.items], ["ARR-001", "PAR-002"])

    def test_review_revisions_approval_and_audit_persist_in_postgresql(self):
        seed_reference_order(self.session_factory)
        with Session(self.engine) as session:
            order_id = session.scalar(
                select(PurchaseOrderEntity.id).where(
                    PurchaseOrderEntity.workspace_id == self.demo_workspace_id,
                    PurchaseOrderEntity.reference == "PC-2026-0001",
                )
            )
            user = User(
                email=f"reviewer-{uuid4().hex}@example.test",
                password_hash=hash_password("integration-test-password"),
            )
            session.add(user)
            session.flush()
            session.add(
                WorkspaceMembership(
                    user_id=user.id,
                    workspace_id=self.demo_workspace_id,
                    role="operator",
                )
            )
            user_id = user.id
            session.commit()

        repository = SqlAlchemyDocumentRepository(self.session_factory)
        document, duplicate = repository.create(
            workspace_id=self.demo_workspace_id,
            purchase_order_id=order_id,
            original_filename="review.pdf",
            size_bytes=123,
            content_type="application/pdf",
            sha256=uuid4().hex + uuid4().hex,
            storage_key=f"{self.demo_workspace_id}/{uuid4().hex}.pdf",
        )
        self.assertFalse(duplicate)
        extracted = ReferenceDocument.model_validate(reference_payload())
        repository.save_result(
            document_id=document.id,
            workspace_id=self.demo_workspace_id,
            result=DocumentProcessingResult(
                document=extracted,
                pdf_warnings=[],
                validation=ValidationReport(status="ready_for_review", findings=[]),
                status="ready_for_review",
            ),
            page_texts=[{"number": 1, "text": REFERENCE_TEXT}],
        )

        revised_payload = reference_payload()
        revised_payload["document_number"]["value"] = "DOC-CORRIGIDO"
        review = DocumentReviewService(repository, self.repository)
        corrected = review.correct(
            document_id=document.id,
            workspace_id=self.demo_workspace_id,
            expected_version=1,
            reviewer_user_id=user_id,
            reviewed_document=ReferenceDocument.model_validate(revised_payload),
            justification="Correção validada na base PostgreSQL.",
        )
        self.assertEqual(corrected.review_version, 2)
        approved = review.approve(
            document_id=document.id,
            workspace_id=self.demo_workspace_id,
            expected_version=2,
            approver_user_id=user_id,
        )
        self.assertEqual(approved.review_status, "approved")

        with Session(self.engine) as session:
            stored = session.get(DocumentEntity, document.id)
            self.assertEqual(stored.extracted_document["document_number"]["value"], "DOC-2026-1042")
            self.assertEqual(stored.reviewed_document["document_number"]["value"], "DOC-CORRIGIDO")
            self.assertEqual(
                session.scalar(
                    select(DocumentApprovalEntity.reviewed_version).where(
                        DocumentApprovalEntity.document_id == document.id
                    )
                ),
                2,
            )
            self.assertEqual(
                session.scalar(
                    select(DocumentReviewRevisionEntity.version).where(
                        DocumentReviewRevisionEntity.document_id == document.id
                    )
                ),
                2,
            )
            event_types = set(
                session.scalars(
                    select(DocumentAuditEventEntity.event_type).where(
                        DocumentAuditEventEntity.document_id == document.id
                    )
                ).all()
            )
        self.assertEqual(event_types, {"review", "correction", "approval"})

    def test_document_assignment_swap_removal_filters_and_audit_persist_in_postgresql(self):
        seed_reference_order(self.session_factory)
        with Session(self.engine) as session:
            order_id = session.scalar(
                select(PurchaseOrderEntity.id).where(
                    PurchaseOrderEntity.workspace_id == self.demo_workspace_id,
                    PurchaseOrderEntity.reference == "PC-2026-0001",
                )
            )
            operator = User(
                email=f"assignment-operator-{uuid4().hex}@example.test",
                password_hash=hash_password("integration-test-password"),
            )
            first_assignee = User(
                email=f"assignment-first-{uuid4().hex}@example.test",
                password_hash=hash_password("integration-test-password"),
            )
            second_assignee = User(
                email=f"assignment-second-{uuid4().hex}@example.test",
                password_hash=hash_password("integration-test-password"),
            )
            session.add_all([operator, first_assignee, second_assignee])
            session.flush()
            for member in (operator, first_assignee, second_assignee):
                session.add(WorkspaceMembership(
                    user_id=member.id,
                    workspace_id=self.demo_workspace_id,
                    role="operator",
                ))
            document = DocumentEntity(
                workspace_id=self.demo_workspace_id,
                purchase_order_id=order_id,
                original_filename="assignment-integration.pdf",
                size_bytes=128,
                content_type="application/pdf",
                sha256=uuid4().hex + uuid4().hex,
                storage_key=f"{self.demo_workspace_id}/{uuid4().hex}.pdf",
                processing_status="ready_for_review",
                review_status="pending",
                review_version=1,
                extracted_document=reference_payload(),
                final_status="ready_for_review",
            )
            session.add(document)
            session.commit()
            document_id, operator_id = document.id, operator.id
            first_id, second_id = first_assignee.id, second_assignee.id

        repository = SqlAlchemyDocumentRepository(self.session_factory)
        assigned = repository.assign(
            document_id=document_id,
            workspace_id=self.demo_workspace_id,
            assignee_user_id=first_id,
            actor_user_id=operator_id,
        )
        self.assertEqual(assigned.assignee.email, first_assignee.email)
        repository.assign(
            document_id=document_id,
            workspace_id=self.demo_workspace_id,
            assignee_user_id=second_id,
            actor_user_id=operator_id,
        )
        self.assertEqual(
            [item.id for item in repository.list_for_workspace(
                self.demo_workspace_id, limit=100, offset=0, assignee_user_id=second_id
            )],
            [document_id],
        )
        repository.assign(
            document_id=document_id,
            workspace_id=self.demo_workspace_id,
            assignee_user_id=None,
            actor_user_id=operator_id,
        )
        self.assertEqual(
            [item.id for item in repository.list_for_workspace(
                self.demo_workspace_id, limit=100, offset=0, unassigned=True
            )].count(document_id),
            1,
        )
        with Session(self.engine) as session:
            stored = session.get(DocumentEntity, document_id)
            events = session.scalars(
                select(DocumentAuditEventEntity).where(
                    DocumentAuditEventEntity.document_id == document_id
                ).order_by(DocumentAuditEventEntity.id)
            ).all()
            self.assertIsNone(stored.assignee_user_id)
            self.assertEqual(stored.assigned_by_user_id, operator_id)
            self.assertIsNotNone(stored.assigned_at)
            self.assertEqual([event.event_type for event in events], ["assignment"] * 3)
            self.assertEqual(events[-1].details["previous_assignee_user_id"], second_id)
            self.assertIsNone(events[-1].details["assignee_user_id"])

    def test_erp_submission_snapshot_idempotency_and_audit_persist_in_postgresql(self):
        seed_reference_order(self.session_factory)
        with Session(self.engine) as session:
            order_id = session.scalar(
                select(PurchaseOrderEntity.id).where(
                    PurchaseOrderEntity.workspace_id == self.demo_workspace_id,
                    PurchaseOrderEntity.reference == "PC-2026-0001",
                )
            )
            user = User(
                email=f"erp-approver-{uuid4().hex}@example.test",
                password_hash=hash_password("integration-test-password"),
            )
            session.add(user)
            session.flush()
            approver_id = user.id
            session.add(WorkspaceMembership(
                user_id=approver_id,
                workspace_id=self.demo_workspace_id,
                role="approver",
            ))
            payload = reference_payload()
            document = DocumentEntity(
                workspace_id=self.demo_workspace_id,
                purchase_order_id=order_id,
                original_filename="approved-for-erp.pdf",
                size_bytes=100,
                content_type="application/pdf",
                sha256=uuid4().hex + uuid4().hex,
                storage_key=f"{self.demo_workspace_id}/{uuid4().hex}.pdf",
                processing_status="ready_for_review",
                review_status="approved",
                review_version=1,
                extracted_document=payload,
                final_status="ready_for_review",
            )
            session.add(document)
            session.flush()
            document_id = document.id
            session.add(DocumentApprovalEntity(
                document_id=document_id,
                workspace_id=self.demo_workspace_id,
                reviewed_version=1,
                approved_document=payload,
                validation_result={"status": "ready_for_review", "findings": []},
                approver_user_id=approver_id,
            ))
            session.commit()

        repository = SqlAlchemyERPSubmissionRepository(self.session_factory)
        first, should_enqueue = repository.request(
            document_id=document_id,
            workspace_id=self.demo_workspace_id,
            expected_version=1,
            requested_by_user_id=approver_id,
            job_id="erp-pg-first",
        )
        repeated, enqueue_again = repository.request(
            document_id=document_id,
            workspace_id=self.demo_workspace_id,
            expected_version=1,
            requested_by_user_id=approver_id,
            job_id="erp-pg-repeat",
        )
        self.assertTrue(should_enqueue)
        self.assertFalse(enqueue_again)
        self.assertEqual(first.id, repeated.id)

        worker = ERPSubmissionWorkerService(repository, DemoERPClient())
        self.assertEqual(
            worker.process(submission_id=first.id, job_id="erp-pg-first"), "succeeded"
        )
        self.assertEqual(
            worker.process(submission_id=first.id, job_id="erp-pg-first"), "skipped"
        )
        with Session(self.engine) as session:
            stored = session.get(ERPSubmissionEntity, first.id)
            self.assertEqual(stored.state, "succeeded")
            self.assertEqual(stored.attempts, 1)
            self.assertTrue(stored.erp_identifier.startswith("ERP-DEMO-"))
            snapshot = session.scalar(
                select(DocumentApprovalEntity.approved_document).where(
                    DocumentApprovalEntity.document_id == document_id
                )
            )
            event_types = set(session.scalars(
                select(DocumentAuditEventEntity.event_type).where(
                    DocumentAuditEventEntity.document_id == document_id
                )
            ))
        self.assertEqual(snapshot, payload)
        self.assertEqual(event_types, {"erp_submission", "erp_attempt", "erp_success"})

    def test_dashboard_aggregates_findings_and_states_with_postgresql_queries(self):
        suffix = uuid4().hex
        workspace = Workspace(slug=f"dashboard-{suffix}", name="Dashboard de integração")
        user = User(email=f"dashboard-{suffix}@example.test", password_hash="not-a-login")
        with Session(self.engine) as session:
            session.add_all([workspace, user])
            session.flush()
            supplier = Supplier(
                workspace_id=workspace.id, name="Fornecedor de teste", identifier=f"S-{suffix}"
            )
            session.add(supplier)
            session.flush()
            order = PurchaseOrderEntity(
                workspace_id=workspace.id, supplier_id=supplier.id, reference=f"PO-{suffix}",
                currency="BRL", total=Decimal("2.00"),
                items=[PurchaseOrderItem(
                    code="ITEM", description="Item de teste", quantity=Decimal("1"),
                    unit_price=Decimal("2.00"), line_total=Decimal("2.00"),
                )],
            )
            session.add(order)
            session.flush()
            docs = []
            for index, code in enumerate(("price_mismatch", "price_mismatch", "supplier_mismatch")):
                document = DocumentEntity(
                    workspace_id=workspace.id, purchase_order_id=order.id,
                    original_filename="private.pdf", size_bytes=10, content_type="application/pdf",
                    sha256=f"{suffix}-{index}", storage_key=f"private/{suffix}-{index}",
                    uploaded_at=datetime(2026, 10, 14, 12, index, tzinfo=timezone.utc),
                    processing_status=("queued" if index == 0 else "ready_for_review"),
                    review_status="pending", review_version=1,
                    reviewed_document={"corrected": True} if index == 2 else None,
                    final_status="divergent",
                    validation_result={"status": "divergent", "findings": [{
                        "severity": "divergence", "code": code, "message": "Informação de teste",
                    }]},
                )
                session.add(document)
                docs.append(document)
            session.flush()
            session.add_all([
                DocumentEntity(
                    workspace_id=workspace.id,
                    purchase_order_id=order.id,
                    original_filename=f"erp-{state}.pdf",
                    size_bytes=10,
                    content_type="application/pdf",
                    sha256=f"{suffix}-erp-{state}",
                    storage_key=f"private/{suffix}-erp-{state}",
                    uploaded_at=datetime(2025, 10, 14, 12, tzinfo=timezone.utc),
                    processing_status="ready_for_review",
                    review_status="approved",
                    review_version=1,
                    final_status="ready_for_review",
                )
                for state in ("queued", "processing", "succeeded", "failed")
            ])
            session.flush()
            erp_documents = session.scalars(
                select(DocumentEntity).where(
                    DocumentEntity.workspace_id == workspace.id,
                    DocumentEntity.original_filename.like("erp-%"),
                )
            ).all()
            session.add_all([
                ERPSubmissionEntity(
                    workspace_id=workspace.id, document_id=erp_document.id, approved_version=1,
                    requested_by_user_id=user.id, state=state,
                    requested_at=datetime(2026, 10, 14, 12, tzinfo=timezone.utc),
                )
                for erp_document, state in zip(erp_documents, ("queued", "processing", "succeeded", "failed"))
            ])
            workspace_id = workspace.id
            session.commit()

        result = SqlAlchemyDashboardRepository(self.session_factory).summarize(
            workspace_id, date(2026, 10, 14), date(2026, 10, 14)
        )
        self.assertEqual(result["documents"]["queued"], 1)
        self.assertEqual(result["documents"]["awaiting_review"], 1)
        self.assertEqual(result["documents"]["awaiting_approval"], 1)
        self.assertEqual(result["erp_submissions"], {"pending": 2, "succeeded": 1, "failed": 1})
        self.assertEqual(result["volume_total"], 3)
        self.assertEqual(result["common_divergences"], [
            {"code": "price_mismatch", "count": 2},
            {"code": "supplier_mismatch", "count": 1},
        ])


if __name__ == "__main__":
    unittest.main()
