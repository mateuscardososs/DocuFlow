"""Respostas HTTP de documentos sem expor a chave interna do arquivo."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.app.schemas import ReferenceDocument
from backend.app.validation import ValidationReport


class DocumentPageText(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: int
    text: str
    source: Literal["native", "ocr"] = "native"


class DocumentSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    workspace_id: int
    purchase_order_id: int
    purchase_order_reference: str | None = None
    supplier_name: str | None = None
    original_filename: str
    size_bytes: int
    content_type: str
    sha256: str
    uploaded_at: datetime
    processing_status: Literal["queued", "processing", "ready_for_review", "failed"]
    review_status: Literal["pending", "approved", "rejected"]
    review_version: int
    processing_error: str | None
    final_status: Literal["blocked", "divergent", "ready_for_review"] | None


class DocumentDetail(DocumentSummary):
    page_texts: list[DocumentPageText] | None
    extracted_document: ReferenceDocument | None
    reviewed_document: ReferenceDocument | None
    pdf_warnings: list[str] | None
    validation_result: ValidationReport | None
    approval: "DocumentApprovalView | None"


class DocumentAnalysisResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: int
    duplicate: bool
    processing_status: Literal["queued", "processing", "ready_for_review", "failed"]
    state: Literal["queued", "processing", "ready_for_review", "failed"]
    review_status: Literal["pending", "approved", "rejected"]
    review_version: int
    processing_error: str | None
    document: ReferenceDocument | None
    pdf_warnings: list[str] | None
    validation: ValidationReport | None
    status: Literal["blocked", "divergent", "ready_for_review"] | None


class DocumentEnqueueResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: int
    duplicate: bool = False
    state: Literal["queued", "processing", "ready_for_review", "failed"]
    processing_status: Literal["queued", "processing", "ready_for_review", "failed"]


class ReviewCorrectionRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    expected_version: int = Field(gt=0)
    justification: str = Field(min_length=1, max_length=4000)
    reviewed_document: ReferenceDocument

    @field_validator("justification")
    @classmethod
    def justification_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("A justificativa é obrigatória.")
        return normalized


class ReviewDecisionRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    expected_version: int = Field(gt=0)


class ReviewRejectionRequest(ReviewDecisionRequest):
    justification: str = Field(min_length=1, max_length=4000)

    @field_validator("justification")
    @classmethod
    def justification_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("A justificativa é obrigatória.")
        return normalized


class ERPSubmissionRequestBody(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    expected_version: int = Field(gt=0)


class DocumentApprovalView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved_version: int
    approver_user_id: int
    approved_at: datetime
    approved_document: ReferenceDocument
    validation_result: ValidationReport


class DocumentReviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: int
    review_status: Literal["pending", "approved", "rejected"]
    review_version: int
    reviewed_document: ReferenceDocument
    validation: ValidationReport
    approval: DocumentApprovalView | None


class DocumentAuditEventView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    event_type: Literal[
        "review", "correction", "approval", "rejection",
        "erp_submission", "erp_attempt", "erp_success", "erp_failure",
    ]
    version: int
    actor_user_id: int
    justification: str | None
    details: dict[str, Any] | None
    created_at: datetime


class ERPSubmissionView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    document_id: int
    approved_version: int
    state: Literal["queued", "processing", "succeeded", "failed"]
    attempts: int
    erp_identifier: str | None
    failure_message: str | None
    requested_at: datetime
    updated_at: datetime
