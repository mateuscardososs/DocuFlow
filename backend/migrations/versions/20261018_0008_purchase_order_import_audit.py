"""Adiciona auditoria resumida para importação de pedidos por CSV.

Revision ID: 20261018_0008
Revises: 20261017_0007
Create Date: 2026-10-18
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261018_0008"
down_revision: Union[str, None] = "20261017_0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "purchase_order_import_audits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("total_rows", sa.Integer(), nullable=False),
        sa.Column("imported_orders", sa.Integer(), nullable=False),
        sa.Column("ignored_duplicates", sa.Integer(), nullable=False),
        sa.Column("error_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('succeeded', 'partial', 'conflict', 'failed')",
            name="ck_purchase_order_import_audits_purchase_order_import_outcome_valid",
        ),
        sa.CheckConstraint("total_rows >= 0", name="ck_purchase_order_import_audits_purchase_order_import_rows_nonnegative"),
        sa.CheckConstraint("imported_orders >= 0", name="ck_purchase_order_import_audits_purchase_order_import_count_nonnegative"),
        sa.CheckConstraint("ignored_duplicates >= 0", name="ck_purchase_order_import_audits_purchase_order_import_duplicates_nonnegative"),
        sa.CheckConstraint("error_count >= 0", name="ck_purchase_order_import_audits_purchase_order_import_errors_nonnegative"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    raise NotImplementedError("A migração preserva o histórico de importação e não será revertida automaticamente.")
