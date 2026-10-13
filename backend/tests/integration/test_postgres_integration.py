"""Integration checks that run only against the dedicated local PostgreSQL test DB."""

import os
import unittest
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

from backend.app.models.identity import Workspace
from backend.app.models.document import DocumentEntity
from backend.app.schemas import ReferenceDocument
from backend.app.models.purchase_order import PurchaseOrderEntity, PurchaseOrderItem, Supplier
from backend.app.repositories.purchase_orders import SqlAlchemyPurchaseOrderRepository
from backend.app.repositories.documents import SqlAlchemyDocumentRepository
from backend.app.seed_reference_order import seed_reference_order
from backend.app.services.document_processing import DocumentProcessingResult
from backend.app.validation import ValidationReport
from backend.tests.test_document_processing import reference_payload


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
        self.assertEqual(revision, "20261014_0003")
        self.assertIn(
            "uq_purchase_orders_workspace_reference",
            {item["name"] for item in inspector.get_unique_constraints("purchase_orders")},
        )
        self.assertTrue(inspector.get_foreign_keys("purchase_orders"))
        self.assertIn("documents", inspector.get_table_names())
        self.assertIn(
            "uq_documents_workspace_sha256",
            {item["name"] for item in inspector.get_unique_constraints("documents")},
        )

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
            page_texts=[{"number": 1, "text": "Texto do PDF"}],
        )
        stored = repository.get_by_id(document.id, self.demo_workspace_id)
        self.assertEqual(stored.sha256, checksum)
        self.assertEqual(stored.processing_status, "succeeded")
        self.assertEqual(stored.review_status, "pending")
        self.assertEqual(stored.page_texts, [{"number": 1, "text": "Texto do PDF"}])
        self.assertEqual(stored.pdf_warnings, ["A página 2 não contém texto utilizável."])
        self.assertEqual(stored.validation_result["status"], "ready_for_review")
        self.assertEqual(stored.extracted_document["document_number"]["value"], "DOC-2026-1042")
        self.assertIsNone(repository.get_by_id(document.id, other_workspace_id))

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


if __name__ == "__main__":
    unittest.main()
