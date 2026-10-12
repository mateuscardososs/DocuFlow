import unittest
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.models.base import Base
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

    def tearDown(self):
        self.engine.dispose()

    def test_creates_and_reads_supplier_order_and_decimal_items(self):
        with Session(self.engine) as session:
            supplier = Supplier(name="Metalúrgica Horizonte Ltda.", identifier="FORN-0001")
            order = PurchaseOrderEntity(
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

        result = self.repository.get_by_reference("PC-2026-0001")
        self.assertIsNotNone(result)
        self.assertEqual(result.supplier_identifier, "FORN-0001")
        self.assertEqual(result.items[0].quantity, "10.000")
        self.assertEqual(result.items[0].unit_price, "85.50")
        self.assertEqual(result.total, "1250.00")

    def test_missing_reference_returns_none(self):
        self.assertIsNone(self.repository.get_by_reference("PC-NAO-EXISTE"))

    def test_reference_order_seed_is_explicit_and_idempotent(self):
        self.assertTrue(seed_reference_order(self.session_factory))
        self.assertFalse(seed_reference_order(self.session_factory))

        order = self.repository.get_by_reference("PC-2026-0001")
        self.assertEqual(order.supplier_identifier, "FORN-0001")
        self.assertEqual([item.code for item in order.items], ["ARR-001", "PAR-002"])

    def test_supplier_and_order_references_are_unique(self):
        with Session(self.engine) as session:
            supplier = Supplier(name="Fornecedor", identifier="FORN-UNICO")
            session.add(supplier)
            session.flush()
            order = PurchaseOrderEntity(
                reference="PC-UNICO",
                supplier=supplier,
                currency="BRL",
                total=Decimal("1.00"),
            )
            session.add(order)
            session.commit()

            session.add(Supplier(name="Outro fornecedor", identifier="FORN-UNICO"))
            with self.assertRaises(IntegrityError):
                session.flush()
            session.rollback()

            order = session.scalar(
                select(PurchaseOrderEntity).where(PurchaseOrderEntity.reference == "PC-UNICO")
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
                    reference="PC-UNICO",
                    supplier=session.get(Supplier, supplier.id),
                    currency="BRL",
                    total=Decimal("2.00"),
                )
            )
            with self.assertRaises(IntegrityError):
                session.flush()

    def test_initial_migration_creates_all_tables_and_constraints(self):
        config_path = Path(__file__).parents[1] / "alembic.ini"
        config = Config(str(config_path))
        migration_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        with migration_engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

        inspector = sqlalchemy_inspect(migration_engine)
        self.assertTrue(
            {"suppliers", "purchase_orders", "purchase_order_items", "alembic_version"}
            .issubset(set(inspector.get_table_names()))
        )
        constraints = inspector.get_unique_constraints("purchase_orders")
        self.assertIn("uq_purchase_orders_reference", {item["name"] for item in constraints})
        self.assertIn(
            "uq_suppliers_identifier",
            {item["name"] for item in inspector.get_unique_constraints("suppliers")},
        )
        self.assertIn(
            "uq_purchase_order_items_order_code",
            {item["name"] for item in inspector.get_unique_constraints("purchase_order_items")},
        )
        for table in ("purchase_orders", "purchase_order_items"):
            self.assertTrue(sqlalchemy_inspect(migration_engine).get_check_constraints(table))
        migration_engine.dispose()


if __name__ == "__main__":
    unittest.main()
