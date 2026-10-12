"""Aplicação HTTP fina sobre os serviços existentes do DocuFlow."""

import os

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from sqlalchemy.exc import SQLAlchemyError

from backend.app.db import create_database
from backend.app.integrations.ollama_extraction import OllamaConfig, OllamaExtractionProvider
from backend.app.repositories.purchase_orders import (
    PurchaseOrderRepository,
    SqlAlchemyPurchaseOrderRepository,
)
from backend.app.services.document_processing import (
    DocumentProcessingConfig,
    DocumentProcessingResult,
    DocumentProcessingService,
)
from backend.app.services.pdf_text_extractor import DEFAULT_MAX_BYTES, PdfExtractionError
from backend.app.services.structured_extraction import (
    ExtractionProvider,
    StructuredExtractionError,
)


def create_app(
    *,
    document_processing_service: DocumentProcessingService | None = None,
    extraction_provider: ExtractionProvider | None = None,
    order_repository: PurchaseOrderRepository | None = None,
    max_upload_bytes: int = DEFAULT_MAX_BYTES,
) -> FastAPI:
    if max_upload_bytes <= 0:
        raise ValueError("max_upload_bytes precisa ser positivo.")
    if document_processing_service is not None and extraction_provider is not None:
        raise ValueError("Informe o serviço de processamento ou o provedor, não ambos.")

    if document_processing_service is None:
        provider = (
            extraction_provider
            if extraction_provider is not None
            else OllamaExtractionProvider(OllamaConfig(model=os.getenv("DOCUFLOW_OLLAMA_MODEL", "gemma4")))
        )
        document_processing_service = DocumentProcessingService(
            provider,
            config=DocumentProcessingConfig(pdf_max_bytes=max_upload_bytes),
        )

    database = None
    if order_repository is None:
        database = create_database()
        order_repository = SqlAlchemyPurchaseOrderRepository(database.session_factory)

    app = FastAPI(title="DocuFlow API", version="0.1.0")
    app.state.document_processing_service = document_processing_service
    app.state.order_repository = order_repository
    app.state.max_upload_bytes = max_upload_bytes
    app.state.database_engine = database.engine if database is not None else None

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/documents/analyze", response_model=DocumentProcessingResult)
    async def analyze_document(
        file: UploadFile = File(...),
        purchase_order_reference: str = Form(...),
        service: DocumentProcessingService = Depends(_get_processing_service),
        repository: PurchaseOrderRepository = Depends(_get_order_repository),
        upload_limit: int = Depends(_get_upload_limit),
    ) -> DocumentProcessingResult:
        media_type = (file.content_type or "").split(";", 1)[0].strip().lower()
        if media_type != "application/pdf":
            await file.close()
            raise HTTPException(status_code=415, detail="Envie um arquivo com tipo application/pdf.")

        if file.size is not None and file.size > upload_limit:
            await file.close()
            raise HTTPException(status_code=413, detail=_size_limit_message(upload_limit))

        try:
            # A leitura é limitada mesmo se o parser não informar o tamanho do upload.
            pdf_bytes = await file.read(upload_limit + 1)
        finally:
            await file.close()
        if len(pdf_bytes) > upload_limit:
            raise HTTPException(status_code=413, detail=_size_limit_message(upload_limit))

        try:
            order = repository.get_by_reference(purchase_order_reference)
        except SQLAlchemyError:
            raise HTTPException(status_code=503, detail="Banco de dados indisponível.") from None
        if order is None:
            raise HTTPException(status_code=404, detail="Pedido de compra não encontrado.")

        try:
            return service.process(pdf_bytes, order)
        except PdfExtractionError as error:
            status_code = 413 if error.code == "file_too_large" else 422
            raise HTTPException(status_code=status_code, detail=str(error)) from None
        except StructuredExtractionError as error:
            if error.code == "provider_timeout":
                status_code = 504
            elif error.code == "provider_error":
                status_code = 502
            else:
                status_code = 422
            raise HTTPException(status_code=status_code, detail=str(error)) from None
        except Exception:
            raise HTTPException(
                status_code=500,
                detail="Falha interna ao processar o documento.",
            ) from None

    return app


def _get_processing_service(request: Request) -> DocumentProcessingService:
    return request.app.state.document_processing_service


def _get_order_repository(request: Request) -> PurchaseOrderRepository:
    return request.app.state.order_repository


def _get_upload_limit(request: Request) -> int:
    return request.app.state.max_upload_bytes


def _size_limit_message(max_bytes: int) -> str:
    limit_mb = max_bytes / (1024 * 1024)
    return f"O arquivo excede o limite configurado de {limit_mb:g} MB."
