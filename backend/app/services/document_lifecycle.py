"""Registra uploads, guarda seus arquivos e coordena o enqueue do processamento."""

from dataclasses import dataclass
from hashlib import sha256
from pathlib import PurePosixPath
from uuid import uuid4

from backend.app.models.document import DocumentEntity
from backend.app.repositories.documents import DocumentRepository
from backend.app.services.document_storage import DocumentFileStorage
from backend.app.services.document_queue import DocumentQueue


class DocumentAttemptError(Exception):
    def __init__(self, document_id: int, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.document_id = document_id
        self.code = code


class DocumentNotFound(Exception):
    pass


class DocumentNotEligible(Exception):
    pass


@dataclass(frozen=True)
class DocumentAttempt:
    document: DocumentEntity
    duplicate: bool


class DocumentLifecycleService:
    def __init__(
        self,
        repository: DocumentRepository,
        storage: DocumentFileStorage,
        document_queue: DocumentQueue | None = None,
    ) -> None:
        self.repository = repository
        self.storage = storage
        self.document_queue = document_queue

    @staticmethod
    def digest(content: bytes) -> str:
        return sha256(content).hexdigest()

    def find_duplicate(self, workspace_id: int, content: bytes) -> DocumentEntity | None:
        return self.repository.find_by_hash(self.digest(content), workspace_id)

    def enqueue_upload(
        self,
        *,
        workspace_id: int,
        order_id: int,
        original_filename: str,
        content_type: str,
        content: bytes,
    ) -> DocumentAttempt:
        if self.document_queue is None:
            raise RuntimeError("A fila de documentos não foi configurada.")
        document, duplicate = self.repository.create(
            workspace_id=workspace_id,
            purchase_order_id=order_id,
            original_filename=_safe_original_filename(original_filename),
            size_bytes=len(content),
            content_type=content_type,
            sha256=self.digest(content),
            storage_key=self.storage.new_key(workspace_id),
        )
        if duplicate:
            return DocumentAttempt(document=document, duplicate=True)
        try:
            self.storage.store(document.storage_key, content)
        except Exception:
            self.repository.mark_failed(
                document_id=document.id,
                workspace_id=workspace_id,
                safe_message="O arquivo foi registrado, mas não pôde ser armazenado.",
            )
            raise DocumentAttemptError(
                document.id, "storage_error", "O arquivo não pôde ser armazenado."
            ) from None

        job_id = uuid4().hex
        if not self.repository.set_job_id(
            document_id=document.id, workspace_id=workspace_id, job_id=job_id
        ):
            raise RuntimeError("O documento não pôde ser preparado para a fila.")
        queued = self.repository.get_by_id(document.id, workspace_id)
        if queued is None:
            raise RuntimeError("O documento registrado não foi encontrado.")
        try:
            self.document_queue.enqueue(document.id, job_id=job_id)
        except Exception:
            self.repository.mark_enqueue_failed(
                document_id=document.id,
                workspace_id=workspace_id,
                job_id=job_id,
                safe_message="O processamento não foi iniciado; Redis ou a fila está indisponível.",
            )
            raise DocumentAttemptError(
                document.id,
                "queue_unavailable",
                "O documento foi salvo, mas o processamento não foi iniciado. Tente novamente.",
            ) from None
        return DocumentAttempt(document=queued, duplicate=False)

    def reprocess(self, *, document_id: int, workspace_id: int) -> DocumentEntity:
        if self.document_queue is None:
            raise RuntimeError("A fila de documentos não foi configurada.")
        current = self.repository.get_by_id(document_id, workspace_id)
        if current is None:
            raise DocumentNotFound
        if current.processing_status != "failed" or current.review_status == "approved":
            raise DocumentNotEligible
        job_id = uuid4().hex
        if not self.repository.requeue_failed(
            document_id=document_id, workspace_id=workspace_id, job_id=job_id
        ):
            raise DocumentNotEligible
        queued = self.repository.get_by_id(document_id, workspace_id)
        if queued is None:
            raise DocumentNotFound
        try:
            self.document_queue.enqueue(document_id, job_id=job_id)
        except Exception:
            self.repository.mark_enqueue_failed(
                document_id=document_id,
                workspace_id=workspace_id,
                job_id=job_id,
                safe_message="O processamento não foi iniciado; Redis ou a fila está indisponível.",
            )
            raise DocumentAttemptError(
                document_id,
                "queue_unavailable",
                "O documento foi preservado, mas o processamento não foi iniciado. Tente novamente.",
            ) from None
        return queued


def _safe_original_filename(filename: str) -> str:
    normalized = filename.replace("\\", "/").replace("\r", "").replace("\n", "")
    basename = PurePosixPath(normalized).name.strip()
    basename = "".join(character for character in basename if character.isprintable())
    return (basename or "documento.pdf")[:512]
