"""Persistência SQLAlchemy de documentos, sempre limitada ao workspace."""

from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.app.models.document import (
    DocumentApprovalEntity,
    DocumentAuditEventEntity,
    DocumentEntity,
    DocumentReviewRevisionEntity,
)
from backend.app.services.document_processing import DocumentProcessingResult


class DocumentRepository(Protocol):
    def create(
        self,
        *,
        workspace_id: int,
        purchase_order_id: int,
        original_filename: str,
        size_bytes: int,
        content_type: str,
        sha256: str,
        storage_key: str,
    ) -> tuple[DocumentEntity, bool]: ...
    def get_by_id(self, document_id: int, workspace_id: int) -> DocumentEntity | None: ...
    def find_by_hash(self, sha256: str, workspace_id: int) -> DocumentEntity | None: ...
    def list_for_workspace(
        self, workspace_id: int, *, limit: int, offset: int
    ) -> list[DocumentEntity]: ...
    def save_result(
        self,
        *,
        document_id: int,
        workspace_id: int,
        result: DocumentProcessingResult,
        page_texts: list[dict[str, int | str]],
        job_id: str | None = None,
    ) -> None: ...
    def mark_failed(
        self, *, document_id: int, workspace_id: int, safe_message: str
    ) -> None: ...
    def mark_enqueue_failed(
        self, *, document_id: int, workspace_id: int, job_id: str, safe_message: str
    ) -> None: ...
    def set_job_id(self, *, document_id: int, workspace_id: int, job_id: str) -> bool: ...
    def requeue_failed(self, *, document_id: int, workspace_id: int, job_id: str) -> bool: ...
    def claim_for_processing(
        self, *, document_id: int, job_id: str, max_attempts: int
    ) -> DocumentEntity | None: ...
    def record_attempt_error(
        self, *, document_id: int, job_id: str, safe_message: str
    ) -> None: ...
    def list_stale_processing(self, *, cutoff: datetime) -> list[DocumentEntity]: ...
    def requeue_stale(
        self, *, document_id: int, cutoff: datetime, job_id: str
    ) -> bool: ...
    def fail_stale(
        self, *, document_id: int, job_id: str, cutoff: datetime, safe_message: str
    ) -> bool: ...
    def save_correction(
        self,
        *,
        document_id: int,
        workspace_id: int,
        expected_version: int,
        reviewer_user_id: int,
        reviewed_document: dict[str, Any],
        validation_result: dict[str, Any],
        justification: str,
    ) -> DocumentEntity: ...
    def save_approval(
        self,
        *,
        document_id: int,
        workspace_id: int,
        expected_version: int,
        approver_user_id: int,
        approved_document: dict[str, Any],
        validation_result: dict[str, Any],
    ) -> DocumentEntity: ...
    def save_rejection(
        self,
        *,
        document_id: int,
        workspace_id: int,
        expected_version: int,
        approver_user_id: int,
        justification: str,
    ) -> DocumentEntity: ...
    def current_approval(
        self, document_id: int, workspace_id: int, version: int
    ) -> DocumentApprovalEntity | None: ...
    def audit_events(self, document_id: int, workspace_id: int) -> list[DocumentAuditEventEntity]: ...


class SqlAlchemyDocumentRepository:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def create(
        self,
        *,
        workspace_id: int,
        purchase_order_id: int,
        original_filename: str,
        size_bytes: int,
        content_type: str,
        sha256: str,
        storage_key: str,
    ) -> tuple[DocumentEntity, bool]:
        try:
            with self._session_factory.begin() as session:
                document = DocumentEntity(
                    workspace_id=workspace_id,
                    purchase_order_id=purchase_order_id,
                    original_filename=original_filename,
                    size_bytes=size_bytes,
                    content_type=content_type,
                    sha256=sha256,
                    storage_key=storage_key,
                    processing_status="queued",
                    review_status="pending",
                )
                session.add(document)
                session.flush()
                document_id = document.id
        except IntegrityError:
            existing = self.find_by_hash(sha256, workspace_id)
            if existing is None:
                raise
            return existing, True

        created = self.get_by_id(document_id, workspace_id)
        if created is None:
            raise RuntimeError("O documento recém-criado não foi encontrado.")
        return created, False

    def get_by_id(self, document_id: int, workspace_id: int) -> DocumentEntity | None:
        with self._session_factory() as session:
            return session.scalar(
                select(DocumentEntity).where(
                    DocumentEntity.id == document_id,
                    DocumentEntity.workspace_id == workspace_id,
                )
            )

    def find_by_hash(self, sha256: str, workspace_id: int) -> DocumentEntity | None:
        with self._session_factory() as session:
            return session.scalar(
                select(DocumentEntity).where(
                    DocumentEntity.sha256 == sha256,
                    DocumentEntity.workspace_id == workspace_id,
                )
            )

    def list_for_workspace(
        self, workspace_id: int, *, limit: int, offset: int
    ) -> list[DocumentEntity]:
        with self._session_factory() as session:
            return list(
                session.scalars(
                    select(DocumentEntity)
                    .where(DocumentEntity.workspace_id == workspace_id)
                    .order_by(DocumentEntity.uploaded_at.desc(), DocumentEntity.id.desc())
                    .limit(limit)
                    .offset(offset)
                ).all()
            )

    def save_result(
        self,
        *,
        document_id: int,
        workspace_id: int,
        result: DocumentProcessingResult,
        page_texts: list[dict[str, int | str]],
        job_id: str | None = None,
    ) -> None:
        with self._session_factory.begin() as session:
            document = session.scalar(
                select(DocumentEntity)
                .where(
                    DocumentEntity.id == document_id,
                    DocumentEntity.workspace_id == workspace_id,
                    DocumentEntity.processing_status.in_(("queued", "processing")),
                    DocumentEntity.review_status != "approved",
                    *([DocumentEntity.active_job_id == job_id] if job_id else []),
                )
                .with_for_update()
            )
            if document is None:
                raise RuntimeError("O documento não está elegível para salvar o resultado.")
            document.page_texts = page_texts
            document.extracted_document = result.document.model_dump(mode="json")
            document.pdf_warnings = list(result.pdf_warnings)
            document.validation_result = result.validation.model_dump(mode="json")
            document.final_status = result.status
            document.processing_status = "ready_for_review"
            document.processing_error = None
            document.processing_started_at = None

    def mark_failed(self, *, document_id: int, workspace_id: int, safe_message: str) -> None:
        with self._session_factory.begin() as session:
            document = session.scalar(
                select(DocumentEntity).where(
                    DocumentEntity.id == document_id,
                    DocumentEntity.workspace_id == workspace_id,
                )
            )
            if document is None:
                raise RuntimeError("O documento não foi encontrado para registrar a falha.")
            if document.processing_status == "ready_for_review" or document.review_status == "approved":
                return
            document.processing_status = "failed"
            document.processing_error = safe_message

    def mark_enqueue_failed(
        self, *, document_id: int, workspace_id: int, job_id: str, safe_message: str
    ) -> None:
        with self._session_factory.begin() as session:
            document = session.scalar(
                select(DocumentEntity)
                .where(
                    DocumentEntity.id == document_id,
                    DocumentEntity.workspace_id == workspace_id,
                    DocumentEntity.active_job_id == job_id,
                    DocumentEntity.processing_status == "queued",
                    DocumentEntity.review_status != "approved",
                )
                .with_for_update()
            )
            if document is not None:
                document.processing_status = "failed"
                document.processing_error = document.processing_error or safe_message

    def set_job_id(self, *, document_id: int, workspace_id: int, job_id: str) -> bool:
        with self._session_factory.begin() as session:
            document = session.scalar(
                select(DocumentEntity)
                .where(
                    DocumentEntity.id == document_id,
                    DocumentEntity.workspace_id == workspace_id,
                )
                .with_for_update()
            )
            if document is None or document.processing_status != "queued":
                return False
            document.active_job_id = job_id
            return True

    def requeue_failed(self, *, document_id: int, workspace_id: int, job_id: str) -> bool:
        with self._session_factory.begin() as session:
            document = session.scalar(
                select(DocumentEntity)
                .where(
                    DocumentEntity.id == document_id,
                    DocumentEntity.workspace_id == workspace_id,
                )
                .with_for_update()
            )
            if (
                document is None
                or document.processing_status != "failed"
                or document.review_status == "approved"
            ):
                return False
            document.processing_status = "queued"
            document.processing_attempts = 0
            document.processing_started_at = None
            document.active_job_id = job_id
            document.processing_error = None
            return True

    def claim_for_processing(
        self, *, document_id: int, job_id: str, max_attempts: int
    ) -> DocumentEntity | None:
        with self._session_factory.begin() as session:
            document = session.scalar(
                select(DocumentEntity)
                .where(DocumentEntity.id == document_id)
                .with_for_update()
            )
            if (
                document is None
                or document.active_job_id != job_id
                or document.review_status == "approved"
                or document.processing_status not in {"queued", "processing"}
                or document.processing_attempts >= max_attempts
            ):
                return None
            document.processing_status = "processing"
            document.processing_attempts += 1
            document.processing_started_at = datetime.now(timezone.utc)
            return document

    def record_attempt_error(
        self, *, document_id: int, job_id: str, safe_message: str
    ) -> None:
        with self._session_factory.begin() as session:
            document = session.scalar(
                select(DocumentEntity)
                .where(
                    DocumentEntity.id == document_id,
                    DocumentEntity.active_job_id == job_id,
                    DocumentEntity.processing_status == "processing",
                    DocumentEntity.review_status != "approved",
                )
                .with_for_update()
            )
            if document is not None:
                document.processing_error = safe_message

    def fail_job(
        self, *, document_id: int, job_id: str, safe_message: str
    ) -> None:
        with self._session_factory.begin() as session:
            document = session.scalar(
                select(DocumentEntity)
                .where(
                    DocumentEntity.id == document_id,
                    DocumentEntity.active_job_id == job_id,
                    DocumentEntity.processing_status == "processing",
                    DocumentEntity.review_status != "approved",
                )
                .with_for_update()
            )
            if document is not None:
                document.processing_status = "failed"
                document.processing_error = document.processing_error or safe_message

    def fail_stale(
        self, *, document_id: int, job_id: str, cutoff: datetime, safe_message: str
    ) -> bool:
        with self._session_factory.begin() as session:
            document = session.scalar(
                select(DocumentEntity)
                .where(DocumentEntity.id == document_id)
                .with_for_update()
            )
            if (
                document is None
                or document.active_job_id != job_id
                or document.processing_status != "processing"
                or document.review_status == "approved"
                or (
                    document.processing_started_at is not None
                    and document.processing_started_at
                    > _compatible_datetime(cutoff, document.processing_started_at)
                )
            ):
                return False
            document.processing_status = "failed"
            document.processing_error = safe_message
            return True

    def list_stale_processing(self, *, cutoff: datetime) -> list[DocumentEntity]:
        with self._session_factory() as session:
            return list(
                session.scalars(
                    select(DocumentEntity).where(
                        DocumentEntity.processing_status == "processing",
                        DocumentEntity.review_status != "approved",
                        or_(
                            DocumentEntity.processing_started_at.is_(None),
                            DocumentEntity.processing_started_at <= cutoff,
                        ),
                    )
                ).all()
            )

    def requeue_stale(
        self, *, document_id: int, cutoff: datetime, job_id: str
    ) -> bool:
        with self._session_factory.begin() as session:
            document = session.scalar(
                select(DocumentEntity)
                .where(DocumentEntity.id == document_id)
                .with_for_update()
            )
            if (
                document is None
                or document.processing_status != "processing"
                or document.review_status == "approved"
                or (
                    document.processing_started_at is not None
                    and document.processing_started_at
                    > _compatible_datetime(cutoff, document.processing_started_at)
                )
            ):
                return False
            document.processing_status = "queued"
            document.processing_attempts = 0
            document.processing_started_at = None
            document.active_job_id = job_id
            document.processing_error = "Execução interrompida; o documento foi recolocado na fila."
            return True

    def save_correction(
        self,
        *,
        document_id: int,
        workspace_id: int,
        expected_version: int,
        reviewer_user_id: int,
        reviewed_document: dict[str, Any],
        validation_result: dict[str, Any],
        justification: str,
    ) -> DocumentEntity:
        from backend.app.services.document_review import (
            DocumentReviewNotFound,
            DocumentNotReviewable,
        )

        with self._session_factory.begin() as session:
            document = session.scalar(
                select(DocumentEntity)
                .where(
                    DocumentEntity.id == document_id,
                    DocumentEntity.workspace_id == workspace_id,
                )
                .with_for_update()
            )
            if document is None:
                raise DocumentReviewNotFound
            _require_current_version(document, expected_version)
            if document.processing_status != "ready_for_review" or document.extracted_document is None:
                raise DocumentNotReviewable

            next_version = document.review_version + 1
            document.review_version = next_version
            document.reviewed_document = reviewed_document
            document.validation_result = validation_result
            document.final_status = validation_result["status"]
            document.review_status = "pending"
            session.add(
                DocumentReviewRevisionEntity(
                    document_id=document_id,
                    workspace_id=workspace_id,
                    version=next_version,
                    reviewed_document=reviewed_document,
                    validation_result=validation_result,
                    reviewer_user_id=reviewer_user_id,
                    justification=justification,
                )
            )
            for event_type in ("review", "correction"):
                session.add(
                    DocumentAuditEventEntity(
                        document_id=document_id,
                        workspace_id=workspace_id,
                        event_type=event_type,
                        version=next_version,
                        actor_user_id=reviewer_user_id,
                        justification=justification,
                        details={"validation_status": validation_result["status"]},
                    )
                )
            session.flush()

        record = self.get_by_id(document_id, workspace_id)
        if record is None:
            raise DocumentReviewNotFound
        return record

    def save_approval(
        self,
        *,
        document_id: int,
        workspace_id: int,
        expected_version: int,
        approver_user_id: int,
        approved_document: dict[str, Any],
        validation_result: dict[str, Any],
    ) -> DocumentEntity:
        from backend.app.services.document_review import (
            DocumentReviewNotFound,
            DocumentNotReviewable,
            ReviewActionConflict,
        )

        with self._session_factory.begin() as session:
            document = session.scalar(
                select(DocumentEntity)
                .where(
                    DocumentEntity.id == document_id,
                    DocumentEntity.workspace_id == workspace_id,
                )
                .with_for_update()
            )
            if document is None:
                raise DocumentReviewNotFound
            _require_current_version(document, expected_version)
            if document.processing_status != "ready_for_review" or document.extracted_document is None:
                raise DocumentNotReviewable
            if document.review_status != "pending":
                raise ReviewActionConflict

            document.validation_result = validation_result
            document.final_status = validation_result["status"]
            document.review_status = "approved"
            session.add(
                DocumentApprovalEntity(
                    document_id=document_id,
                    workspace_id=workspace_id,
                    reviewed_version=document.review_version,
                    approved_document=approved_document,
                    validation_result=validation_result,
                    approver_user_id=approver_user_id,
                )
            )
            session.add(
                DocumentAuditEventEntity(
                    document_id=document_id,
                    workspace_id=workspace_id,
                    event_type="approval",
                    version=document.review_version,
                    actor_user_id=approver_user_id,
                    details={"validation_status": validation_result["status"]},
                )
            )
            session.flush()

        record = self.get_by_id(document_id, workspace_id)
        if record is None:
            raise DocumentReviewNotFound
        return record

    def save_rejection(
        self,
        *,
        document_id: int,
        workspace_id: int,
        expected_version: int,
        approver_user_id: int,
        justification: str,
    ) -> DocumentEntity:
        from backend.app.services.document_review import (
            DocumentReviewNotFound,
            DocumentNotReviewable,
            ReviewActionConflict,
        )

        with self._session_factory.begin() as session:
            document = session.scalar(
                select(DocumentEntity)
                .where(
                    DocumentEntity.id == document_id,
                    DocumentEntity.workspace_id == workspace_id,
                )
                .with_for_update()
            )
            if document is None:
                raise DocumentReviewNotFound
            _require_current_version(document, expected_version)
            if document.processing_status != "ready_for_review" or document.extracted_document is None:
                raise DocumentNotReviewable
            if document.review_status != "pending":
                raise ReviewActionConflict

            document.review_status = "rejected"
            session.add(
                DocumentAuditEventEntity(
                    document_id=document_id,
                    workspace_id=workspace_id,
                    event_type="rejection",
                    version=document.review_version,
                    actor_user_id=approver_user_id,
                    justification=justification,
                )
            )
            session.flush()

        record = self.get_by_id(document_id, workspace_id)
        if record is None:
            raise DocumentReviewNotFound
        return record

    def current_approval(
        self, document_id: int, workspace_id: int, version: int
    ) -> DocumentApprovalEntity | None:
        with self._session_factory() as session:
            return session.scalar(
                select(DocumentApprovalEntity).where(
                    DocumentApprovalEntity.document_id == document_id,
                    DocumentApprovalEntity.workspace_id == workspace_id,
                    DocumentApprovalEntity.reviewed_version == version,
                )
            )

    def audit_events(
        self, document_id: int, workspace_id: int
    ) -> list[DocumentAuditEventEntity]:
        with self._session_factory() as session:
            return list(
                session.scalars(
                    select(DocumentAuditEventEntity)
                    .where(
                        DocumentAuditEventEntity.document_id == document_id,
                        DocumentAuditEventEntity.workspace_id == workspace_id,
                    )
                    .order_by(DocumentAuditEventEntity.created_at, DocumentAuditEventEntity.id)
                ).all()
            )


def _require_current_version(document: DocumentEntity, expected_version: int) -> None:
    from backend.app.services.document_review import ReviewVersionConflict

    if document.review_version != expected_version:
        raise ReviewVersionConflict


def _compatible_datetime(value: datetime, reference: datetime) -> datetime:
    if reference.tzinfo is None and value.tzinfo is not None:
        return value.replace(tzinfo=None)
    if reference.tzinfo is not None and value.tzinfo is None:
        return value.replace(tzinfo=reference.tzinfo)
    return value

