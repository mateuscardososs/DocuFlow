"""API test-only: PostgreSQL dedicado, filas RQ exclusivas e sem Ollama."""

import os

from redis import Redis
from rq import Queue

from backend.app.api.app import create_app
from backend.app.services.erp_queue import ERPQueueConfig, RQERPSubmissionQueue


class UnusedExtractionProvider:
    def extract(self, *, prompt: str, output_schema: dict) -> str:
        raise AssertionError("A extração deve ocorrer no worker determinístico de teste.")


class BrowserTestDocumentQueue:
    def __init__(self) -> None:
        self.connection = Redis.from_url(os.environ["TEST_REDIS_URL"])
        self.queue = Queue(os.environ["TEST_RQ_QUEUE_NAME"], connection=self.connection)

    def enqueue(self, document_id: int, *, job_id: str) -> None:
        self.queue.enqueue(
            "backend.tests.integration.browser_worker.process_document_job",
            document_id,
            job_id=job_id,
            job_timeout=60,
        )


document_queue = BrowserTestDocumentQueue()
erp_queue = RQERPSubmissionQueue(ERPQueueConfig.from_environment())
app = create_app(
    extraction_provider=UnusedExtractionProvider(),
    document_queue=document_queue,
    erp_submission_queue=erp_queue,
)


@app.on_event("shutdown")
def close_test_redis_connections() -> None:
    document_queue.connection.close()
    erp_queue.connection.close()
