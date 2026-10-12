"""Comandos administrativos locais para workspaces e usuários."""

import argparse
from getpass import getpass
import re
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.app.auth import hash_password
from backend.app.db import create_database
from backend.app.models.identity import User, Workspace, WorkspaceMembership


VALID_ROLES = {"operator", "approver"}
WORKSPACE_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


def create_workspace(session_factory: sessionmaker, *, slug: str, name: str) -> int:
    normalized_slug = slug.strip().lower()
    normalized_name = name.strip()
    if not WORKSPACE_SLUG.fullmatch(normalized_slug) or not normalized_name:
        raise ValueError("Informe um slug simples e um nome de workspace.")

    try:
        with session_factory.begin() as session:
            workspace = Workspace(slug=normalized_slug, name=normalized_name)
            session.add(workspace)
            session.flush()
            workspace_id = workspace.id
    except IntegrityError as error:
        raise ValueError("Já existe um workspace com esse slug.") from error
    return workspace_id


def create_user(
    session_factory: sessionmaker,
    *,
    email: str,
    workspace_slug: str,
    roles: Iterable[str],
    password: str,
) -> int:
    normalized_email = email.strip().lower()
    selected_roles = set(roles)
    if "@" not in normalized_email or len(normalized_email) > 255:
        raise ValueError("Informe um e-mail válido.")
    if not selected_roles or not selected_roles.issubset(VALID_ROLES):
        raise ValueError("Selecione ao menos um papel válido: operator ou approver.")
    if len(password) < 12:
        raise ValueError("A senha precisa ter pelo menos 12 caracteres.")

    encoded_password = hash_password(password)
    with session_factory.begin() as session:
        workspace = session.scalar(
            select(Workspace).where(Workspace.slug == workspace_slug.strip().lower())
        )
        if workspace is None:
            raise ValueError("Workspace não encontrado. Crie o workspace antes do usuário.")
        if session.scalar(select(User.id).where(User.email == normalized_email)) is not None:
            raise ValueError("Já existe um usuário com esse e-mail.")

        user = User(email=normalized_email, password_hash=encoded_password)
        session.add(user)
        session.flush()
        session.add_all(
            WorkspaceMembership(user_id=user.id, workspace_id=workspace.id, role=role)
            for role in sorted(selected_roles)
        )
        user_id = user.id
    return user_id


def add_membership(
    session_factory: sessionmaker,
    *,
    email: str,
    workspace_slug: str,
    roles: Iterable[str],
) -> None:
    normalized_email = email.strip().lower()
    selected_roles = set(roles)
    if not selected_roles or not selected_roles.issubset(VALID_ROLES):
        raise ValueError("Selecione ao menos um papel válido: operator ou approver.")

    with session_factory.begin() as session:
        user = session.scalar(select(User).where(User.email == normalized_email))
        workspace = session.scalar(
            select(Workspace).where(Workspace.slug == workspace_slug.strip().lower())
        )
        if user is None:
            raise ValueError("Usuário não encontrado.")
        if workspace is None:
            raise ValueError("Workspace não encontrado.")

        existing_roles = set(
            session.scalars(
                select(WorkspaceMembership.role).where(
                    WorkspaceMembership.user_id == user.id,
                    WorkspaceMembership.workspace_id == workspace.id,
                )
            ).all()
        )
        session.add_all(
            WorkspaceMembership(user_id=user.id, workspace_id=workspace.id, role=role)
            for role in sorted(selected_roles - existing_roles)
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Administração local do DocuFlow")
    commands = parser.add_subparsers(dest="command", required=True)

    workspace = commands.add_parser("create-workspace", help="Cria um workspace")
    workspace.add_argument("--slug", required=True)
    workspace.add_argument("--name", required=True)

    user = commands.add_parser("create-user", help="Cria usuário e associa papéis")
    user.add_argument("--email", required=True)
    user.add_argument("--workspace", required=True, dest="workspace_slug")
    user.add_argument(
        "--role",
        required=True,
        action="append",
        choices=sorted(VALID_ROLES),
        help="Pode ser repetido para conceder os dois papéis.",
    )

    membership = commands.add_parser(
        "add-membership", help="Associa usuário existente a um workspace e concede papéis"
    )
    membership.add_argument("--email", required=True)
    membership.add_argument("--workspace", required=True, dest="workspace_slug")
    membership.add_argument(
        "--role",
        required=True,
        action="append",
        choices=sorted(VALID_ROLES),
        help="Pode ser repetido para conceder os dois papéis.",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    database = create_database()
    try:
        if args.command == "create-workspace":
            workspace_id = create_workspace(
                database.session_factory, slug=args.slug, name=args.name
            )
            print(f"Workspace {args.slug} criado (id {workspace_id}).")
            return

        if args.command == "add-membership":
            add_membership(
                database.session_factory,
                email=args.email,
                workspace_slug=args.workspace_slug,
                roles=args.role,
            )
            print(f"Papéis atualizados para {args.email.strip().lower()} em {args.workspace_slug}.")
            return

        password = getpass("Senha (mínimo 12 caracteres): ")
        confirmation = getpass("Confirme a senha: ")
        if password != confirmation:
            raise ValueError("As senhas não coincidem.")
        user_id = create_user(
            database.session_factory,
            email=args.email,
            workspace_slug=args.workspace_slug,
            roles=args.role,
            password=password,
        )
        print(f"Usuário {args.email.strip().lower()} criado (id {user_id}).")
    except ValueError as error:
        raise SystemExit(str(error)) from None
    finally:
        database.engine.dispose()


if __name__ == "__main__":
    main()
