"""Adiciona persistência de documentos e seus resultados.

Revision ID: 20261014_0003
Revises: 20261013_0002
Create Date: 2026-10-14
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261014_0003"
down_revision: Union[str, None] = "20261013_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("purchase_orders", recreate="always") as batch:
            batch.create_unique_constraint(
                "uq_purchase_orders_id_workspace", ["id", "workspace_id"]
            )
    else:
        op.create_unique_constraint(
            "uq_purchase_orders_id_workspace",
            "purchase_orders",
            ["id", "workspace_id"],
        )
    op.create_table(
        "documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("purchase_order_id", sa.Integer(), nullable=False),
        sa.Column("original_filename", sa.String(length=512), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column(
            "uploaded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "processing_status",
            sa.String(length=20),
            server_default="processing",
            nullable=False,
        ),
        sa.Column(
            "review_status", sa.String(length=20), server_default="pending", nullable=False
        ),
        sa.Column("processing_error", sa.Text(), nullable=True),
        sa.Column("page_texts", sa.JSON(), nullable=True),
        sa.Column("extracted_document", sa.JSON(), nullable=True),
        sa.Column("pdf_warnings", sa.JSON(), nullable=True),
        sa.Column("validation_result", sa.JSON(), nullable=True),
        sa.Column("final_status", sa.String(length=30), nullable=True),
        sa.CheckConstraint(
            "processing_status IN ('processing', 'succeeded', 'failed')",
            name="ck_documents_processing_status_valid",
        ),
        sa.CheckConstraint(
            "review_status = 'pending'", name="ck_documents_review_status_valid"
        ),
        sa.CheckConstraint(
            "final_status IS NULL OR final_status IN ('blocked', 'divergent', 'ready_for_review')",
            name="ck_documents_final_status_valid",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_documents_workspace_id_workspaces",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["purchase_order_id", "workspace_id"],
            ["purchase_orders.id", "purchase_orders.workspace_id"],
            name="fk_documents_order_workspace",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_documents"),
        sa.UniqueConstraint(
            "workspace_id", "sha256", name="uq_documents_workspace_sha256"
        ),
    )
    op.create_index(
        "ix_documents_workspace_uploaded_at",
        "documents",
        ["workspace_id", "uploaded_at"],
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Esta revisão não pode ser revertida automaticamente pois excluiria arquivos e resultados."
    )
