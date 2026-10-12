"""Adiciona usuários e isolamento por workspace preservando registros existentes.

Revision ID: 20261013_0002
Revises: 20261012_0001
Create Date: 2026-10-13
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261013_0002"
down_revision: Union[str, None] = "20261012_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=512), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )
    op.create_table(
        "workspaces",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspaces")),
        sa.UniqueConstraint("slug", name=op.f("uq_workspaces_slug")),
    )
    op.create_table(
        "workspace_memberships",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.CheckConstraint(
            "role IN ('operator', 'approver')",
            name=op.f("ck_workspace_memberships_valid_workspace_role"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_workspace_memberships_user_id_users"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_workspace_memberships_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "user_id", "workspace_id", "role", name=op.f("pk_workspace_memberships")
        ),
    )

    workspace_table = sa.table(
        "workspaces",
        sa.column("id", sa.Integer()),
        sa.column("slug", sa.String()),
        sa.column("name", sa.String()),
    )
    bind = op.get_bind()
    bind.execute(
        workspace_table.insert().values(slug="demo", name="Workspace de demonstração")
    )
    demo_workspace_id = bind.scalar(
        sa.select(workspace_table.c.id).where(workspace_table.c.slug == "demo")
    )

    op.add_column("suppliers", sa.Column("workspace_id", sa.Integer(), nullable=True))
    op.add_column("purchase_orders", sa.Column("workspace_id", sa.Integer(), nullable=True))
    bind.execute(
        sa.text("UPDATE suppliers SET workspace_id = :workspace_id"),
        {"workspace_id": demo_workspace_id},
    )
    bind.execute(
        sa.text("UPDATE purchase_orders SET workspace_id = :workspace_id"),
        {"workspace_id": demo_workspace_id},
    )

    if bind.dialect.name == "sqlite":
        _upgrade_sqlite_constraints()
    else:
        _upgrade_server_constraints()


def _upgrade_server_constraints() -> None:
    op.drop_constraint("uq_suppliers_identifier", "suppliers", type_="unique")
    op.drop_constraint("fk_purchase_orders_supplier_id_suppliers", "purchase_orders", type_="foreignkey")
    op.drop_constraint("uq_purchase_orders_reference", "purchase_orders", type_="unique")
    op.alter_column("suppliers", "workspace_id", existing_type=sa.Integer(), nullable=False)
    op.alter_column("purchase_orders", "workspace_id", existing_type=sa.Integer(), nullable=False)

    op.create_unique_constraint(
        "uq_suppliers_workspace_identifier", "suppliers", ["workspace_id", "identifier"]
    )
    op.create_unique_constraint(
        "uq_suppliers_id_workspace", "suppliers", ["id", "workspace_id"]
    )
    op.create_foreign_key(
        "fk_suppliers_workspace_id_workspaces",
        "suppliers",
        "workspaces",
        ["workspace_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_purchase_orders_workspace_reference",
        "purchase_orders",
        ["workspace_id", "reference"],
    )
    op.create_foreign_key(
        "fk_purchase_orders_workspace_id_workspaces",
        "purchase_orders",
        "workspaces",
        ["workspace_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_purchase_orders_supplier_workspace",
        "purchase_orders",
        "suppliers",
        ["supplier_id", "workspace_id"],
        ["id", "workspace_id"],
        ondelete="RESTRICT",
    )


def _upgrade_sqlite_constraints() -> None:
    with op.batch_alter_table("suppliers", recreate="always") as batch:
        batch.drop_constraint("uq_suppliers_identifier", type_="unique")
        batch.alter_column("workspace_id", existing_type=sa.Integer(), nullable=False)
        batch.create_unique_constraint(
            "uq_suppliers_workspace_identifier", ["workspace_id", "identifier"]
        )
        batch.create_unique_constraint("uq_suppliers_id_workspace", ["id", "workspace_id"])
        batch.create_foreign_key(
            "fk_suppliers_workspace_id_workspaces", "workspaces", ["workspace_id"], ["id"], ondelete="RESTRICT"
        )

    with op.batch_alter_table("purchase_orders", recreate="always") as batch:
        batch.drop_constraint("uq_purchase_orders_reference", type_="unique")
        batch.drop_constraint("fk_purchase_orders_supplier_id_suppliers", type_="foreignkey")
        batch.alter_column("workspace_id", existing_type=sa.Integer(), nullable=False)
        batch.create_unique_constraint(
            "uq_purchase_orders_workspace_reference", ["workspace_id", "reference"]
        )
        batch.create_foreign_key(
            "fk_purchase_orders_workspace_id_workspaces",
            "workspaces",
            ["workspace_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_purchase_orders_supplier_workspace",
            "suppliers",
            ["supplier_id", "workspace_id"],
            ["id", "workspace_id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    raise NotImplementedError(
        "Esta revisão não pode ser revertida automaticamente após existirem workspaces e associações."
    )
