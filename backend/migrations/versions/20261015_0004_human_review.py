"""Adiciona versões de revisão, aprovações e auditoria append-only.

Revision ID: 20261015_0004
Revises: 20261014_0003
Create Date: 2026-10-15
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261015_0004"
down_revision: Union[str, None] = "20261014_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("documents", recreate="always") as batch:
            batch.drop_constraint("ck_documents_review_status_valid", type_="check")
            batch.add_column(
                sa.Column("review_version", sa.Integer(), server_default="1", nullable=False)
            )
            batch.add_column(sa.Column("reviewed_document", sa.JSON(), nullable=True))
            batch.create_check_constraint(
                "review_status_valid", "review_status IN ('pending', 'approved', 'rejected')"
            )
            batch.create_check_constraint("review_version_positive", "review_version > 0")
            batch.create_unique_constraint("uq_documents_id_workspace", ["id", "workspace_id"])
    else:
        op.drop_constraint("ck_documents_review_status_valid", "documents", type_="check")
        op.add_column(
            "documents",
            sa.Column("review_version", sa.Integer(), server_default="1", nullable=False),
        )
        op.add_column("documents", sa.Column("reviewed_document", sa.JSON(), nullable=True))
        op.create_check_constraint(
            "ck_documents_review_status_valid",
            "documents",
            "review_status IN ('pending', 'approved', 'rejected')",
        )
        op.create_check_constraint(
            "review_version_positive", "documents", "review_version > 0"
        )
        op.create_unique_constraint(
            "uq_documents_id_workspace", "documents", ["id", "workspace_id"]
        )

    op.create_table(
        "document_review_revisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("reviewed_document", sa.JSON(), nullable=False),
        sa.Column("validation_result", sa.JSON(), nullable=False),
        sa.Column("reviewer_user_id", sa.Integer(), nullable=False),
        sa.Column("justification", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("version > 1", name="ck_document_review_revisions_review_revision_version_valid"),
        sa.ForeignKeyConstraint(
            ["document_id", "workspace_id"], ["documents.id", "documents.workspace_id"],
            name="fk_document_review_revisions_document_id_documents", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_user_id"], ["users.id"],
            name="fk_document_review_revisions_reviewer_user_id_users", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_document_review_revisions"),
        sa.UniqueConstraint("document_id", "version", name="uq_document_review_revision_version"),
    )

    op.create_table(
        "document_approvals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("reviewed_version", sa.Integer(), nullable=False),
        sa.Column("approved_document", sa.JSON(), nullable=False),
        sa.Column("validation_result", sa.JSON(), nullable=False),
        sa.Column("approver_user_id", sa.Integer(), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id", "workspace_id"], ["documents.id", "documents.workspace_id"],
            name="fk_document_approvals_document_id_documents", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["approver_user_id"], ["users.id"],
            name="fk_document_approvals_approver_user_id_users", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_document_approvals"),
        sa.UniqueConstraint("document_id", "reviewed_version", name="uq_document_approval_version"),
    )

    op.create_table(
        "document_audit_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=False),
        sa.Column("justification", sa.Text(), nullable=True),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "event_type IN ('review', 'correction', 'approval', 'rejection')",
            name="ck_document_audit_events_document_audit_event_type_valid",
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "workspace_id"], ["documents.id", "documents.workspace_id"],
            name="fk_document_audit_events_document_id_documents", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"],
            name="fk_document_audit_events_actor_user_id_users", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_document_audit_events"),
    )
    op.create_index(
        "ix_document_audit_document_created",
        "document_audit_events",
        ["document_id", "created_at"],
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Esta migração não pode ser revertida automaticamente sem excluir o histórico de revisão."
    )
