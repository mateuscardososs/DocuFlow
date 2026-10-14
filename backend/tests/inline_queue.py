"""Adaptador síncrono exclusivo dos testes legados, sem iniciar Redis."""

from backend.app.services.document_queue import DocumentQueueConfig
from backend.app.services.document_worker import DocumentWorkerService


class InlineDocumentQueue:
    def __init__(self) -> None:
        self.app = None

    def enqueue(self, document_id: int, *, job_id: str) -> None:
        app = self.app
        worker = DocumentWorkerService(
            app.state.document_repository,
            app.state.order_repository,
            app.state.document_storage,
            app.state.document_processing_service,
            DocumentQueueConfig("redis://test-only", max_attempts=1),
            commercial_policy_repository=app.state.commercial_policy_repository,
        )
        try:
            worker.process(document_id=document_id, job_id=job_id)
        except Exception:
            worker.mark_exhausted(document_id=document_id, job_id=job_id)
