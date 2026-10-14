"""Coordena PDF, extração estruturada e validação comercial."""

from dataclasses import dataclass, field
from typing import Literal

from backend.app.commercial_policy import CommercialPolicy
from backend.app.schemas import PurchaseOrder, ReferenceDocument
from backend.app.services.document_text_extractor import (
    DocumentTextExtractor,
    OcrConfig,
)
from backend.app.services.pdf_text_extractor import DEFAULT_MAX_BYTES, PageText
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
    ocr: OcrConfig = field(default_factory=OcrConfig)


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
        text_extractor: DocumentTextExtractor | None = None,
    ) -> None:
        self.provider = provider
        self.config = config
        self.text_extractor = text_extractor or DocumentTextExtractor(
            max_bytes=config.pdf_max_bytes,
            config=config.ocr,
        )

    def process(
        self,
        pdf_bytes: bytes,
        order: PurchaseOrder,
        *,
        content_type: str = "application/pdf",
        commercial_policy: CommercialPolicy | None = None,
    ) -> DocumentProcessingResult:
        result, _ = self.process_with_pages(
            pdf_bytes, order, content_type=content_type, commercial_policy=commercial_policy
        )
        return result

    def process_with_pages(
        self,
        pdf_bytes: bytes,
        order: PurchaseOrder,
        *,
        content_type: str = "application/pdf",
        commercial_policy: CommercialPolicy | None = None,
    ) -> tuple[DocumentProcessingResult, list[PageText]]:
        extraction_result = self.text_extractor.extract(pdf_bytes, content_type)
        page_texts = {page.number: page.text for page in extraction_result.pages}

        document = StructuredExtractionService(
            self.provider,
            config=self.config.extraction,
        ).extract(extraction_result.pages)
        validation = validate_document(
            document, order, page_texts, commercial_policy=commercial_policy
        )

        result = DocumentProcessingResult(
            document=document,
            pdf_warnings=extraction_result.warnings,
            validation=validation,
            status=validation.status,
        )
        return result, extraction_result.pages
