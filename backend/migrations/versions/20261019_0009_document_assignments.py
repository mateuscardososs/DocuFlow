"""Atribui documentos a membros do workspace e registra alterações na auditoria."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261019_0009"
down_revision: Union[str, None] = "20261018_0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_OLD_AUDIT_CHECK = (
    "event_type IN ('review', 'correction', 'approval', 'rejection', "
    "'erp_submission', 'erp_attempt', 'erp_success', 'erp_failure')"
)
_NEW_AUDIT_CHECK = (
    "event_type IN ('review', 'correction', 'approval', 'rejection', "
    "'erp_submission', 'erp_attempt', 'erp_success', 'erp_failure', 'assignment')"
)


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("documents", recreate="always") as batch:
            batch.add_column(sa.Column("assignee_user_id", sa.Integer(), nullable=True))
            batch.add_column(sa.Column("assigned_by_user_id", sa.Integer(), nullable=True))
            batch.add_column(sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True))
            batch.create_foreign_key(
                "fk_documents_assignee_user_id_users", "users", ["assignee_user_id"], ["id"], ondelete="RESTRICT"
            )
            batch.create_foreign_key(
                "fk_documents_assigned_by_user_id_users", "users", ["assigned_by_user_id"], ["id"], ondelete="RESTRICT"
            )
        with op.batch_alter_table("document_audit_events", recreate="always") as batch:
            batch.drop_constraint(
                sa.schema.conv("ck_document_audit_events_document_audit_event_type_valid"),
                type_="check",
            )
            batch.create_check_constraint("document_audit_event_type_valid", _NEW_AUDIT_CHECK)
    else:
        op.add_column("documents", sa.Column("assignee_user_id", sa.Integer(), nullable=True))
        op.add_column("documents", sa.Column("assigned_by_user_id", sa.Integer(), nullable=True))
        op.add_column("documents", sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True))
        op.create_foreign_key(
            "fk_documents_assignee_user_id_users", "documents", "users",
            ["assignee_user_id"], ["id"], ondelete="RESTRICT",
        )
        op.create_foreign_key(
            "fk_documents_assigned_by_user_id_users", "documents", "users",
            ["assigned_by_user_id"], ["id"], ondelete="RESTRICT",
        )
        op.drop_constraint(
            sa.schema.conv("ck_document_audit_events_document_audit_event_type_valid"),
            "document_audit_events",
            type_="check",
        )
        op.create_check_constraint(
            "document_audit_event_type_valid", "document_audit_events", _NEW_AUDIT_CHECK
        )
    op.create_index(
        "ix_documents_workspace_assignee", "documents", ["workspace_id", "assignee_user_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_documents_workspace_assignee", table_name="documents")
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("document_audit_events", recreate="always") as batch:
            batch.drop_constraint(
                sa.schema.conv("ck_document_audit_events_document_audit_event_type_valid"),
                type_="check",
            )
            batch.create_check_constraint("document_audit_event_type_valid", _OLD_AUDIT_CHECK)
        with op.batch_alter_table("documents", recreate="always") as batch:
            batch.drop_constraint("fk_documents_assigned_by_user_id_users", type_="foreignkey")
            batch.drop_constraint("fk_documents_assignee_user_id_users", type_="foreignkey")
            batch.drop_column("assigned_at")
            batch.drop_column("assigned_by_user_id")
            batch.drop_column("assignee_user_id")
    else:
        op.drop_constraint("fk_documents_assigned_by_user_id_users", "documents", type_="foreignkey")
        op.drop_constraint("fk_documents_assignee_user_id_users", "documents", type_="foreignkey")
        op.drop_column("documents", "assigned_at")
        op.drop_column("documents", "assigned_by_user_id")
        op.drop_column("documents", "assignee_user_id")
        op.drop_constraint(
            sa.schema.conv("ck_document_audit_events_document_audit_event_type_valid"),
            "document_audit_events",
            type_="check",
        )
        op.create_check_constraint(
            "document_audit_event_type_valid", "document_audit_events", _OLD_AUDIT_CHECK
        )
