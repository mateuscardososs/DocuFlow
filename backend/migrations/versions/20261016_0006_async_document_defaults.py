"""Alinha o default do banco ao estado de fila usado no upload.

Revision ID: 20261016_0006
Revises: 20261016_0005
Create Date: 2026-10-16
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261016_0006"
down_revision: Union[str, None] = "20261016_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("documents", recreate="always") as batch:
            batch.alter_column(
                "processing_status",
                existing_type=sa.String(length=20),
                existing_nullable=False,
                server_default="queued",
            )
    else:
        op.alter_column(
            "documents",
            "processing_status",
            existing_type=sa.String(length=20),
            existing_nullable=False,
            server_default="queued",
        )


def downgrade() -> None:
    raise NotImplementedError("O default de processamento não é revertido automaticamente.")
