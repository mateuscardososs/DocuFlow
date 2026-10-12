"""Autenticação por senha Argon2id e tokens JWT de curta duração."""

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from pwdlib import PasswordHash
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from backend.app.models.identity import User, Workspace, WorkspaceMembership


JWT_ALGORITHM = "HS256"
JWT_ISSUER = "docuflow"
_password_hasher = PasswordHash.recommended()
_dummy_password_hash = _password_hasher.hash("constant dummy password")


@dataclass(frozen=True)
class AuthSettings:
    jwt_secret_key: str
    access_token_expire_minutes: int = 30

    def __post_init__(self) -> None:
        if len(self.jwt_secret_key.encode("utf-8")) < 32:
            raise ValueError("JWT_SECRET_KEY deve ter pelo menos 32 bytes.")
        if self.access_token_expire_minutes <= 0:
            raise ValueError("ACCESS_TOKEN_EXPIRE_MINUTES deve ser positivo.")

    @classmethod
    def from_environment(cls) -> "AuthSettings":
        secret_key = os.getenv("JWT_SECRET_KEY")
        if not secret_key:
            raise RuntimeError("Configure JWT_SECRET_KEY antes de iniciar o DocuFlow.")
        try:
            expires = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "30"))
        except ValueError as error:
            raise RuntimeError("ACCESS_TOKEN_EXPIRE_MINUTES precisa ser um número inteiro.") from error
        return cls(jwt_secret_key=secret_key, access_token_expire_minutes=expires)


@dataclass(frozen=True)
class AuthenticatedUser:
    id: int
    email: str


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("A senha não pode ser vazia.")
    return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _password_hasher.verify(password, password_hash)
    except Exception:
        return False


class AuthenticationService:
    def __init__(self, session_factory: sessionmaker, settings: AuthSettings) -> None:
        self._session_factory = session_factory
        self.settings = settings

    def authenticate(self, email: str, password: str) -> AuthenticatedUser | None:
        normalized_email = email.strip().lower()
        with self._session_factory() as session:
            user = session.scalar(
                select(User).where(func.lower(User.email) == normalized_email)
            )
            encoded_hash = user.password_hash if user is not None else _dummy_password_hash
            password_matches = verify_password(password, encoded_hash)
            if user is None or not user.is_active or not password_matches:
                return None
            return AuthenticatedUser(id=user.id, email=user.email)

    def issue_access_token(self, user_id: int) -> tuple[str, int]:
        expires_in = self.settings.access_token_expire_minutes * 60
        now = datetime.now(timezone.utc)
        token = jwt.encode(
            {
                "sub": str(user_id),
                "iss": JWT_ISSUER,
                "iat": now,
                "exp": now + timedelta(seconds=expires_in),
            },
            self.settings.jwt_secret_key,
            algorithm=JWT_ALGORITHM,
        )
        return token, expires_in

    def user_from_token(self, token: str) -> AuthenticatedUser | None:
        try:
            claims = jwt.decode(
                token,
                self.settings.jwt_secret_key,
                algorithms=[JWT_ALGORITHM],
                issuer=JWT_ISSUER,
                options={"require": ["exp", "iat", "iss", "sub"]},
            )
            user_id = int(claims["sub"])
        except (jwt.PyJWTError, KeyError, TypeError, ValueError):
            return None

        with self._session_factory() as session:
            user = session.get(User, user_id)
            if user is None or not user.is_active:
                return None
            return AuthenticatedUser(id=user.id, email=user.email)

    def profile(self, user_id: int) -> dict | None:
        with self._session_factory() as session:
            user = session.get(User, user_id)
            if user is None or not user.is_active:
                return None
            email = user.email
            rows = session.execute(
                select(Workspace.id, Workspace.slug, Workspace.name, WorkspaceMembership.role)
                .join(WorkspaceMembership, WorkspaceMembership.workspace_id == Workspace.id)
                .where(WorkspaceMembership.user_id == user_id)
                .order_by(Workspace.slug, WorkspaceMembership.role)
            ).all()

        memberships: dict[int, dict] = {}
        for workspace_id, slug, name, role in rows:
            membership = memberships.setdefault(
                workspace_id,
                {"id": workspace_id, "slug": slug, "name": name, "roles": []},
            )
            membership["roles"].append(role)
        return {"id": user_id, "email": email, "workspaces": list(memberships.values())}

    def roles_for(self, user_id: int, workspace_id: int) -> set[str]:
        with self._session_factory() as session:
            return set(
                session.scalars(
                    select(WorkspaceMembership.role).where(
                        WorkspaceMembership.user_id == user_id,
                        WorkspaceMembership.workspace_id == workspace_id,
                    )
                ).all()
            )
