"""Cria versões append-only da política comercial dos workspaces."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261020_0010"
down_revision: Union[str, None] = "20261019_0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workspace_commercial_policy_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("currency_tolerances", sa.JSON(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("workspace_id", "version", name="uq_workspace_policy_version"),
        sa.CheckConstraint(
            "version > 0",
            name="ck_workspace_commercial_policy_versions_workspace_policy_version_positive",
        ),
    )


def downgrade() -> None:
    raise NotImplementedError(
        "A migração preserva o histórico de políticas e não o apaga automaticamente."
    )
