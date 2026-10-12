"""Aplicação HTTP fina sobre os serviços existentes do DocuFlow."""

import os

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from backend.app.auth import AuthSettings, AuthenticatedUser, AuthenticationService
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
from backend.app.schemas import AccessToken, AuthenticatedUserProfile


def create_app(
    *,
    document_processing_service: DocumentProcessingService | None = None,
    extraction_provider: ExtractionProvider | None = None,
    order_repository: PurchaseOrderRepository | None = None,
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

    auth_settings = auth_settings or AuthSettings.from_environment()
    authentication = AuthenticationService(session_factory, auth_settings)
    oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")

    app = FastAPI(title="DocuFlow API", version="0.1.0")
    app.state.document_processing_service = document_processing_service
    app.state.order_repository = order_repository
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

    @app.post("/documents/analyze", response_model=DocumentProcessingResult)
    async def analyze_document(
        file: UploadFile = File(...),
        purchase_order_reference: str = Form(...),
        service: DocumentProcessingService = Depends(_get_processing_service),
        repository: PurchaseOrderRepository = Depends(_get_order_repository),
        upload_limit: int = Depends(_get_upload_limit),
        user: AuthenticatedUser = Depends(current_user),
        workspace_id: int = Header(alias="X-Workspace-ID"),
    ) -> DocumentProcessingResult:
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
            order = repository.get_by_reference(purchase_order_reference, workspace_id)
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
