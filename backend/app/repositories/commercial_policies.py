"""Histórico append-only das políticas comerciais por workspace."""

from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.app.commercial_policy import CommercialPolicy, normalize_currency_tolerances
from backend.app.models.commercial_policy import WorkspaceCommercialPolicyEntity
from backend.app.models.identity import Workspace


class PolicyWorkspaceNotFound(Exception):
    pass


class PolicyVersionConflict(Exception):
    pass


class CommercialPolicyRepository(Protocol):
    def current(self, workspace_id: int) -> CommercialPolicy: ...
    def current_record(self, workspace_id: int) -> WorkspaceCommercialPolicyEntity | None: ...
    def history(self, workspace_id: int, *, limit: int = 50) -> list[WorkspaceCommercialPolicyEntity]: ...
    def update(
        self, *, workspace_id: int, expected_version: int, actor_user_id: int,
        currency_tolerances: dict[str, str],
    ) -> WorkspaceCommercialPolicyEntity: ...


class SqlAlchemyCommercialPolicyRepository:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def current(self, workspace_id: int) -> CommercialPolicy:
        record = self.current_record(workspace_id)
        if record is None:
            return CommercialPolicy.defaults()
        return CommercialPolicy(version=record.version, currency_tolerances=record.currency_tolerances)

    def current_record(self, workspace_id: int) -> WorkspaceCommercialPolicyEntity | None:
        with self._session_factory() as session:
            return session.scalar(
                select(WorkspaceCommercialPolicyEntity)
                .where(WorkspaceCommercialPolicyEntity.workspace_id == workspace_id)
                .order_by(WorkspaceCommercialPolicyEntity.version.desc())
                .limit(1)
            )

    def history(
        self, workspace_id: int, *, limit: int = 50
    ) -> list[WorkspaceCommercialPolicyEntity]:
        with self._session_factory() as session:
            return list(session.scalars(
                select(WorkspaceCommercialPolicyEntity)
                .where(WorkspaceCommercialPolicyEntity.workspace_id == workspace_id)
                .order_by(WorkspaceCommercialPolicyEntity.version.desc())
                .limit(limit)
            ).all())

    def update(
        self,
        *,
        workspace_id: int,
        expected_version: int,
        actor_user_id: int,
        currency_tolerances: dict[str, str],
    ) -> WorkspaceCommercialPolicyEntity:
        normalized = normalize_currency_tolerances(currency_tolerances)
        try:
            with self._session_factory.begin() as session:
                workspace = session.scalar(
                    select(Workspace)
                    .where(Workspace.id == workspace_id)
                    .with_for_update()
                )
                if workspace is None:
                    raise PolicyWorkspaceNotFound
                current_version = session.scalar(
                    select(func.max(WorkspaceCommercialPolicyEntity.version)).where(
                        WorkspaceCommercialPolicyEntity.workspace_id == workspace_id
                    )
                ) or 0
                if current_version != expected_version:
                    raise PolicyVersionConflict
                record = WorkspaceCommercialPolicyEntity(
                    workspace_id=workspace_id,
                    version=current_version + 1,
                    currency_tolerances=normalized,
                    created_by_user_id=actor_user_id,
                )
                session.add(record)
                session.flush()
                record_id = record.id
        except IntegrityError:
            # Protects backends without SELECT FOR UPDATE; in PostgreSQL the workspace row
            # lock serializes concurrent first writes and subsequent version increments.
            with self._session_factory() as session:
                current_version = session.scalar(
                    select(func.max(WorkspaceCommercialPolicyEntity.version)).where(
                        WorkspaceCommercialPolicyEntity.workspace_id == workspace_id
                    )
                ) or 0
            if current_version != expected_version:
                raise PolicyVersionConflict from None
            raise
        record = self.get_by_id(record_id, workspace_id)
        if record is None:
            raise RuntimeError("A versão da política recém-criada não foi encontrada.")
        return record

    def get_by_id(self, record_id: int, workspace_id: int) -> WorkspaceCommercialPolicyEntity | None:
        with self._session_factory() as session:
            return session.scalar(
                select(WorkspaceCommercialPolicyEntity).where(
                    WorkspaceCommercialPolicyEntity.id == record_id,
                    WorkspaceCommercialPolicyEntity.workspace_id == workspace_id,
                )
            )
