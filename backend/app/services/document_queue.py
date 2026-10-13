"""Contrato de fila e adaptador RQ; os argumentos do job contêm apenas o ID do documento."""

import os
from dataclasses import dataclass
from typing import Protocol

from redis import Redis
from rq import Callback, Queue, Retry


class DocumentQueue(Protocol):
    def enqueue(self, document_id: int, *, job_id: str) -> None: ...


@dataclass(frozen=True)
class DocumentQueueConfig:
    redis_url: str
    queue_name: str = "documents"
    job_timeout_seconds: int = 300
    max_attempts: int = 3
    stale_after_seconds: int = 900

    def __post_init__(self) -> None:
        if not self.redis_url:
            raise ValueError("Configure REDIS_URL para iniciar o processamento assíncrono.")
        if self.job_timeout_seconds <= 0 or self.max_attempts <= 0 or self.stale_after_seconds <= 0:
            raise ValueError("Timeout e número de tentativas devem ser positivos.")

    @classmethod
    def from_environment(cls) -> "DocumentQueueConfig":
        return cls(
            redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            queue_name=os.getenv("DOCUFLOW_QUEUE_NAME", "documents"),
            job_timeout_seconds=int(os.getenv("DOCUFLOW_JOB_TIMEOUT_SECONDS", "300")),
            max_attempts=int(os.getenv("DOCUFLOW_MAX_ATTEMPTS", "3")),
            stale_after_seconds=int(os.getenv("DOCUFLOW_STALE_AFTER_SECONDS", "900")),
        )


class RQDocumentQueue:
    def __init__(self, config: DocumentQueueConfig, *, connection=None) -> None:
        self.config = config
        self.connection = connection or Redis.from_url(config.redis_url)
        self.queue = Queue(config.queue_name, connection=self.connection)

    def enqueue(self, document_id: int, *, job_id: str) -> None:
        retry = Retry(max=self.config.max_attempts - 1) if self.config.max_attempts > 1 else None
        options = {
            "job_id": job_id,
            "job_timeout": self.config.job_timeout_seconds,
            "on_failure": Callback("backend.app.worker.mark_document_job_failed"),
        }
        if retry is not None:
            options["retry"] = retry
        self.queue.enqueue("backend.app.worker.process_document_job", document_id, **options)
