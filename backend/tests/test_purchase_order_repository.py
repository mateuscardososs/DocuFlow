import unittest
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.models.base import Base
from backend.app.models.identity import Workspace
from backend.app.models.purchase_order import PurchaseOrderEntity, PurchaseOrderItem, Supplier
from backend.app.repositories.purchase_orders import SqlAlchemyPurchaseOrderRepository
from backend.app.seed_reference_order import seed_reference_order


class PurchaseOrderRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False)
        self.repository = SqlAlchemyPurchaseOrderRepository(self.session_factory)
        with self.session_factory.begin() as session:
            workspace = Workspace(slug="demo", name="Workspace demo")
            session.add(workspace)
            session.flush()
            self.workspace_id = workspace.id

    def tearDown(self):
        self.engine.dispose()

    def test_creates_and_reads_supplier_order_and_decimal_items(self):
        with Session(self.engine) as session:
            supplier = Supplier(
                workspace_id=self.workspace_id,
                name="Metalúrgica Horizonte Ltda.",
                identifier="FORN-0001",
            )
            order = PurchaseOrderEntity(
                workspace_id=self.workspace_id,
                reference="PC-2026-0001",
                supplier=supplier,
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
            session.commit()
            supplier_id = supplier.id
            order_id = order.id

        with Session(self.engine) as session:
            stored_supplier = session.get(Supplier, supplier_id)
            stored_order = session.scalar(
                select(PurchaseOrderEntity).where(PurchaseOrderEntity.id == order_id)
            )
            self.assertEqual(stored_supplier.name, "Metalúrgica Horizonte Ltda.")
            self.assertEqual(stored_order.items[0].quantity, Decimal("10.000"))

        result = self.repository.get_by_reference("PC-2026-0001", self.workspace_id)
        self.assertIsNotNone(result)
        self.assertEqual(result.supplier_identifier, "FORN-0001")
        self.assertEqual(result.items[0].quantity, "10.000")
        self.assertEqual(result.items[0].unit_price, "85.50")
        self.assertEqual(result.total, "1250.00")
        self.assertEqual(
            self.repository.get_entity_id_by_reference("PC-2026-0001", self.workspace_id),
            order_id,
        )

    def test_reference_is_scoped_to_workspace(self):
        other_workspace_id = self.workspace_id + 100
        with Session(self.engine) as session:
            supplier = Supplier(
                workspace_id=self.workspace_id,
                name="Fornecedor",
                identifier="FORN-UNICO",
            )
            session.add(supplier)
            session.flush()
            session.add(
                PurchaseOrderEntity(
                    workspace_id=self.workspace_id,
                    reference="PC-UNICO",
                    supplier=supplier,
                    currency="BRL",
                    total=Decimal("1.00"),
                )
            )
            session.commit()

        self.assertIsNotNone(self.repository.get_by_reference("PC-UNICO", self.workspace_id))
        self.assertIsNone(self.repository.get_by_reference("PC-UNICO", other_workspace_id))
        self.assertIsNone(self.repository.get_by_reference("PC-NAO-EXISTE", self.workspace_id))

    def test_reference_order_seed_is_explicit_and_idempotent(self):
        self.assertTrue(seed_reference_order(self.session_factory))
        self.assertFalse(seed_reference_order(self.session_factory))

        order = self.repository.get_by_reference("PC-2026-0001", self.workspace_id)
        self.assertEqual(order.supplier_identifier, "FORN-0001")
        self.assertEqual([item.code for item in order.items], ["ARR-001", "PAR-002"])

    def test_supplier_and_order_references_are_unique_inside_workspace(self):
        with Session(self.engine) as session:
            supplier = Supplier(
                workspace_id=self.workspace_id, name="Fornecedor", identifier="FORN-UNICO"
            )
            session.add(supplier)
            session.flush()
            order = PurchaseOrderEntity(
                workspace_id=self.workspace_id,
                reference="PC-UNICO",
                supplier=supplier,
                currency="BRL",
                total=Decimal("1.00"),
            )
            session.add(order)
            session.commit()

            session.add(
                Supplier(
                    workspace_id=self.workspace_id,
                    name="Outro fornecedor",
                    identifier="FORN-UNICO",
                )
            )
            with self.assertRaises(IntegrityError):
                session.flush()
            session.rollback()

            order = session.scalar(
                select(PurchaseOrderEntity).where(
                    PurchaseOrderEntity.reference == "PC-UNICO",
                    PurchaseOrderEntity.workspace_id == self.workspace_id,
                )
            )
            order.items.append(
                PurchaseOrderItem(
                    code="ITEM-UNICO",
                    description="Primeiro",
                    quantity=Decimal("1"),
                    unit_price=Decimal("1.00"),
                    line_total=Decimal("1.00"),
                )
            )
            session.commit()
            order.items.append(
                PurchaseOrderItem(
                    code="ITEM-UNICO",
                    description="Duplicado",
                    quantity=Decimal("1"),
                    unit_price=Decimal("1.00"),
                    line_total=Decimal("1.00"),
                )
            )
            with self.assertRaises(IntegrityError):
                session.flush()
            session.rollback()

            session.add(
                PurchaseOrderEntity(
                    workspace_id=self.workspace_id,
                    reference="PC-UNICO",
                    supplier=session.get(Supplier, supplier.id),
                    currency="BRL",
                    total=Decimal("2.00"),
                )
            )
            with self.assertRaises(IntegrityError):
                session.flush()

    def test_initial_and_workspace_migrations_preserve_existing_data(self):
        config_path = Path(__file__).parents[1] / "alembic.ini"
        config = Config(str(config_path))
        migration_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        with migration_engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "20261012_0001")
            connection.execute(
                text(
                    "INSERT INTO suppliers (id, name, identifier) "
                    "VALUES (17, 'Fornecedor legado', 'LEGADO-17')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO purchase_orders "
                    "(id, reference, supplier_id, currency, total) "
                    "VALUES (29, 'PC-LEGADO-29', 17, 'BRL', 99.90)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO purchase_order_items "
                    "(id, purchase_order_id, code, description, quantity, unit_price, line_total) "
                    "VALUES (39, 29, 'LEG-001', 'Item legado', 1, 99.90, 99.90)"
                )
            )
            command.upgrade(config, "20261015_0004")
            demo_workspace_id = connection.scalar(
                text("SELECT id FROM workspaces WHERE slug = 'demo'")
            )
            connection.execute(
                text(
                    "INSERT INTO documents "
                    "(id, workspace_id, purchase_order_id, original_filename, size_bytes, "
                    "content_type, sha256, storage_key, processing_status, review_status) "
                    "VALUES (101, :workspace_id, 29, 'legacy.pdf', 123, 'application/pdf', "
                    "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', "
                    "'1/legacy.pdf', 'succeeded', 'pending')"
                ),
                {"workspace_id": demo_workspace_id},
            )
            command.upgrade(config, "head")

        inspector = sqlalchemy_inspect(migration_engine)
        self.assertTrue(
            {
                "users",
                "workspaces",
                "workspace_memberships",
                "documents",
                "suppliers",
                "purchase_orders",
                "purchase_order_items",
                "alembic_version",
            }.issubset(set(inspector.get_table_names()))
        )
        self.assertIn(
            "uq_purchase_orders_workspace_reference",
            {item["name"] for item in inspector.get_unique_constraints("purchase_orders")},
        )
        self.assertIn(
            "uq_suppliers_workspace_identifier",
            {item["name"] for item in inspector.get_unique_constraints("suppliers")},
        )
        self.assertIn(
            "uq_purchase_order_items_order_code",
            {item["name"] for item in inspector.get_unique_constraints("purchase_order_items")},
        )
        self.assertIn(
            "uq_documents_workspace_sha256",
            {item["name"] for item in inspector.get_unique_constraints("documents")},
        )
        with migration_engine.connect() as connection:
            legacy = connection.execute(
                text(
                    "SELECT s.identifier, p.reference, i.code, w.slug "
                    "FROM suppliers AS s "
                    "JOIN purchase_orders AS p ON p.supplier_id = s.id "
                    "JOIN purchase_order_items AS i ON i.purchase_order_id = p.id "
                    "JOIN workspaces AS w ON w.id = p.workspace_id "
                    "WHERE s.id = 17 AND p.id = 29 AND i.id = 39"
                )
            ).one()
            migrated_status = connection.scalar(
                text("SELECT processing_status FROM documents WHERE id = 101")
            )
        self.assertEqual(legacy, ("LEGADO-17", "PC-LEGADO-29", "LEG-001", "demo"))
        self.assertEqual(migrated_status, "ready_for_review")
        migration_engine.dispose()


if __name__ == "__main__":
    unittest.main()
