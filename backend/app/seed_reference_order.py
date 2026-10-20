"""Cadastro explícito do pedido fictício: python -m backend.app.seed_reference_order."""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from backend.app.db import create_database
from backend.app.models.identity import Workspace
from backend.app.models.purchase_order import PurchaseOrderEntity, PurchaseOrderItem, Supplier
from backend.app.reference_order_data import REFERENCE_PURCHASE_ORDER


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
                PurchaseOrderEntity.reference == REFERENCE_PURCHASE_ORDER.reference,
                PurchaseOrderEntity.workspace_id == workspace.id,
            )
        )
        if existing is not None:
            return False

        supplier = session.scalar(
            select(Supplier).where(
                Supplier.identifier == REFERENCE_PURCHASE_ORDER.supplier_identifier,
                Supplier.workspace_id == workspace.id,
            )
        )
        if supplier is None:
            supplier = Supplier(
                workspace_id=workspace.id,
                name=REFERENCE_PURCHASE_ORDER.supplier_name,
                identifier=REFERENCE_PURCHASE_ORDER.supplier_identifier,
            )

        order = PurchaseOrderEntity(
            workspace_id=workspace.id,
            reference=REFERENCE_PURCHASE_ORDER.reference,
            supplier=supplier,
            currency=REFERENCE_PURCHASE_ORDER.currency,
            total=Decimal(REFERENCE_PURCHASE_ORDER.total),
            items=[
                PurchaseOrderItem(
                    code=item.code,
                    description=item.description,
                    quantity=Decimal(item.quantity),
                    unit_price=Decimal(item.unit_price),
                    line_total=Decimal(item.line_total),
                )
                for item in REFERENCE_PURCHASE_ORDER.items
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
