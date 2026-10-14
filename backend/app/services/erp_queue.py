"""Fila RQ independente para solicitações de cadastro no ERP de demonstração."""

import os
from dataclasses import dataclass
from typing import Protocol

from redis import Redis
from rq import Callback, Queue


class ERPSubmissionQueue(Protocol):
    def enqueue(self, submission_id: int, *, job_id: str) -> None: ...


@dataclass(frozen=True)
class ERPQueueConfig:
    redis_url: str
    queue_name: str = "erp-submissions"
    job_timeout_seconds: int = 60

    def __post_init__(self) -> None:
        if not self.redis_url or not self.queue_name or self.job_timeout_seconds <= 0:
            raise ValueError("Configuração da fila ERP inválida.")

    @classmethod
    def from_environment(cls) -> "ERPQueueConfig":
        return cls(
            redis_url=os.getenv("ERP_REDIS_URL") or os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            queue_name=os.getenv("DOCUFLOW_ERP_QUEUE_NAME", "erp-submissions"),
            job_timeout_seconds=int(os.getenv("DOCUFLOW_ERP_JOB_TIMEOUT_SECONDS", "60")),
        )


class RQERPSubmissionQueue:
    def __init__(self, config: ERPQueueConfig, *, connection=None) -> None:
        self.config = config
        self.connection = connection or Redis.from_url(config.redis_url)
        self.queue = Queue(config.queue_name, connection=self.connection)

    def enqueue(self, submission_id: int, *, job_id: str) -> None:
        self.queue.enqueue(
            "backend.app.erp_worker.process_erp_submission_job",
            submission_id,
            job_id=job_id,
            job_timeout=self.config.job_timeout_seconds,
            on_failure=Callback("backend.app.erp_worker.mark_erp_job_failed"),
        )
