"""Coordena PDF, extração estruturada e validação comercial."""

from dataclasses import dataclass, field
from typing import Literal

from backend.app.schemas import PurchaseOrder, ReferenceDocument
from backend.app.services.pdf_text_extractor import DEFAULT_MAX_BYTES, extract_pdf_text
from backend.app.services.structured_extraction import (
    ExtractionProvider,
    StructuredExtractionConfig,
    StructuredExtractionService,
)
from backend.app.validation import ValidationReport, validate_document


@dataclass(frozen=True)
class DocumentProcessingConfig:
    pdf_max_bytes: int = DEFAULT_MAX_BYTES
    extraction: StructuredExtractionConfig = field(default_factory=StructuredExtractionConfig)


@dataclass(frozen=True)
class DocumentProcessingResult:
    document: ReferenceDocument
    pdf_warnings: list[str]
    validation: ValidationReport
    status: Literal["blocked", "divergent", "ready_for_review"]


class DocumentProcessingService:
    def __init__(
        self,
        provider: ExtractionProvider,
        *,
        config: DocumentProcessingConfig = DocumentProcessingConfig(),
    ) -> None:
        self.provider = provider
        self.config = config

    def process(self, pdf_bytes: bytes, order: PurchaseOrder) -> DocumentProcessingResult:
        pdf_result = extract_pdf_text(pdf_bytes, max_bytes=self.config.pdf_max_bytes)
        page_texts = {page.number: page.text for page in pdf_result.pages}

        document = StructuredExtractionService(
            self.provider,
            config=self.config.extraction,
        ).extract(page_texts)
        validation = validate_document(document, order, page_texts)

        return DocumentProcessingResult(
            document=document,
            pdf_warnings=pdf_result.warnings,
            validation=validation,
            status=validation.status,
        )
