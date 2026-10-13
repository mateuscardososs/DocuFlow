"""Entradas RQ exclusivas dos testes reais; nunca usadas pela API de produção."""

import json
import os

from redis import Redis
from rq import Queue, Worker, get_current_job
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.models.document import DocumentEntity
from backend.app.repositories.documents import SqlAlchemyDocumentRepository
from backend.app.repositories.purchase_orders import SqlAlchemyPurchaseOrderRepository
from backend.app.services.document_processing import (
    DocumentProcessingConfig,
    DocumentProcessingService,
)
from backend.app.services.document_queue import DocumentQueueConfig
from backend.app.services.document_storage import DocumentFileStorage
from backend.app.services.document_worker import DocumentWorkerService
from backend.tests.test_document_processing import reference_payload


class DeterministicProvider:
    def __init__(self, document_id: int, session_factory, redis: Redis) -> None:
        self.document_id = document_id
        self.sessions = session_factory
        self.redis = redis

    def extract(self, *, prompt: str, output_schema: dict) -> str:
        with self.sessions() as session:
            status = session.scalar(
                select(DocumentEntity.processing_status).where(
                    DocumentEntity.id == self.document_id
                )
            )
        marker = f"{os.environ['TEST_RQ_QUEUE_NAME']}:observed:{self.document_id}"
        self.redis.set(marker, status or "missing")
        return json.dumps(reference_payload(), ensure_ascii=False)


def _runtime():
    database_url = os.environ["TEST_DATABASE_URL"]
    redis_url = os.environ["TEST_REDIS_URL"]
    engine = create_engine(database_url, pool_pre_ping=True)
    sessions = sessionmaker(engine, expire_on_commit=False)
    redis = Redis.from_url(redis_url)
    documents = SqlAlchemyDocumentRepository(sessions)
    orders = SqlAlchemyPurchaseOrderRepository(sessions)
    storage = DocumentFileStorage(os.environ["TEST_DOCUMENT_STORAGE_DIR"])
    return engine, sessions, redis, documents, orders, storage


def process_document_job(document_id: int) -> str:
    job = get_current_job()
    if job is None:
        raise RuntimeError("O job de teste precisa de contexto RQ.")
    engine, sessions, redis, documents, orders, storage = _runtime()
    try:
        provider = DeterministicProvider(document_id, sessions, redis)
        config = DocumentQueueConfig(
            redis_url=os.environ["TEST_REDIS_URL"],
            queue_name=os.environ["TEST_RQ_QUEUE_NAME"],
            max_attempts=3,
        )
        service = DocumentWorkerService(
            documents,
            orders,
            storage,
            DocumentProcessingService(provider, config=DocumentProcessingConfig()),
            config,
        )
        return service.process(document_id=document_id, job_id=job.id)
    finally:
        redis.close()
        engine.dispose()


def crash_after_claim(document_id: int) -> None:
    """Simula o processo caindo depois de marcar processing no PostgreSQL."""
    job = get_current_job()
    if job is None:
        raise RuntimeError("O job de teste precisa de contexto RQ.")
    engine, _, redis, documents, _, _ = _runtime()
    try:
        documents.claim_for_processing(
            document_id=document_id,
            job_id=job.id,
            max_attempts=3,
        )
        raise RuntimeError("interrupção simulada depois do claim")
    finally:
        redis.close()
        engine.dispose()


def run_burst() -> None:
    redis = Redis.from_url(os.environ["TEST_REDIS_URL"])
    try:
        queue = Queue(os.environ["TEST_RQ_QUEUE_NAME"], connection=redis)
        Worker([queue], connection=redis).work(
            burst=True,
            logging_level="WARNING",
        )
    finally:
        redis.close()


if __name__ == "__main__":
    run_burst()
