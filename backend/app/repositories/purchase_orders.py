"""Interface de consulta e adaptador SQLAlchemy para pedidos de compra."""

from decimal import Decimal
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import joinedload, selectinload, sessionmaker

from backend.app.models.purchase_order import (
    PurchaseOrderEntity,
    PurchaseOrderImportAuditEntity,
    PurchaseOrderItem,
    Supplier,
)
from backend.app.schemas import PurchaseOrder
from backend.app.services.purchase_order_csv_import import (
    PurchaseOrderImportConflict,
    PurchaseOrderImportPersistenceError,
    purchase_orders_equal,
)


class PurchaseOrderRepository(Protocol):
    def get_by_reference(self, reference: str, workspace_id: int) -> PurchaseOrder | None: ...
    def get_by_id(self, order_id: int, workspace_id: int) -> PurchaseOrder | None: ...
    def get_entity_id_by_reference(self, reference: str, workspace_id: int) -> int | None: ...
    def get_display_fields_by_ids(
        self, workspace_id: int, order_ids: set[int]
    ) -> dict[int, dict[str, str]]: ...
    def get_supplier_name_by_identifier(self, identifier: str, workspace_id: int) -> str | None: ...
    def import_orders(
        self, orders: list[PurchaseOrder], *, workspace_id: int, actor_user_id: int,
        total_rows: int, duplicate_references: set[str], error_count: int,
    ) -> tuple[list[str], list[str]]: ...


class SqlAlchemyPurchaseOrderRepository:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def get_supplier_name_by_identifier(self, identifier: str, workspace_id: int) -> str | None:
        with self._session_factory() as session:
            return session.scalar(
                select(Supplier.name).where(
                    Supplier.identifier == identifier,
                    Supplier.workspace_id == workspace_id,
                )
            )

    def import_orders(
        self, orders: list[PurchaseOrder], *, workspace_id: int, actor_user_id: int,
        total_rows: int, duplicate_references: set[str], error_count: int,
    ) -> tuple[list[str], list[str]]:
        imported: list[str] = []
        ignored: list[str] = []
        try:
            with self._session_factory.begin() as session:
                for order in orders:
                    supplier = session.scalar(
                        select(Supplier).where(
                            Supplier.identifier == order.supplier_identifier,
                            Supplier.workspace_id == workspace_id,
                        )
                    )
                    if supplier is None:
                        raise ValueError("Um fornecedor informado não pertence a este workspace.")
                    normalized = order.model_copy(update={"supplier_name": supplier.name})
                    existing = session.scalar(
                        select(PurchaseOrderEntity)
                        .options(
                            joinedload(PurchaseOrderEntity.supplier),
                            selectinload(PurchaseOrderEntity.items),
                        )
                        .where(
                            PurchaseOrderEntity.reference == order.reference,
                            PurchaseOrderEntity.workspace_id == workspace_id,
                        )
                    )
                    if existing is not None:
                        stored = _purchase_order_schema(existing)
                        if purchase_orders_equal(normalized, stored):
                            ignored.append(order.reference)
                            continue
                        raise PurchaseOrderImportConflict(order.reference)
                    entity = PurchaseOrderEntity(
                        workspace_id=workspace_id,
                        reference=normalized.reference,
                        supplier_id=supplier.id,
                        currency=normalized.currency,
                        total=Decimal(normalized.total),
                        items=[
                            PurchaseOrderItem(
                                code=item.code,
                                description=item.description,
                                quantity=Decimal(item.quantity),
                                unit_price=Decimal(item.unit_price),
                                line_total=Decimal(item.line_total),
                            )
                            for item in normalized.items
                        ],
                    )
                    session.add(entity)
                    imported.append(normalized.reference)
                outcome = "partial" if error_count and imported else (
                    "succeeded" if imported or ignored else "failed"
                )
                session.add(
                    PurchaseOrderImportAuditEntity(
                        workspace_id=workspace_id,
                        actor_user_id=actor_user_id,
                        outcome=outcome,
                        total_rows=total_rows,
                        imported_orders=len(imported),
                        ignored_duplicates=len(ignored),
                        error_count=error_count,
                    )
                )
        except PurchaseOrderImportConflict as error:
            self._record_import_audit(
                workspace_id, actor_user_id, "conflict", total_rows, 0,
                len(duplicate_references), error_count + 1,
            )
            raise error
        except Exception:
            self._record_import_audit(
                workspace_id, actor_user_id, "failed", total_rows, 0,
                len(duplicate_references), error_count + 1,
            )
            raise PurchaseOrderImportPersistenceError(
                "Não foi possível gravar os pedidos importados."
            ) from None
        return imported, ignored

    def _record_import_audit(
        self, workspace_id: int, actor_user_id: int, outcome: str, total_rows: int,
        imported_orders: int, ignored_duplicates: int, error_count: int,
    ) -> None:
        try:
            with self._session_factory.begin() as session:
                session.add(PurchaseOrderImportAuditEntity(
                    workspace_id=workspace_id,
                    actor_user_id=actor_user_id,
                    outcome=outcome,
                    total_rows=total_rows,
                    imported_orders=imported_orders,
                    ignored_duplicates=ignored_duplicates,
                    error_count=error_count,
                ))
        except Exception:
            # Falhas na própria auditoria não devem expor detalhes internos pela API.
            pass

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
            return _purchase_order_schema(order)

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
            return _purchase_order_schema(order)

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


def _purchase_order_schema(order: PurchaseOrderEntity) -> PurchaseOrder:
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
