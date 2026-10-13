"""Recoloca jobs órfãos na fila depois que o processo worker caiu."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from backend.app.repositories.documents import DocumentRepository
from backend.app.services.document_queue import DocumentQueue, DocumentQueueConfig


class DocumentRecoveryService:
    def __init__(
        self,
        repository: DocumentRepository,
        queue: DocumentQueue,
        config: DocumentQueueConfig,
    ) -> None:
        self.repository = repository
        self.queue = queue
        self.config = config

    def recover(self, *, now: datetime | None = None) -> dict[str, int]:
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=self.config.stale_after_seconds)
        counts = {"requeued": 0, "failed": 0, "skipped": 0}
        for record in self.repository.list_stale_processing(cutoff=cutoff):
            if record.processing_attempts >= self.config.max_attempts:
                failed = self.repository.fail_stale(
                    document_id=record.id,
                    job_id=record.active_job_id or "",
                    cutoff=cutoff,
                    safe_message="O processamento foi interrompido após esgotar as tentativas configuradas.",
                )
                counts["failed" if failed else "skipped"] += 1
                continue

            job_id = uuid4().hex
            if not self.repository.requeue_stale(
                document_id=record.id, cutoff=cutoff, job_id=job_id
            ):
                counts["skipped"] += 1
                continue
            try:
                self.queue.enqueue(record.id, job_id=job_id)
            except Exception:
                self.repository.mark_enqueue_failed(
                    document_id=record.id,
                    workspace_id=record.workspace_id,
                    job_id=job_id,
                    safe_message="A recuperação não conseguiu iniciar o processamento; tente novamente.",
                )
                counts["failed"] += 1
            else:
                counts["requeued"] += 1
        return counts
