"""Versões imutáveis da política comercial de cada workspace."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.models.base import Base
from backend.app.models.identity import User


class WorkspaceCommercialPolicyEntity(Base):
    __tablename__ = "workspace_commercial_policy_versions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "version", name="uq_workspace_policy_version"),
        CheckConstraint("version > 0", name="workspace_policy_version_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    currency_tolerances: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False)
    created_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    author: Mapped[User] = relationship("User", lazy="joined")
