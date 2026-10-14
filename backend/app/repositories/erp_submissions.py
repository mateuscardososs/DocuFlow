"""Persistência transacional das solicitações ERP e de seus eventos de auditoria."""

from copy import deepcopy
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from backend.app.models.document import DocumentApprovalEntity, DocumentAuditEventEntity, DocumentEntity
from backend.app.models.erp_submission import ERPSubmissionEntity


class ERPSubmissionRepository(Protocol):
    def request(
        self,
        *,
        document_id: int,
        workspace_id: int,
        expected_version: int,
        requested_by_user_id: int,
        job_id: str,
    ) -> tuple[ERPSubmissionEntity, bool]: ...
    def get_by_id(self, submission_id: int, workspace_id: int) -> ERPSubmissionEntity | None: ...
    def claim(self, submission_id: int, job_id: str) -> tuple[ERPSubmissionEntity, dict[str, Any]] | None: ...
    def succeed(self, submission_id: int, job_id: str, erp_identifier: str) -> bool: ...
    def fail(self, submission_id: int, job_id: str, safe_message: str) -> bool: ...
    def fail_enqueue(self, submission_id: int, job_id: str, safe_message: str) -> bool: ...


class SqlAlchemyERPSubmissionRepository:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def request(
        self,
        *,
        document_id: int,
        workspace_id: int,
        expected_version: int,
        requested_by_user_id: int,
        job_id: str,
    ) -> tuple[ERPSubmissionEntity, bool]:
        from backend.app.services.erp_submission import (
            ERPSubmissionNotEligible,
            ERPSubmissionNotFound,
        )

        with self._session_factory.begin() as session:
            document = session.scalar(
                select(DocumentEntity)
                .where(
                    DocumentEntity.id == document_id,
                    DocumentEntity.workspace_id == workspace_id,
                )
                .with_for_update(of=DocumentEntity)
            )
            if document is None:
                raise ERPSubmissionNotFound
            if (
                document.review_status != "approved"
                or document.processing_status != "ready_for_review"
                or document.review_version != expected_version
            ):
                raise ERPSubmissionNotEligible
            approval = session.scalar(
                select(DocumentApprovalEntity).where(
                    DocumentApprovalEntity.document_id == document_id,
                    DocumentApprovalEntity.workspace_id == workspace_id,
                    DocumentApprovalEntity.reviewed_version == expected_version,
                )
            )
            if approval is None:
                raise ERPSubmissionNotEligible

            submission = session.scalar(
                select(ERPSubmissionEntity)
                .where(
                    ERPSubmissionEntity.workspace_id == workspace_id,
                    ERPSubmissionEntity.document_id == document_id,
                    ERPSubmissionEntity.approved_version == expected_version,
                )
                .with_for_update()
            )
            enqueue = submission is None or submission.state == "failed"
            if submission is None:
                submission = ERPSubmissionEntity(
                    workspace_id=workspace_id,
                    document_id=document_id,
                    approved_version=expected_version,
                    requested_by_user_id=requested_by_user_id,
                    state="queued",
                    attempts=0,
                    active_job_id=job_id,
                )
                session.add(submission)
                session.flush()
            elif enqueue:
                submission.requested_by_user_id = requested_by_user_id
                submission.state = "queued"
                submission.active_job_id = job_id
                submission.failure_message = None
            if enqueue:
                _audit(
                    session,
                    submission,
                    "erp_submission",
                    requested_by_user_id,
                    {"retry": submission.attempts > 0},
                )
            session.flush()
            session.expunge(submission)
            return submission, enqueue

    def get_by_id(self, submission_id: int, workspace_id: int) -> ERPSubmissionEntity | None:
        with self._session_factory() as session:
            return session.scalar(
                select(ERPSubmissionEntity).where(
                    ERPSubmissionEntity.id == submission_id,
                    ERPSubmissionEntity.workspace_id == workspace_id,
                )
            )

    def claim(
        self, submission_id: int, job_id: str
    ) -> tuple[ERPSubmissionEntity, dict[str, Any]] | None:
        with self._session_factory.begin() as session:
            candidate = session.get(ERPSubmissionEntity, submission_id)
            if candidate is None:
                return None
            document = session.scalar(
                select(DocumentEntity)
                .where(
                    DocumentEntity.id == candidate.document_id,
                    DocumentEntity.workspace_id == candidate.workspace_id,
                )
                .with_for_update(of=DocumentEntity)
            )
            submission = session.scalar(
                select(ERPSubmissionEntity)
                .where(ERPSubmissionEntity.id == submission_id)
                .with_for_update()
            )
            if (
                submission is None
                or submission.state != "queued"
                or submission.active_job_id != job_id
            ):
                return None
            if (
                document is None
                or document.review_status != "approved"
                or document.processing_status != "ready_for_review"
                or document.review_version != submission.approved_version
            ):
                submission.state = "failed"
                submission.failure_message = (
                    "A aprovação mudou antes do envio; solicite o envio após nova aprovação."
                )
                _audit(
                    session,
                    submission,
                    "erp_failure",
                    submission.requested_by_user_id,
                    {"reason": "approval_no_longer_current"},
                )
                return None
            approval = session.scalar(
                select(DocumentApprovalEntity).where(
                    DocumentApprovalEntity.document_id == submission.document_id,
                    DocumentApprovalEntity.workspace_id == submission.workspace_id,
                    DocumentApprovalEntity.reviewed_version == submission.approved_version,
                )
            )
            if approval is None:
                submission.state = "failed"
                submission.failure_message = "O snapshot aprovado não está disponível para envio."
                _audit(
                    session,
                    submission,
                    "erp_failure",
                    submission.requested_by_user_id,
                    {"reason": "approved_snapshot_unavailable"},
                )
                return None
            submission.state = "processing"
            submission.attempts += 1
            _audit(
                session,
                submission,
                "erp_attempt",
                submission.requested_by_user_id,
                {"attempt": submission.attempts},
            )
            session.flush()
            session.expunge(submission)
            return submission, deepcopy(approval.approved_document)

    def succeed(self, submission_id: int, job_id: str, erp_identifier: str) -> bool:
        with self._session_factory.begin() as session:
            submission = _active_submission(session, submission_id, job_id)
            if submission is None:
                return False
            submission.state = "succeeded"
            submission.erp_identifier = erp_identifier
            submission.failure_message = None
            _audit(
                session,
                submission,
                "erp_success",
                submission.requested_by_user_id,
                {"erp_identifier": erp_identifier},
            )
            return True

    def fail(self, submission_id: int, job_id: str, safe_message: str) -> bool:
        with self._session_factory.begin() as session:
            submission = _active_submission(session, submission_id, job_id)
            if submission is None:
                return False
            submission.state = "failed"
            submission.failure_message = safe_message
            _audit(
                session,
                submission,
                "erp_failure",
                submission.requested_by_user_id,
                {"failure_message": safe_message},
            )
            return True

    def fail_enqueue(self, submission_id: int, job_id: str, safe_message: str) -> bool:
        with self._session_factory.begin() as session:
            submission = session.scalar(
                select(ERPSubmissionEntity)
                .where(
                    ERPSubmissionEntity.id == submission_id,
                    ERPSubmissionEntity.active_job_id == job_id,
                    ERPSubmissionEntity.state == "queued",
                )
                .with_for_update()
            )
            if submission is None:
                return False
            submission.state = "failed"
            submission.failure_message = safe_message
            _audit(
                session,
                submission,
                "erp_failure",
                submission.requested_by_user_id,
                {"failure_message": safe_message, "stage": "enqueue"},
            )
            return True


def _active_submission(session, submission_id: int, job_id: str) -> ERPSubmissionEntity | None:
    return session.scalar(
        select(ERPSubmissionEntity)
        .where(
            ERPSubmissionEntity.id == submission_id,
            ERPSubmissionEntity.active_job_id == job_id,
            ERPSubmissionEntity.state.in_({"queued", "processing"}),
        )
        .with_for_update()
    )


def _audit(
    session,
    submission: ERPSubmissionEntity,
    event_type: str,
    actor_user_id: int,
    details: dict[str, Any],
) -> None:
    session.add(
        DocumentAuditEventEntity(
            document_id=submission.document_id,
            workspace_id=submission.workspace_id,
            event_type=event_type,
            version=submission.approved_version,
            actor_user_id=actor_user_id,
            details={"submission_id": submission.id, **details},
        )
    )
