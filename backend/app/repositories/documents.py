"""Persistência SQLAlchemy de documentos, sempre limitada ao workspace."""

from typing import Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.app.models.document import DocumentEntity
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
    ) -> None: ...
    def mark_failed(
        self, *, document_id: int, workspace_id: int, safe_message: str
    ) -> None: ...


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
                    processing_status="processing",
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
    ) -> None:
        with self._session_factory.begin() as session:
            document = session.scalar(
                select(DocumentEntity).where(
                    DocumentEntity.id == document_id,
                    DocumentEntity.workspace_id == workspace_id,
                )
            )
            if document is None:
                raise RuntimeError("O documento não foi encontrado para atualizar o resultado.")
            document.page_texts = page_texts
            document.extracted_document = result.document.model_dump(mode="json")
            document.pdf_warnings = list(result.pdf_warnings)
            document.validation_result = result.validation.model_dump(mode="json")
            document.final_status = result.status
            document.processing_status = "succeeded"
            document.processing_error = None

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
            document.processing_status = "failed"
            document.processing_error = safe_message

