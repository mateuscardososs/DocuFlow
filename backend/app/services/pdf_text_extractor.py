"""Leitura local do texto já existente em PDFs, sem OCR."""

import warnings
from dataclasses import dataclass
from io import BytesIO

from pypdf import PdfReader
from pypdf.errors import PdfReadWarning


MAX_PAGES = 5
DEFAULT_MAX_BYTES = 10 * 1024 * 1024


class PdfExtractionError(Exception):
    """Erro seguro para apresentar a quem forneceu o documento."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class PageText:
    number: int
    text: str


@dataclass
class PdfTextExtractionResult:
    pages: list[PageText]
    warnings: list[str]


def extract_pdf_text(
    content: bytes,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> PdfTextExtractionResult:
    """Extrai texto por página e rejeita PDFs ilegíveis, protegidos ou longos."""
    if not isinstance(content, bytes):
        raise PdfExtractionError("invalid_pdf", "O conteúdo informado não está em formato PDF válido.")
    if max_bytes <= 0:
        raise ValueError("O limite de bytes precisa ser positivo.")
    if len(content) > max_bytes:
        limit_mb = max_bytes / (1024 * 1024)
        raise PdfExtractionError(
            "file_too_large",
            f"O arquivo excede o limite configurado de {limit_mb:g} MB.",
        )
    if b"%PDF-" not in content[:1024]:
        raise PdfExtractionError(
            "invalid_pdf",
            "O arquivo não parece ser um PDF válido.",
        )

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", PdfReadWarning)
            reader = PdfReader(BytesIO(content), strict=False)
            if reader.is_encrypted:
                try:
                    decrypted = reader.decrypt("")
                except Exception:
                    decrypted = 0
                if not decrypted:
                    raise PdfExtractionError(
                        "password_required",
                        "O PDF está protegido por senha e não pode ser lido sem uma senha.",
                    )

            page_count = len(reader.pages)
            if page_count == 0:
                raise PdfExtractionError("invalid_pdf", "O PDF não contém páginas legíveis.")
            if page_count > MAX_PAGES:
                raise PdfExtractionError(
                    "too_many_pages",
                    f"O PDF tem {page_count} páginas; o limite é {MAX_PAGES}.",
                )

            pages: list[PageText] = []
            page_warnings: list[str] = []
            for number, page in enumerate(reader.pages, start=1):
                text = page.extract_text() or ""
                pages.append(PageText(number=number, text=text))
                if not text.strip():
                    page_warnings.append(f"A página {number} não contém texto utilizável.")
            return PdfTextExtractionResult(pages=pages, warnings=page_warnings)
    except PdfExtractionError:
        raise
    except Exception:
        # Detalhes de parser variam conforme o arquivo e não são úteis ao usuário.
        raise PdfExtractionError(
            "invalid_pdf",
            "Não foi possível ler o PDF. Verifique se o arquivo é um PDF válido.",
        ) from None
