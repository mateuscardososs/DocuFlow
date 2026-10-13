"""Coordena revisão humana usando as validações comerciais já existentes."""

import re

from backend.app.models.document import DocumentEntity
from backend.app.repositories.documents import DocumentRepository
from backend.app.repositories.purchase_orders import PurchaseOrderRepository
from backend.app.schemas import ReferenceDocument
from backend.app.validation import ValidationReport, validate_document


class DocumentReviewError(Exception):
    """Erro de domínio seguro para mapear a resposta HTTP."""


class DocumentReviewNotFound(DocumentReviewError):
    pass


class DocumentNotReviewable(DocumentReviewError):
    pass


class ReviewVersionConflict(DocumentReviewError):
    pass


class ReviewActionConflict(DocumentReviewError):
    pass


class ApprovalBlocked(DocumentReviewError):
    pass


_JWT_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}(?![A-Za-z0-9_-])"
)
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(password|senha|token|secret|segredo|api[_-]?key)\s*[:=]\s*\S+"
)


def _safe_justification(value: str) -> str:
    value = _JWT_PATTERN.sub("[REDACTED]", value.strip())
    return _SECRET_ASSIGNMENT_PATTERN.sub(r"\1=[REDACTED]", value)


class DocumentReviewService:
    def __init__(
        self,
        document_repository: DocumentRepository,
        order_repository: PurchaseOrderRepository,
    ) -> None:
        self.documents = document_repository
        self.orders = order_repository

    def correct(
        self,
        *,
        document_id: int,
        workspace_id: int,
        expected_version: int,
        reviewer_user_id: int,
        reviewed_document: ReferenceDocument,
        justification: str,
    ) -> DocumentEntity:
        current = self._load_reviewable(document_id, workspace_id, expected_version)
        validation = self._validate(current, reviewed_document)
        return self.documents.save_correction(
            document_id=document_id,
            workspace_id=workspace_id,
            expected_version=expected_version,
            reviewer_user_id=reviewer_user_id,
            reviewed_document=reviewed_document.model_dump(mode="json"),
            validation_result=validation.model_dump(mode="json"),
            justification=_safe_justification(justification),
        )

    def approve(
        self,
        *,
        document_id: int,
        workspace_id: int,
        expected_version: int,
        approver_user_id: int,
    ) -> DocumentEntity:
        current = self._load_reviewable(document_id, workspace_id, expected_version)
        document = self._current_document(current)
        validation = self._validate(current, document)
        if validation.status == "blocked":
            raise ApprovalBlocked("Documento contém erros bloqueantes e não pode ser aprovado.")
        return self.documents.save_approval(
            document_id=document_id,
            workspace_id=workspace_id,
            expected_version=expected_version,
            approver_user_id=approver_user_id,
            approved_document=document.model_dump(mode="json"),
            validation_result=validation.model_dump(mode="json"),
        )

    def reject(
        self,
        *,
        document_id: int,
        workspace_id: int,
        expected_version: int,
        approver_user_id: int,
        justification: str,
    ) -> DocumentEntity:
        self._load_reviewable(document_id, workspace_id, expected_version)
        return self.documents.save_rejection(
            document_id=document_id,
            workspace_id=workspace_id,
            expected_version=expected_version,
            approver_user_id=approver_user_id,
            justification=_safe_justification(justification),
        )

    def _load_reviewable(
        self, document_id: int, workspace_id: int, expected_version: int
    ) -> DocumentEntity:
        current = self.documents.get_by_id(document_id, workspace_id)
        if current is None:
            raise DocumentReviewNotFound("Documento não encontrado.")
        if current.review_version != expected_version:
            raise ReviewVersionConflict(
                "A versão do documento mudou. Atualize os dados antes de tentar novamente."
            )
        if current.processing_status != "succeeded" or current.extracted_document is None:
            raise DocumentNotReviewable("Documento ainda não está disponível para revisão.")
        return current

    @staticmethod
    def _current_document(current: DocumentEntity) -> ReferenceDocument:
        value = (
            current.reviewed_document
            if current.reviewed_document is not None
            else current.extracted_document
        )
        return ReferenceDocument.model_validate(value)

    def _validate(
        self, current: DocumentEntity, document: ReferenceDocument
    ) -> ValidationReport:
        order = self.orders.get_by_id(current.purchase_order_id, current.workspace_id)
        if order is None:
            raise DocumentReviewNotFound("Pedido de referência não encontrado.")
        page_texts = {
            int(page["number"]): str(page["text"])
            for page in (current.page_texts or [])
        }
        return validate_document(document, order, page_texts)
