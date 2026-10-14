"""Adiciona solicitações ERP com idempotência e auditoria append-only.

Revision ID: 20261017_0007
Revises: 20261016_0006
Create Date: 2026-10-17
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261017_0007"
down_revision: Union[str, None] = "20261016_0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_AUDIT_CHECK = (
    "event_type IN ('review', 'correction', 'approval', 'rejection', "
    "'erp_submission', 'erp_attempt', 'erp_success', 'erp_failure')"
)


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("document_audit_events", recreate="always") as batch:
            batch.drop_constraint(
                "ck_document_audit_events_document_audit_event_type_valid", type_="check"
            )
            batch.create_check_constraint(
                "document_audit_event_type_valid", _AUDIT_CHECK
            )
    else:
        op.drop_constraint(
            "ck_document_audit_events_document_audit_event_type_valid",
            "document_audit_events",
            type_="check",
        )
        op.create_check_constraint(
            "document_audit_event_type_valid", "document_audit_events", _AUDIT_CHECK
        )

    op.create_table(
        "erp_submissions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("approved_version", sa.Integer(), nullable=False),
        sa.Column("requested_by_user_id", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("active_job_id", sa.String(length=64), nullable=True),
        sa.Column("erp_identifier", sa.String(length=255), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "state IN ('queued', 'processing', 'succeeded', 'failed')",
            name="ck_erp_submissions_erp_submission_state_valid",
        ),
        sa.CheckConstraint("approved_version > 0", name="ck_erp_submissions_erp_submission_version_positive"),
        sa.CheckConstraint("attempts >= 0", name="ck_erp_submissions_erp_submission_attempts_nonnegative"),
        sa.ForeignKeyConstraint(
            ["document_id", "workspace_id"],
            ["documents.id", "documents.workspace_id"],
            name="fk_erp_submission_document_workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"], ["users.id"],
            name="fk_erp_submissions_requested_by_user_id_users", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"],
            name="fk_erp_submissions_workspace_id_workspaces", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_erp_submissions"),
        sa.UniqueConstraint(
            "workspace_id", "document_id", "approved_version",
            name="uq_erp_submission_workspace_document_version",
        ),
    )


def downgrade() -> None:
    raise NotImplementedError(
        "A migração preserva solicitações e auditoria ERP; não será revertida automaticamente."
    )
