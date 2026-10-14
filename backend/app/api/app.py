"""Aplicação HTTP fina sobre os serviços existentes do DocuFlow."""

import os

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from backend.app.auth import AuthSettings, AuthenticatedUser, AuthenticationService
from backend.app.document_schemas import (
    DocumentAnalysisResponse,
    DocumentApprovalView,
    DocumentAuditEventView,
    DocumentDetail,
    DocumentReviewResponse,
    DocumentSummary,
    ERPSubmissionRequestBody,
    ERPSubmissionView,
    ReviewCorrectionRequest,
    ReviewDecisionRequest,
    ReviewRejectionRequest,
)
from backend.app.db import create_database
from backend.app.integrations.ollama_extraction import OllamaConfig, OllamaExtractionProvider
from backend.app.models.document import DocumentEntity
from backend.app.models.erp_submission import ERPSubmissionEntity
from backend.app.repositories.purchase_orders import (
    PurchaseOrderRepository,
    SqlAlchemyPurchaseOrderRepository,
)
from backend.app.repositories.documents import DocumentRepository, SqlAlchemyDocumentRepository
from backend.app.repositories.erp_submissions import SqlAlchemyERPSubmissionRepository
from backend.app.services.document_lifecycle import (
    DocumentAttemptError,
    DocumentLifecycleService,
    DocumentNotEligible,
    DocumentNotFound,
)
from backend.app.services.document_queue import DocumentQueue, DocumentQueueConfig, RQDocumentQueue
from backend.app.services.document_review import (
    ApprovalBlocked,
    DocumentNotReviewable,
    DocumentReviewNotFound,
    DocumentReviewService,
    ReviewActionConflict,
    ReviewVersionConflict,
)
from backend.app.services.document_storage import DocumentFileStorage
from backend.app.services.document_processing import (
    DocumentProcessingConfig,
    DocumentProcessingService,
)
from backend.app.services.erp_queue import (
    ERPQueueConfig,
    ERPSubmissionQueue,
    RQERPSubmissionQueue,
)
from backend.app.services.erp_submission import (
    ERPSubmissionEnqueueError,
    ERPSubmissionNotEligible,
    ERPSubmissionNotFound,
    ERPSubmissionService,
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
    document_queue: DocumentQueue | None = None,
    session_factory: sessionmaker | None = None,
    auth_settings: AuthSettings | None = None,
    erp_submission_queue: ERPSubmissionQueue | None = None,
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
    if document_queue is None:
        document_queue = RQDocumentQueue(DocumentQueueConfig.from_environment())

    auth_settings = auth_settings or AuthSettings.from_environment()
    authentication = AuthenticationService(session_factory, auth_settings)
    document_lifecycle = DocumentLifecycleService(
        document_repository, document_storage, document_queue
    )
    document_review = DocumentReviewService(document_repository, order_repository)
    erp_submission_repository = SqlAlchemyERPSubmissionRepository(session_factory)
    if erp_submission_queue is None:
        erp_submission_queue = RQERPSubmissionQueue(ERPQueueConfig.from_environment())
    erp_submission = ERPSubmissionService(
        erp_submission_repository,
        erp_submission_queue,
    )
    oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")

    app = FastAPI(title="DocuFlow API", version="0.1.0")
    app.state.document_processing_service = document_processing_service
    app.state.order_repository = order_repository
    app.state.document_repository = document_repository
    app.state.document_storage = document_storage
    app.state.document_queue = document_queue
    app.state.document_lifecycle = document_lifecycle
    app.state.document_review = document_review
    app.state.erp_submission_repository = erp_submission_repository
    app.state.erp_submission_queue = erp_submission_queue
    app.state.erp_submission = erp_submission
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

    @app.post("/documents/analyze", response_model=DocumentAnalysisResponse, status_code=202)
    async def analyze_document(
        file: UploadFile = File(...),
        purchase_order_reference: str = Form(...),
        repository: PurchaseOrderRepository = Depends(_get_order_repository),
        upload_limit: int = Depends(_get_upload_limit),
        user: AuthenticatedUser = Depends(current_user),
        workspace_id: int = Header(alias="X-Workspace-ID"),
        response: Response = None,
    ) -> DocumentAnalysisResponse | JSONResponse:
        roles = authentication.roles_for(user.id, workspace_id)
        if not roles:
            raise HTTPException(status_code=403, detail="Usuário não pertence a este workspace.")
        if "operator" not in roles:
            raise HTTPException(status_code=403, detail="O papel operator é necessário para analisar.")

        media_type = (file.content_type or "").split(";", 1)[0].strip().lower()
        if media_type not in {"application/pdf", "image/png", "image/jpeg"}:
            await file.close()
            raise HTTPException(
                status_code=415,
                detail="Envie um arquivo PDF, PNG ou JPEG.",
            )

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
                if response is not None:
                    response.status_code = 200
                return _analysis_response(duplicate, duplicate=True)

            order = repository.get_by_reference(purchase_order_reference, workspace_id)
            if order is None:
                raise HTTPException(status_code=404, detail="Pedido de compra não encontrado.")

            attempt = document_lifecycle.enqueue_upload(
                workspace_id=workspace_id,
                order_id=_order_id(repository, purchase_order_reference, workspace_id),
                original_filename=file.filename
                or {
                    "application/pdf": "documento.pdf",
                    "image/png": "documento.png",
                    "image/jpeg": "documento.jpg",
                }[media_type],
                content_type=media_type,
                content=pdf_bytes,
            )
            if attempt.duplicate and response is not None:
                response.status_code = 200
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

    @app.post("/documents/{document_id}/reprocess", response_model=DocumentAnalysisResponse, status_code=202)
    def reprocess_document(
        document_id: int,
        user: AuthenticatedUser = Depends(current_user),
        workspace_id: int = Header(alias="X-Workspace-ID"),
    ) -> DocumentAnalysisResponse:
        _require_document_role(authentication, user, workspace_id, "operator")
        try:
            record = document_lifecycle.reprocess(document_id=document_id, workspace_id=workspace_id)
            return _analysis_response(record, duplicate=False)
        except DocumentNotFound:
            raise HTTPException(status_code=404, detail="Documento não encontrado.") from None
        except DocumentNotEligible:
            raise HTTPException(status_code=409, detail="O documento não está elegível para reprocessamento.") from None
        except DocumentAttemptError as error:
            return JSONResponse(
                status_code=503,
                content={"detail": str(error), "document_id": error.document_id, "processing_status": "failed"},
            )
        except SQLAlchemyError:
            raise HTTPException(status_code=503, detail="Banco de dados indisponível.") from None

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
            if record is None:
                raise HTTPException(status_code=404, detail="Documento não encontrado.")
            return _document_detail(record, document_repository)
        except SQLAlchemyError:
            raise HTTPException(status_code=503, detail="Banco de dados indisponível.") from None

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
        return FileResponse(path, filename=record.original_filename, media_type=record.content_type)

    @app.patch("/documents/{document_id}/review", response_model=DocumentReviewResponse)
    def correct_document(
        document_id: int,
        request: ReviewCorrectionRequest,
        user: AuthenticatedUser = Depends(current_user),
        workspace_id: int = Header(alias="X-Workspace-ID"),
    ) -> DocumentReviewResponse:
        _require_document_role(authentication, user, workspace_id, "operator")
        try:
            record = document_review.correct(
                document_id=document_id,
                workspace_id=workspace_id,
                expected_version=request.expected_version,
                reviewer_user_id=user.id,
                reviewed_document=request.reviewed_document,
                justification=request.justification,
            )
            return _review_response(record, document_repository)
        except (
            DocumentReviewNotFound,
            DocumentNotReviewable,
            ReviewVersionConflict,
            ReviewActionConflict,
            ApprovalBlocked,
        ) as error:
            raise _review_http_error(error) from None
        except SQLAlchemyError:
            raise HTTPException(status_code=503, detail="Banco de dados indisponível.") from None

    @app.post("/documents/{document_id}/approve", response_model=DocumentReviewResponse)
    def approve_document(
        document_id: int,
        request: ReviewDecisionRequest,
        user: AuthenticatedUser = Depends(current_user),
        workspace_id: int = Header(alias="X-Workspace-ID"),
    ) -> DocumentReviewResponse:
        _require_document_role(authentication, user, workspace_id, "approver")
        try:
            record = document_review.approve(
                document_id=document_id,
                workspace_id=workspace_id,
                expected_version=request.expected_version,
                approver_user_id=user.id,
            )
            return _review_response(record, document_repository)
        except (
            DocumentReviewNotFound,
            DocumentNotReviewable,
            ReviewVersionConflict,
            ReviewActionConflict,
            ApprovalBlocked,
        ) as error:
            raise _review_http_error(error) from None
        except SQLAlchemyError:
            raise HTTPException(status_code=503, detail="Banco de dados indisponível.") from None

    @app.post("/documents/{document_id}/reject", response_model=DocumentReviewResponse)
    def reject_document(
        document_id: int,
        request: ReviewRejectionRequest,
        user: AuthenticatedUser = Depends(current_user),
        workspace_id: int = Header(alias="X-Workspace-ID"),
    ) -> DocumentReviewResponse:
        _require_document_role(authentication, user, workspace_id, "approver")
        try:
            record = document_review.reject(
                document_id=document_id,
                workspace_id=workspace_id,
                expected_version=request.expected_version,
                approver_user_id=user.id,
                justification=request.justification,
            )
            return _review_response(record, document_repository)
        except (
            DocumentReviewNotFound,
            DocumentNotReviewable,
            ReviewVersionConflict,
            ReviewActionConflict,
            ApprovalBlocked,
        ) as error:
            raise _review_http_error(error) from None
        except SQLAlchemyError:
            raise HTTPException(status_code=503, detail="Banco de dados indisponível.") from None

    @app.get("/documents/{document_id}/audit", response_model=list[DocumentAuditEventView])
    def get_document_audit(
        document_id: int,
        user: AuthenticatedUser = Depends(current_user),
        workspace_id: int = Header(alias="X-Workspace-ID"),
    ) -> list[DocumentAuditEventView]:
        _require_workspace_member(authentication, user, workspace_id)
        try:
            if document_repository.get_by_id(document_id, workspace_id) is None:
                raise HTTPException(status_code=404, detail="Documento não encontrado.")
            return [
                DocumentAuditEventView.model_validate(event, from_attributes=True)
                for event in document_repository.audit_events(document_id, workspace_id)
            ]
        except SQLAlchemyError:
            raise HTTPException(status_code=503, detail="Banco de dados indisponível.") from None

    @app.post(
        "/documents/{document_id}/erp-submission",
        response_model=ERPSubmissionView,
        status_code=202,
    )
    def request_erp_submission(
        document_id: int,
        request: ERPSubmissionRequestBody,
        response: Response,
        user: AuthenticatedUser = Depends(current_user),
        workspace_id: int = Header(alias="X-Workspace-ID"),
    ) -> ERPSubmissionView | JSONResponse:
        _require_document_role(authentication, user, workspace_id, "approver")
        try:
            result = erp_submission.request(
                document_id=document_id,
                workspace_id=workspace_id,
                expected_version=request.expected_version,
                requested_by_user_id=user.id,
            )
            if not result.enqueued and result.submission.state == "succeeded":
                response.status_code = 200
            return _erp_submission_view(result.submission)
        except ERPSubmissionNotFound:
            raise HTTPException(status_code=404, detail="Documento não encontrado.") from None
        except ERPSubmissionNotEligible:
            raise HTTPException(
                status_code=409,
                detail="O documento não possui uma versão aprovada elegível para envio.",
            ) from None
        except ERPSubmissionEnqueueError as error:
            submission = erp_submission_repository.get_by_id(error.submission_id, workspace_id)
            content = {"detail": str(error)}
            if submission is not None:
                content["submission"] = _erp_submission_view(submission).model_dump(mode="json")
            return JSONResponse(status_code=503, content=content)
        except SQLAlchemyError:
            raise HTTPException(status_code=503, detail="Banco de dados indisponível.") from None

    @app.get("/erp-submissions/{submission_id}", response_model=ERPSubmissionView)
    def get_erp_submission(
        submission_id: int,
        user: AuthenticatedUser = Depends(current_user),
        workspace_id: int = Header(alias="X-Workspace-ID"),
    ) -> ERPSubmissionView:
        _require_document_role(authentication, user, workspace_id, "approver")
        try:
            submission = erp_submission_repository.get_by_id(submission_id, workspace_id)
        except SQLAlchemyError:
            raise HTTPException(status_code=503, detail="Banco de dados indisponível.") from None
        if submission is None:
            raise HTTPException(status_code=404, detail="Solicitação de envio não encontrada.")
        return _erp_submission_view(submission)

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
        state=record.processing_status,
        review_status=record.review_status,
        review_version=record.review_version,
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
        review_version=record.review_version,
        processing_error=record.processing_error,
        final_status=record.final_status,
    )


def _document_detail(record: DocumentEntity, repository: DocumentRepository) -> DocumentDetail:
    approval = None
    if record.review_status == "approved":
        stored_approval = repository.current_approval(
            record.id, record.workspace_id, record.review_version
        )
        if stored_approval is not None:
            approval = DocumentApprovalView(
                approved_version=stored_approval.reviewed_version,
                approver_user_id=stored_approval.approver_user_id,
                approved_at=stored_approval.approved_at,
                approved_document=ReferenceDocument.model_validate(
                    stored_approval.approved_document
                ),
                validation_result=ValidationReport.model_validate(
                    stored_approval.validation_result
                ),
            )
    return DocumentDetail(
        **_document_summary(record).model_dump(),
        page_texts=record.page_texts,
        extracted_document=(
            ReferenceDocument.model_validate(record.extracted_document)
            if record.extracted_document is not None
            else None
        ),
        reviewed_document=(
            ReferenceDocument.model_validate(record.reviewed_document)
            if record.reviewed_document is not None
            else None
        ),
        pdf_warnings=record.pdf_warnings,
        validation_result=(
            ValidationReport.model_validate(record.validation_result)
            if record.validation_result is not None
            else None
        ),
        approval=approval,
    )


def _processing_error_status(code: str) -> int:
    if code == "queue_unavailable":
        return 503
    if code == "storage_error":
        return 500
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


def _require_document_role(
    authentication: AuthenticationService,
    user: AuthenticatedUser,
    workspace_id: int,
    role: str,
) -> None:
    roles = authentication.roles_for(user.id, workspace_id)
    if not roles:
        raise HTTPException(status_code=403, detail="Usuário não pertence a este workspace.")
    if role not in roles:
        raise HTTPException(status_code=403, detail=f"O papel {role} é necessário para esta ação.")


def _review_response(
    record: DocumentEntity, repository: DocumentRepository
) -> DocumentReviewResponse:
    document_data = (
        record.reviewed_document
        if record.reviewed_document is not None
        else record.extracted_document
    )
    if document_data is None or record.validation_result is None:
        raise HTTPException(status_code=409, detail="Documento ainda não está disponível para revisão.")
    approval = None
    if record.review_status == "approved":
        stored_approval = repository.current_approval(
            record.id, record.workspace_id, record.review_version
        )
        if stored_approval is not None:
            approval = DocumentApprovalView(
                approved_version=stored_approval.reviewed_version,
                approver_user_id=stored_approval.approver_user_id,
                approved_at=stored_approval.approved_at,
                approved_document=ReferenceDocument.model_validate(
                    stored_approval.approved_document
                ),
                validation_result=ValidationReport.model_validate(
                    stored_approval.validation_result
                ),
            )
    return DocumentReviewResponse(
        document_id=record.id,
        review_status=record.review_status,
        review_version=record.review_version,
        reviewed_document=ReferenceDocument.model_validate(document_data),
        validation=ValidationReport.model_validate(record.validation_result),
        approval=approval,
    )


def _erp_submission_view(record: ERPSubmissionEntity) -> ERPSubmissionView:
    return ERPSubmissionView(
        id=record.id,
        document_id=record.document_id,
        approved_version=record.approved_version,
        state=record.state,
        attempts=record.attempts,
        erp_identifier=record.erp_identifier,
        failure_message=record.failure_message,
        requested_at=record.requested_at,
        updated_at=record.updated_at,
    )


def _review_http_error(error: Exception) -> HTTPException:
    if isinstance(error, DocumentReviewNotFound):
        return HTTPException(status_code=404, detail=str(error) or "Documento não encontrado.")
    if isinstance(error, (ReviewVersionConflict, ReviewActionConflict, DocumentNotReviewable)):
        return HTTPException(status_code=409, detail=str(error) or "A revisão não pode ser atualizada.")
    if isinstance(error, ApprovalBlocked):
        return HTTPException(status_code=422, detail=str(error))
    return HTTPException(status_code=409, detail="A ação de revisão não pode ser concluída.")
