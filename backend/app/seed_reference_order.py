"""Cadastro explícito do pedido fictício: python -m backend.app.seed_reference_order."""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from backend.app.db import create_database
from backend.app.models.identity import Workspace
from backend.app.models.purchase_order import PurchaseOrderEntity, PurchaseOrderItem, Supplier


def seed_reference_order(session_factory: sessionmaker) -> bool:
    """Insere o fornecedor e pedido de referência se ainda não existirem."""
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "demo"))
        if workspace is None:
            workspace = Workspace(slug="demo", name="Workspace de demonstração")
            session.add(workspace)
            session.flush()

        existing = session.scalar(
            select(PurchaseOrderEntity).where(
                PurchaseOrderEntity.reference == "PC-2026-0001",
                PurchaseOrderEntity.workspace_id == workspace.id,
            )
        )
        if existing is not None:
            return False

        supplier = session.scalar(
            select(Supplier).where(
                Supplier.identifier == "FORN-0001",
                Supplier.workspace_id == workspace.id,
            )
        )
        if supplier is None:
            supplier = Supplier(
                workspace_id=workspace.id,
                name="Metalúrgica Horizonte Ltda.",
                identifier="FORN-0001",
            )

        order = PurchaseOrderEntity(
            workspace_id=workspace.id,
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
    return True


def main() -> None:
    database = create_database()
    inserted = seed_reference_order(database.session_factory)
    database.engine.dispose()
    print("Pedido PC-2026-0001 cadastrado." if inserted else "Pedido PC-2026-0001 já existe.")


if __name__ == "__main__":
    main()
