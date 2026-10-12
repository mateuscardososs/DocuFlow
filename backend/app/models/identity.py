"""Usuários, workspaces e papéis concedidos dentro de cada workspace."""

from sqlalchemy import CheckConstraint, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.models.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)

    memberships: Mapped[list["WorkspaceMembership"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    memberships: Mapped[list["WorkspaceMembership"]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )


class WorkspaceMembership(Base):
    __tablename__ = "workspace_memberships"
    __table_args__ = (
        CheckConstraint("role IN ('operator', 'approver')", name="valid_workspace_role"),
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(20), primary_key=True)

    user: Mapped[User] = relationship(back_populates="memberships")
    workspace: Mapped[Workspace] = relationship(back_populates="memberships")
