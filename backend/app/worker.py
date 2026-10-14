"""Pontos de entrada importáveis pelo RQ; argumentos serializados ficam só com o ID."""

import os

from rq import get_current_job

from backend.app.db import create_database
from backend.app.integrations.ollama_extraction import OllamaConfig, OllamaExtractionProvider
from backend.app.repositories.documents import SqlAlchemyDocumentRepository
from backend.app.repositories.commercial_policies import SqlAlchemyCommercialPolicyRepository
from backend.app.repositories.purchase_orders import SqlAlchemyPurchaseOrderRepository
from backend.app.services.document_processing import DocumentProcessingConfig, DocumentProcessingService
from backend.app.services.document_text_extractor import OcrConfig
from backend.app.services.document_queue import DocumentQueueConfig
from backend.app.services.document_storage import DocumentFileStorage
from backend.app.services.document_worker import DocumentWorkerService


def create_worker_service() -> tuple[DocumentWorkerService, object]:
    database = create_database()
    config = DocumentQueueConfig.from_environment()
    documents = SqlAlchemyDocumentRepository(database.session_factory)
    orders = SqlAlchemyPurchaseOrderRepository(database.session_factory)
    commercial_policies = SqlAlchemyCommercialPolicyRepository(database.session_factory)
    storage = DocumentFileStorage(os.getenv("DOCUMENT_STORAGE_DIR", "var/documents"))
    provider = OllamaExtractionProvider(OllamaConfig.from_environment())
    processing = DocumentProcessingService(
        provider,
        config=DocumentProcessingConfig(ocr=OcrConfig.from_environment()),
    )
    worker = DocumentWorkerService(
        documents, orders, storage, processing, config, commercial_policies
    )
    return worker, database.engine


def process_document_job(document_id: int) -> str:
    """RQ recebe somente document_id; job_id é metadado do RQ, não dado serializado."""
    job = get_current_job()
    if job is None:
        raise RuntimeError("Execução sem contexto RQ.")
    worker, engine = create_worker_service()
    try:
        return worker.process(document_id=document_id, job_id=job.id)
    finally:
        engine.dispose()


def mark_document_job_failed(job, connection, exc_type, exc_value, traceback) -> None:
    """Callback executado pelo RQ depois da última tentativa, sem salvar traceback no DocuFlow."""
    if not job.args:
        return
    worker, engine = create_worker_service()
    try:
        worker.mark_exhausted(document_id=int(job.args[0]), job_id=job.id)
    finally:
        engine.dispose()
