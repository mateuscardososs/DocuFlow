"""Coordena registro, armazenamento, processamento e persistência de um envio."""

from dataclasses import dataclass
from hashlib import sha256
from pathlib import PurePosixPath

from backend.app.models.document import DocumentEntity
from backend.app.repositories.documents import DocumentRepository
from backend.app.schemas import PurchaseOrder
from backend.app.services.document_processing import DocumentProcessingService
from backend.app.services.pdf_text_extractor import PdfExtractionError
from backend.app.services.structured_extraction import StructuredExtractionError
from backend.app.services.document_storage import DocumentFileStorage


class DocumentAttemptError(Exception):
    def __init__(self, document_id: int, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.document_id = document_id
        self.code = code


@dataclass(frozen=True)
class DocumentAttempt:
    document: DocumentEntity
    duplicate: bool


class DocumentLifecycleService:
    def __init__(
        self,
        repository: DocumentRepository,
        storage: DocumentFileStorage,
        processing_service: DocumentProcessingService,
    ) -> None:
        self.repository = repository
        self.storage = storage
        self.processing_service = processing_service

    @staticmethod
    def digest(content: bytes) -> str:
        return sha256(content).hexdigest()

    def find_duplicate(self, workspace_id: int, content: bytes) -> DocumentEntity | None:
        return self.repository.find_by_hash(self.digest(content), workspace_id)

    def process_upload(
        self,
        *,
        workspace_id: int,
        order_id: int,
        order: PurchaseOrder,
        original_filename: str,
        content_type: str,
        content: bytes,
    ) -> DocumentAttempt:
        digest = self.digest(content)
        key = self.storage.new_key(workspace_id)
        document, duplicate = self.repository.create(
            workspace_id=workspace_id,
            purchase_order_id=order_id,
            original_filename=_safe_original_filename(original_filename),
            size_bytes=len(content),
            content_type=content_type,
            sha256=digest,
            storage_key=key,
        )
        if duplicate:
            return DocumentAttempt(document=document, duplicate=True)

        try:
            self.storage.store(document.storage_key, content)
            result, pages = self.processing_service.process_with_pages(content, order)
            self.repository.save_result(
                document_id=document.id,
                workspace_id=workspace_id,
                result=result,
                page_texts=[{"number": page.number, "text": page.text} for page in pages],
            )
        except PdfExtractionError as error:
            self._mark_failed(document, workspace_id, error.code, str(error))
        except StructuredExtractionError as error:
            self._mark_failed(document, workspace_id, error.code, str(error))
        except Exception:
            self._mark_failed(
                document,
                workspace_id,
                "internal_error",
                "Falha interna ao processar o documento.",
            )

        refreshed = self.repository.get_by_id(document.id, workspace_id)
        if refreshed is None:
            raise RuntimeError("O documento registrado não foi encontrado.")
        return DocumentAttempt(document=refreshed, duplicate=False)

    def _mark_failed(
        self,
        document: DocumentEntity,
        workspace_id: int,
        code: str,
        safe_message: str,
    ) -> None:
        self.repository.mark_failed(
            document_id=document.id,
            workspace_id=workspace_id,
            safe_message=safe_message,
        )
        raise DocumentAttemptError(document.id, code, safe_message) from None


def _safe_original_filename(filename: str) -> str:
    normalized = filename.replace("\\", "/").replace("\r", "").replace("\n", "")
    basename = PurePosixPath(normalized).name.strip()
    basename = "".join(character for character in basename if character.isprintable())
    return (basename or "documento.pdf")[:512]
