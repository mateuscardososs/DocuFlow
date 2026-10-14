"""Entidades persistidas de fornecedores e pedidos de compra."""

from decimal import Decimal
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Numeric,
    String,
    DateTime,
    Integer,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.models.base import Base


class Supplier(Base):
    __tablename__ = "suppliers"
    __table_args__ = (
        UniqueConstraint("workspace_id", "identifier", name="uq_suppliers_workspace_identifier"),
        UniqueConstraint("id", "workspace_id", name="uq_suppliers_id_workspace"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    identifier: Mapped[str] = mapped_column(String(100), nullable=False)

    purchase_orders: Mapped[list["PurchaseOrderEntity"]] = relationship(back_populates="supplier")


class PurchaseOrderEntity(Base):
    __tablename__ = "purchase_orders"
    __table_args__ = (
        UniqueConstraint("workspace_id", "reference", name="uq_purchase_orders_workspace_reference"),
        UniqueConstraint("id", "workspace_id", name="uq_purchase_orders_id_workspace"),
        ForeignKeyConstraint(
            ["supplier_id", "workspace_id"],
            ["suppliers.id", "suppliers.workspace_id"],
            name="fk_purchase_orders_supplier_workspace",
            ondelete="RESTRICT",
        ),
        CheckConstraint("length(currency) = 3", name="currency_code_length"),
        CheckConstraint("total >= 0", name="total_nonnegative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    reference: Mapped[str] = mapped_column(String(100), nullable=False)
    supplier_id: Mapped[int] = mapped_column(nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    total: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)

    supplier: Mapped[Supplier] = relationship(
        back_populates="purchase_orders",
        foreign_keys="[PurchaseOrderEntity.supplier_id, PurchaseOrderEntity.workspace_id]",
    )
    items: Mapped[list["PurchaseOrderItem"]] = relationship(
        back_populates="purchase_order",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="PurchaseOrderItem.id",
    )


class PurchaseOrderItem(Base):
    __tablename__ = "purchase_order_items"
    __table_args__ = (
        UniqueConstraint("purchase_order_id", "code", name="uq_purchase_order_items_order_code"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_price >= 0", name="unit_price_nonnegative"),
        CheckConstraint("line_total >= 0", name="line_total_nonnegative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    purchase_order_id: Mapped[int] = mapped_column(
        ForeignKey("purchase_orders.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    line_total: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)

    purchase_order: Mapped[PurchaseOrderEntity] = relationship(back_populates="items")


class PurchaseOrderImportAuditEntity(Base):
    """Registro resumido e append-only de cada confirmação de importação."""

    __tablename__ = "purchase_order_import_audits"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('succeeded', 'partial', 'conflict', 'failed')",
            name="purchase_order_import_outcome_valid",
        ),
        CheckConstraint("total_rows >= 0", name="purchase_order_import_rows_nonnegative"),
        CheckConstraint("imported_orders >= 0", name="purchase_order_import_count_nonnegative"),
        CheckConstraint("ignored_duplicates >= 0", name="purchase_order_import_duplicates_nonnegative"),
        CheckConstraint("error_count >= 0", name="purchase_order_import_errors_nonnegative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    actor_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    total_rows: Mapped[int] = mapped_column(Integer, nullable=False)
    imported_orders: Mapped[int] = mapped_column(Integer, nullable=False)
    ignored_duplicates: Mapped[int] = mapped_column(Integer, nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
