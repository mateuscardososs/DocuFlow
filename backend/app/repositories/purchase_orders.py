"""Interface de consulta e adaptador SQLAlchemy para pedidos de compra."""

from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import joinedload, selectinload, sessionmaker

from backend.app.models.purchase_order import PurchaseOrderEntity
from backend.app.schemas import PurchaseOrder


class PurchaseOrderRepository(Protocol):
    def get_by_reference(self, reference: str) -> PurchaseOrder | None: ...


class SqlAlchemyPurchaseOrderRepository:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def get_by_reference(self, reference: str) -> PurchaseOrder | None:
        statement = (
            select(PurchaseOrderEntity)
            .options(
                joinedload(PurchaseOrderEntity.supplier),
                selectinload(PurchaseOrderEntity.items),
            )
            .where(PurchaseOrderEntity.reference == reference)
        )
        with self._session_factory() as session:
            order = session.scalar(statement)
            if order is None:
                return None
            return PurchaseOrder.model_validate(
                {
                    "reference": order.reference,
                    "supplier_name": order.supplier.name,
                    "supplier_identifier": order.supplier.identifier,
                    "currency": order.currency,
                    "items": [
                        {
                            "code": item.code,
                            "description": item.description,
                            "quantity": format(item.quantity, "f"),
                            "unit_price": format(item.unit_price, "f"),
                            "line_total": format(item.line_total, "f"),
                        }
                        for item in order.items
                    ],
                    "total": format(order.total, "f"),
                }
            )
