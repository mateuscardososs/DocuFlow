"""Documentos analisados e seus resultados persistidos por workspace."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.models.base import Base


class DocumentEntity(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("workspace_id", "sha256", name="uq_documents_workspace_sha256"),
        ForeignKeyConstraint(
            ["purchase_order_id", "workspace_id"],
            ["purchase_orders.id", "purchase_orders.workspace_id"],
            name="fk_documents_order_workspace",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "processing_status IN ('processing', 'succeeded', 'failed')",
            name="processing_status_valid",
        ),
        CheckConstraint("review_status = 'pending'", name="review_status_valid"),
        CheckConstraint(
            "final_status IS NULL OR final_status IN ('blocked', 'divergent', 'ready_for_review')",
            name="final_status_valid",
        ),
        Index("ix_documents_workspace_uploaded_at", "workspace_id", "uploaded_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    purchase_order_id: Mapped[int] = mapped_column(Integer, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    processing_status: Mapped[str] = mapped_column(String(20), nullable=False, default="processing")
    review_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    processing_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_texts: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    extracted_document: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    pdf_warnings: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    validation_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    final_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
