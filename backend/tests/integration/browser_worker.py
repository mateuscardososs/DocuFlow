"""Worker de navegador com provedor estruturado determinístico e ERP de demonstração."""

import json
import os
import sys

from redis import Redis
from rq import Queue, Worker, get_current_job
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.integrations.erp import DemoERPClient
from backend.app.repositories.documents import SqlAlchemyDocumentRepository
from backend.app.repositories.erp_submissions import SqlAlchemyERPSubmissionRepository
from backend.app.repositories.purchase_orders import SqlAlchemyPurchaseOrderRepository
from backend.app.services.document_processing import DocumentProcessingConfig, DocumentProcessingService
from backend.app.services.document_queue import DocumentQueueConfig
from backend.app.services.document_storage import DocumentFileStorage
from backend.app.services.document_text_extractor import OcrConfig
from backend.app.services.document_worker import DocumentWorkerService
from backend.app.services.erp_submission import ERPSubmissionWorkerService
from backend.tests.test_document_processing import reference_payload


class BrowserDeterministicProvider:
    def extract(self, *, prompt: str, output_schema: dict) -> str:
        payload = reference_payload()
        order_reference = os.environ["DOCUFLOW_TEST_ORDER_REFERENCE"]
        payload["purchase_order_reference"] = {
            "raw_value": order_reference,
            "value": order_reference,
            "page": 1,
            "evidence": f"Pedido de compra: {order_reference}",
        }
        return json.dumps(payload, ensure_ascii=False)


def process_document_job(document_id: int) -> str:
    job = get_current_job()
    if job is None:
        raise RuntimeError("O job do teste de navegador precisa de contexto RQ.")
    engine = create_engine(os.environ["TEST_DATABASE_URL"], pool_pre_ping=True)
    sessions = sessionmaker(engine, expire_on_commit=False)
    redis = Redis.from_url(os.environ["TEST_REDIS_URL"])
    documents = SqlAlchemyDocumentRepository(sessions)
    orders = SqlAlchemyPurchaseOrderRepository(sessions)
    storage = DocumentFileStorage(os.environ["DOCUMENT_STORAGE_DIR"])
    config = DocumentQueueConfig(
        redis_url=os.environ["TEST_REDIS_URL"],
        queue_name=os.environ["TEST_RQ_QUEUE_NAME"],
        max_attempts=1,
    )
    try:
        processing = DocumentProcessingService(
            BrowserDeterministicProvider(),
            config=DocumentProcessingConfig(ocr=OcrConfig.from_environment()),
        )
        worker = DocumentWorkerService(documents, orders, storage, processing, config)
        return worker.process(document_id=document_id, job_id=job.id)
    finally:
        redis.close()
        engine.dispose()


def work() -> None:
    redis = Redis.from_url(os.environ["TEST_REDIS_URL"])
    try:
        queues = [
            Queue(os.environ["TEST_RQ_QUEUE_NAME"], connection=redis),
            Queue(os.environ["TEST_ERP_RQ_QUEUE_NAME"], connection=redis),
        ]
        Worker(queues, connection=redis).work(burst=False, logging_level="WARNING")
    finally:
        redis.close()


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] != "work":
        raise SystemExit("Uso: python -m backend.tests.integration.browser_worker work")
    work()
