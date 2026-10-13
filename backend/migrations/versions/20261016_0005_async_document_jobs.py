"""Adiciona estado de fila e controle de tentativas sem remover resultados.

Revision ID: 20261016_0005
Revises: 20261015_0004
Create Date: 2026-10-16
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261016_0005"
down_revision: Union[str, None] = "20261015_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("documents", recreate="always") as batch:
            batch.drop_constraint("ck_documents_processing_status_valid", type_="check")
            batch.add_column(sa.Column("processing_attempts", sa.Integer(), server_default="0", nullable=False))
            batch.add_column(sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True))
            batch.add_column(sa.Column("active_job_id", sa.String(length=64), nullable=True))
        op.execute(
            "UPDATE documents SET processing_status = 'ready_for_review' "
            "WHERE processing_status = 'succeeded'"
        )
        with op.batch_alter_table("documents", recreate="always") as batch:
            batch.create_check_constraint(
                "processing_status_valid",
                "processing_status IN ('queued', 'processing', 'ready_for_review', 'failed')",
            )
    else:
        op.drop_constraint("ck_documents_processing_status_valid", "documents", type_="check")
        op.add_column(
            "documents", sa.Column("processing_attempts", sa.Integer(), server_default="0", nullable=False)
        )
        op.add_column(
            "documents", sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True)
        )
        op.add_column("documents", sa.Column("active_job_id", sa.String(length=64), nullable=True))
        op.execute(
            "UPDATE documents SET processing_status = 'ready_for_review' "
            "WHERE processing_status = 'succeeded'"
        )
        op.create_check_constraint(
            "processing_status_valid",
            "documents",
            "processing_status IN ('queued', 'processing', 'ready_for_review', 'failed')",
        )


def downgrade() -> None:
    raise NotImplementedError(
        "Esta migração preserva tentativas e resultados; não é revertida automaticamente."
    )
