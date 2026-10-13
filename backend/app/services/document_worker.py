"""Executa um job já persistido, com guarda de estado e tentativas no PostgreSQL."""

from backend.app.repositories.documents import DocumentRepository
from backend.app.repositories.purchase_orders import PurchaseOrderRepository
from backend.app.services.document_processing import DocumentProcessingService
from backend.app.services.document_queue import DocumentQueueConfig
from backend.app.services.document_storage import DocumentFileStorage
from backend.app.services.pdf_text_extractor import PdfExtractionError
from backend.app.services.structured_extraction import StructuredExtractionError


class DocumentWorkerService:
    def __init__(
        self,
        document_repository: DocumentRepository,
        order_repository: PurchaseOrderRepository,
        storage: DocumentFileStorage,
        processing_service: DocumentProcessingService,
        config: DocumentQueueConfig | None = None,
    ) -> None:
        self.documents = document_repository
        self.orders = order_repository
        self.storage = storage
        self.processing = processing_service
        self.config = config or DocumentQueueConfig(
            redis_url="redis://unused", max_attempts=3
        )

    def process(self, *, document_id: int, job_id: str) -> str:
        document = self.documents.claim_for_processing(
            document_id=document_id,
            job_id=job_id,
            max_attempts=self.config.max_attempts,
        )
        if document is None:
            return "skipped"

        try:
            order = self.orders.get_by_id(document.purchase_order_id, document.workspace_id)
            if order is None:
                raise RuntimeError("Pedido não disponível")
            pdf_bytes = self.storage.path_for(document.storage_key).read_bytes()
            result, pages = self.processing.process_with_pages(pdf_bytes, order)
            self.documents.save_result(
                document_id=document.id,
                workspace_id=document.workspace_id,
                result=result,
                page_texts=[{"number": page.number, "text": page.text} for page in pages],
                job_id=job_id,
            )
        except Exception as error:
            safe_message = _safe_attempt_message(error)
            try:
                self.documents.record_attempt_error(
                    document_id=document.id,
                    job_id=job_id,
                    safe_message=safe_message,
                )
            except Exception:
                raise DocumentJobError(
                    "Falha temporária ao processar o documento. O sistema tentará novamente."
                ) from None
            raise DocumentJobError(safe_message) from None
        return "ready_for_review"

    def mark_exhausted(self, *, document_id: int, job_id: str) -> None:
        self.documents.fail_job(
            document_id=document_id,
            job_id=job_id,
            safe_message="O processamento falhou após esgotar as tentativas configuradas.",
        )


def _safe_attempt_message(error: Exception) -> str:
    if isinstance(error, PdfExtractionError):
        return str(error)
    if isinstance(error, StructuredExtractionError):
        return str(error)
    return "Falha temporária ao processar o documento. O sistema tentará novamente."


class DocumentJobError(Exception):
    """Erro seguro para que o RQ não retenha detalhes internos do provedor ou do banco."""
