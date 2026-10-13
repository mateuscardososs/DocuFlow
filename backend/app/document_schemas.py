"""Respostas HTTP de documentos sem expor a chave interna do arquivo."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from backend.app.schemas import ReferenceDocument
from backend.app.validation import ValidationReport


class DocumentPageText(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: int
    text: str


class DocumentSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    workspace_id: int
    purchase_order_id: int
    original_filename: str
    size_bytes: int
    content_type: str
    sha256: str
    uploaded_at: datetime
    processing_status: Literal["processing", "succeeded", "failed"]
    review_status: Literal["pending"]
    processing_error: str | None
    final_status: Literal["blocked", "divergent", "ready_for_review"] | None


class DocumentDetail(DocumentSummary):
    page_texts: list[DocumentPageText] | None
    extracted_document: ReferenceDocument | None
    pdf_warnings: list[str] | None
    validation_result: ValidationReport | None


class DocumentAnalysisResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: int
    duplicate: bool
    processing_status: Literal["processing", "succeeded", "failed"]
    review_status: Literal["pending"]
    processing_error: str | None
    document: ReferenceDocument | None
    pdf_warnings: list[str] | None
    validation: ValidationReport | None
    status: Literal["blocked", "divergent", "ready_for_review"] | None
