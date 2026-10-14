"""Interface de consulta e adaptador SQLAlchemy para pedidos de compra."""

from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import joinedload, selectinload, sessionmaker

from backend.app.models.purchase_order import PurchaseOrderEntity, Supplier
from backend.app.schemas import PurchaseOrder


class PurchaseOrderRepository(Protocol):
    def get_by_reference(self, reference: str, workspace_id: int) -> PurchaseOrder | None: ...
    def get_by_id(self, order_id: int, workspace_id: int) -> PurchaseOrder | None: ...
    def get_entity_id_by_reference(self, reference: str, workspace_id: int) -> int | None: ...
    def get_display_fields_by_ids(
        self, workspace_id: int, order_ids: set[int]
    ) -> dict[int, dict[str, str]]: ...


class SqlAlchemyPurchaseOrderRepository:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def get_by_reference(self, reference: str, workspace_id: int) -> PurchaseOrder | None:
        statement = (
            select(PurchaseOrderEntity)
            .options(
                joinedload(PurchaseOrderEntity.supplier),
                selectinload(PurchaseOrderEntity.items),
            )
            .where(
                PurchaseOrderEntity.reference == reference,
                PurchaseOrderEntity.workspace_id == workspace_id,
            )
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

    def get_by_id(self, order_id: int, workspace_id: int) -> PurchaseOrder | None:
        statement = (
            select(PurchaseOrderEntity)
            .options(
                joinedload(PurchaseOrderEntity.supplier),
                selectinload(PurchaseOrderEntity.items),
            )
            .where(
                PurchaseOrderEntity.id == order_id,
                PurchaseOrderEntity.workspace_id == workspace_id,
            )
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

    def get_entity_id_by_reference(self, reference: str, workspace_id: int) -> int | None:
        with self._session_factory() as session:
            return session.scalar(
                select(PurchaseOrderEntity.id).where(
                    PurchaseOrderEntity.reference == reference,
                    PurchaseOrderEntity.workspace_id == workspace_id,
                )
            )

    def get_display_fields_by_ids(
        self, workspace_id: int, order_ids: set[int]
    ) -> dict[int, dict[str, str]]:
        if not order_ids:
            return {}
        with self._session_factory() as session:
            rows = session.execute(
                select(PurchaseOrderEntity.id, PurchaseOrderEntity.reference, Supplier.name)
                .join(Supplier, PurchaseOrderEntity.supplier_id == Supplier.id)
                .where(
                    PurchaseOrderEntity.workspace_id == workspace_id,
                    PurchaseOrderEntity.id.in_(order_ids),
                    Supplier.workspace_id == workspace_id,
                )
            ).all()
        return {
            order_id: {"reference": reference, "supplier_name": supplier_name}
            for order_id, reference, supplier_name in rows
        }
