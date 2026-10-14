"""Solicitações assíncronas para o ERP local de demonstração."""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.models.base import Base


class ERPSubmissionEntity(Base):
    __tablename__ = "erp_submissions"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "document_id", "approved_version",
            name="uq_erp_submission_workspace_document_version",
        ),
        ForeignKeyConstraint(
            ["document_id", "workspace_id"],
            ["documents.id", "documents.workspace_id"],
            name="fk_erp_submission_document_workspace",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "state IN ('queued', 'processing', 'succeeded', 'failed')",
            name="erp_submission_state_valid",
        ),
        CheckConstraint("approved_version > 0", name="erp_submission_version_positive"),
        CheckConstraint("attempts >= 0", name="erp_submission_attempts_nonnegative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    document_id: Mapped[int] = mapped_column(Integer, nullable=False)
    approved_version: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    erp_identifier: Mapped[str | None] = mapped_column(String(255), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
