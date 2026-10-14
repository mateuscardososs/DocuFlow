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
        UniqueConstraint("id", "workspace_id", name="uq_documents_id_workspace"),
        UniqueConstraint("workspace_id", "sha256", name="uq_documents_workspace_sha256"),
        ForeignKeyConstraint(
            ["purchase_order_id", "workspace_id"],
            ["purchase_orders.id", "purchase_orders.workspace_id"],
            name="fk_documents_order_workspace",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "processing_status IN ('queued', 'processing', 'ready_for_review', 'failed')",
            name="processing_status_valid",
        ),
        CheckConstraint(
            "review_status IN ('pending', 'approved', 'rejected')",
            name="review_status_valid",
        ),
        CheckConstraint("review_version > 0", name="review_version_positive"),
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
    processing_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="queued", server_default="queued"
    )
    processing_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    processing_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    active_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    review_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    review_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    processing_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_texts: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    extracted_document: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    reviewed_document: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    pdf_warnings: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    validation_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    final_status: Mapped[str | None] = mapped_column(String(30), nullable=True)


class DocumentReviewRevisionEntity(Base):
    __tablename__ = "document_review_revisions"
    __table_args__ = (
        UniqueConstraint("document_id", "version", name="uq_document_review_revision_version"),
        ForeignKeyConstraint(
            ["document_id", "workspace_id"],
            ["documents.id", "documents.workspace_id"],
            name="fk_review_revision_document_workspace",
            ondelete="RESTRICT",
        ),
        CheckConstraint("version > 1", name="review_revision_version_valid"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(Integer, nullable=False)
    workspace_id: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    reviewed_document: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    validation_result: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    reviewer_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    justification: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DocumentApprovalEntity(Base):
    __tablename__ = "document_approvals"
    __table_args__ = (
        UniqueConstraint("document_id", "reviewed_version", name="uq_document_approval_version"),
        ForeignKeyConstraint(
            ["document_id", "workspace_id"],
            ["documents.id", "documents.workspace_id"],
            name="fk_document_approval_document_workspace",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(Integer, nullable=False)
    workspace_id: Mapped[int] = mapped_column(Integer, nullable=False)
    reviewed_version: Mapped[int] = mapped_column(Integer, nullable=False)
    approved_document: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    validation_result: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    approver_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DocumentAuditEventEntity(Base):
    __tablename__ = "document_audit_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["document_id", "workspace_id"],
            ["documents.id", "documents.workspace_id"],
            name="fk_document_audit_document_workspace",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "event_type IN ('review', 'correction', 'approval', 'rejection', "
            "'erp_submission', 'erp_attempt', 'erp_success', 'erp_failure')",
            name="document_audit_event_type_valid",
        ),
        Index("ix_document_audit_document_created", "document_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(Integer, nullable=False)
    workspace_id: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    justification: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
