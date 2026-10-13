"""Aplicação HTTP fina sobre os serviços existentes do DocuFlow."""

import os

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from backend.app.auth import AuthSettings, AuthenticatedUser, AuthenticationService
from backend.app.document_schemas import DocumentAnalysisResponse, DocumentDetail, DocumentSummary
from backend.app.db import create_database
from backend.app.integrations.ollama_extraction import OllamaConfig, OllamaExtractionProvider
from backend.app.models.document import DocumentEntity
from backend.app.repositories.purchase_orders import (
    PurchaseOrderRepository,
    SqlAlchemyPurchaseOrderRepository,
)
from backend.app.repositories.documents import DocumentRepository, SqlAlchemyDocumentRepository
from backend.app.services.document_lifecycle import DocumentAttemptError, DocumentLifecycleService
from backend.app.services.document_storage import DocumentFileStorage
from backend.app.services.document_processing import (
    DocumentProcessingConfig,
    DocumentProcessingService,
)
from backend.app.services.pdf_text_extractor import DEFAULT_MAX_BYTES
from backend.app.services.structured_extraction import ExtractionProvider
from backend.app.schemas import AccessToken, AuthenticatedUserProfile, ReferenceDocument
from backend.app.validation import ValidationReport


def create_app(
    *,
    document_processing_service: DocumentProcessingService | None = None,
    extraction_provider: ExtractionProvider | None = None,
    order_repository: PurchaseOrderRepository | None = None,
    document_repository: DocumentRepository | None = None,
    document_storage: DocumentFileStorage | None = None,
    session_factory: sessionmaker | None = None,
    auth_settings: AuthSettings | None = None,
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
    if session_factory is None:
        if database is None:
            raise ValueError("Informe session_factory ao injetar um repositório de pedidos.")
        session_factory = database.session_factory

    if document_repository is None:
        document_repository = SqlAlchemyDocumentRepository(session_factory)
    if document_storage is None:
        document_storage = DocumentFileStorage(os.getenv("DOCUMENT_STORAGE_DIR", "var/documents"))

    auth_settings = auth_settings or AuthSettings.from_environment()
    authentication = AuthenticationService(session_factory, auth_settings)
    document_lifecycle = DocumentLifecycleService(
        document_repository, document_storage, document_processing_service
    )
    oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")

    app = FastAPI(title="DocuFlow API", version="0.1.0")
    app.state.document_processing_service = document_processing_service
    app.state.order_repository = order_repository
    app.state.document_repository = document_repository
    app.state.document_storage = document_storage
    app.state.document_lifecycle = document_lifecycle
    app.state.authentication = authentication
    app.state.max_upload_bytes = max_upload_bytes
    app.state.database_engine = database.engine if database is not None else None

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    def current_user(token: str = Depends(oauth2_scheme)) -> AuthenticatedUser:
        user = authentication.user_from_token(token)
        if user is None:
            raise HTTPException(
                status_code=401,
                detail="Token inválido ou expirado.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return user

    @app.post("/auth/token", response_model=AccessToken)
    def issue_token(form: OAuth2PasswordRequestForm = Depends()) -> AccessToken:
        user = authentication.authenticate(form.username, form.password)
        if user is None:
            raise HTTPException(
                status_code=401,
                detail="E-mail ou senha inválidos.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        token, expires_in = authentication.issue_access_token(user.id)
        return AccessToken(access_token=token, token_type="bearer", expires_in=expires_in)

    @app.get("/auth/me", response_model=AuthenticatedUserProfile)
    def get_current_profile(user: AuthenticatedUser = Depends(current_user)) -> dict:
        profile = authentication.profile(user.id)
        if profile is None:
            raise HTTPException(
                status_code=401,
                detail="Token inválido ou expirado.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return profile

    @app.post("/documents/analyze", response_model=DocumentAnalysisResponse)
    async def analyze_document(
        file: UploadFile = File(...),
        purchase_order_reference: str = Form(...),
        repository: PurchaseOrderRepository = Depends(_get_order_repository),
        upload_limit: int = Depends(_get_upload_limit),
        user: AuthenticatedUser = Depends(current_user),
        workspace_id: int = Header(alias="X-Workspace-ID"),
    ) -> DocumentAnalysisResponse | JSONResponse:
        roles = authentication.roles_for(user.id, workspace_id)
        if not roles:
            raise HTTPException(status_code=403, detail="Usuário não pertence a este workspace.")
        if "operator" not in roles:
            raise HTTPException(status_code=403, detail="O papel operator é necessário para analisar.")

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
            duplicate = document_lifecycle.find_duplicate(workspace_id, pdf_bytes)
            if duplicate is not None:
                return _analysis_response(duplicate, duplicate=True)

            order = repository.get_by_reference(purchase_order_reference, workspace_id)
            if order is None:
                raise HTTPException(status_code=404, detail="Pedido de compra não encontrado.")

            attempt = document_lifecycle.process_upload(
                workspace_id=workspace_id,
                order_id=_order_id(repository, purchase_order_reference, workspace_id),
                order=order,
                original_filename=file.filename or "documento.pdf",
                content_type=media_type,
                content=pdf_bytes,
            )
            return _analysis_response(attempt.document, duplicate=attempt.duplicate)
        except DocumentAttemptError as error:
            status_code = _processing_error_status(error.code)
            return JSONResponse(
                status_code=status_code,
                content={"detail": str(error), "document_id": error.document_id},
            )
        except SQLAlchemyError:
            raise HTTPException(status_code=503, detail="Banco de dados indisponível.") from None
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(
                status_code=500,
                detail="Falha interna ao processar o documento.",
            ) from None

    @app.get("/documents", response_model=list[DocumentSummary])
    def list_documents(
        user: AuthenticatedUser = Depends(current_user),
        workspace_id: int = Header(alias="X-Workspace-ID"),
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> list[DocumentSummary]:
        _require_workspace_member(authentication, user, workspace_id)
        try:
            records = document_repository.list_for_workspace(
                workspace_id, limit=limit, offset=offset
            )
        except SQLAlchemyError:
            raise HTTPException(status_code=503, detail="Banco de dados indisponível.") from None
        return [_document_summary(record) for record in records]

    @app.get("/documents/{document_id}", response_model=DocumentDetail)
    def get_document(
        document_id: int,
        user: AuthenticatedUser = Depends(current_user),
        workspace_id: int = Header(alias="X-Workspace-ID"),
    ) -> DocumentDetail:
        _require_workspace_member(authentication, user, workspace_id)
        try:
            record = document_repository.get_by_id(document_id, workspace_id)
        except SQLAlchemyError:
            raise HTTPException(status_code=503, detail="Banco de dados indisponível.") from None
        if record is None:
            raise HTTPException(status_code=404, detail="Documento não encontrado.")
        return _document_detail(record)

    @app.get("/documents/{document_id}/download")
    def download_document(
        document_id: int,
        user: AuthenticatedUser = Depends(current_user),
        workspace_id: int = Header(alias="X-Workspace-ID"),
    ) -> FileResponse:
        _require_workspace_member(authentication, user, workspace_id)
        try:
            record = document_repository.get_by_id(document_id, workspace_id)
        except SQLAlchemyError:
            raise HTTPException(status_code=503, detail="Banco de dados indisponível.") from None
        if record is None:
            raise HTTPException(status_code=404, detail="Documento não encontrado.")
        try:
            path = document_storage.path_for(record.storage_key)
        except ValueError:
            raise HTTPException(status_code=404, detail="Arquivo não encontrado.") from None
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Arquivo não encontrado.")
        return FileResponse(path, filename=record.original_filename, media_type="application/pdf")

    return app


def _get_order_repository(request: Request) -> PurchaseOrderRepository:
    return request.app.state.order_repository


def _get_upload_limit(request: Request) -> int:
    return request.app.state.max_upload_bytes


def _size_limit_message(max_bytes: int) -> str:
    limit_mb = max_bytes / (1024 * 1024)
    return f"O arquivo excede o limite configurado de {limit_mb:g} MB."


def _require_workspace_member(
    authentication: AuthenticationService,
    user: AuthenticatedUser,
    workspace_id: int,
) -> None:
    if not authentication.roles_for(user.id, workspace_id):
        raise HTTPException(status_code=403, detail="Usuário não pertence a este workspace.")


def _analysis_response(
    record: DocumentEntity, *, duplicate: bool
) -> DocumentAnalysisResponse:
    return DocumentAnalysisResponse(
        document_id=record.id,
        duplicate=duplicate,
        processing_status=record.processing_status,
        review_status=record.review_status,
        processing_error=record.processing_error,
        document=(
            ReferenceDocument.model_validate(record.extracted_document)
            if record.extracted_document is not None
            else None
        ),
        pdf_warnings=record.pdf_warnings,
        validation=(
            ValidationReport.model_validate(record.validation_result)
            if record.validation_result is not None
            else None
        ),
        status=record.final_status,
    )


def _document_summary(record: DocumentEntity) -> DocumentSummary:
    return DocumentSummary(
        id=record.id,
        workspace_id=record.workspace_id,
        purchase_order_id=record.purchase_order_id,
        original_filename=record.original_filename,
        size_bytes=record.size_bytes,
        content_type=record.content_type,
        sha256=record.sha256,
        uploaded_at=record.uploaded_at,
        processing_status=record.processing_status,
        review_status=record.review_status,
        processing_error=record.processing_error,
        final_status=record.final_status,
    )


def _document_detail(record: DocumentEntity) -> DocumentDetail:
    return DocumentDetail(
        **_document_summary(record).model_dump(),
        page_texts=record.page_texts,
        extracted_document=(
            ReferenceDocument.model_validate(record.extracted_document)
            if record.extracted_document is not None
            else None
        ),
        pdf_warnings=record.pdf_warnings,
        validation_result=(
            ValidationReport.model_validate(record.validation_result)
            if record.validation_result is not None
            else None
        ),
    )


def _processing_error_status(code: str) -> int:
    if code == "file_too_large":
        return 413
    if code == "provider_timeout":
        return 504
    if code == "provider_error":
        return 502
    if code == "internal_error":
        return 500
    return 422


def _order_id(repository: PurchaseOrderRepository, reference: str, workspace_id: int) -> int:
    """Resolve o ID persistido depois que a consulta de negócio validou o escopo."""
    order_id = repository.get_entity_id_by_reference(reference, workspace_id)
    if order_id is None:
        raise HTTPException(status_code=404, detail="Pedido de compra não encontrado.")
    return order_id
