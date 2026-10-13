"""Extrai texto nativo e usa Tesseract local apenas onde necessário."""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Callable, Protocol

import pypdfium2 as pdfium
from PIL import Image, UnidentifiedImageError

from backend.app.services.pdf_text_extractor import (
    DEFAULT_MAX_BYTES,
    MAX_PAGES,
    PageText,
    PdfExtractionError,
    PdfTextExtractionResult,
    extract_pdf_text,
)


class DocumentTextExtractionError(Exception):
    """Erro de leitura/OCR com mensagem segura para o usuário."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class OcrTimeoutError(Exception):
    pass


class OcrUnavailableError(Exception):
    pass


class OcrEngine(Protocol):
    def recognize(self, image: bytes, media_type: str) -> str: ...


@dataclass(frozen=True)
class OcrConfig:
    language: str = "por+eng"
    dpi: int = 300
    timeout_seconds: float = 30
    min_native_chars: int = 1
    max_image_pixels: int = 40_000_000
    tesseract_cmd: str = "tesseract"

    def __post_init__(self) -> None:
        if not self.language.strip() or self.dpi < 72 or self.timeout_seconds <= 0:
            raise ValueError("Configuração OCR inválida.")
        if self.min_native_chars < 1 or self.max_image_pixels < 1 or not self.tesseract_cmd:
            raise ValueError("Configuração OCR inválida.")

    @classmethod
    def from_environment(cls) -> "OcrConfig":
        return cls(
            language=os.getenv("DOCUFLOW_OCR_LANGUAGE", "por+eng"),
            dpi=int(os.getenv("DOCUFLOW_OCR_DPI", "300")),
            timeout_seconds=float(os.getenv("DOCUFLOW_OCR_TIMEOUT_SECONDS", "30")),
            min_native_chars=int(os.getenv("DOCUFLOW_NATIVE_TEXT_MIN_CHARS", "1")),
            tesseract_cmd=os.getenv("DOCUFLOW_TESSERACT_CMD", "tesseract"),
        )


class TesseractOcrEngine:
    """Adaptador de processo local; não envia imagens a serviços externos."""

    def __init__(self, config: OcrConfig = OcrConfig()) -> None:
        self.config = config

    def recognize(self, image: bytes, media_type: str) -> str:
        suffix = ".jpg" if media_type == "image/jpeg" else ".png"
        path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temp_file:
                temp_file.write(image)
                path = temp_file.name
            completed = subprocess.run(
                [
                    self.config.tesseract_cmd,
                    path,
                    "stdout",
                    "-l",
                    self.config.language,
                    "--dpi",
                    str(self.config.dpi),
                ],
                capture_output=True,
                text=True,
                timeout=self.config.timeout_seconds,
                check=False,
            )
        except FileNotFoundError:
            raise OcrUnavailableError() from None
        except subprocess.TimeoutExpired:
            raise OcrTimeoutError() from None
        except OSError:
            raise DocumentTextExtractionError(
                "ocr_failed", "O OCR local não conseguiu processar a página."
            ) from None
        finally:
            if path is not None:
                Path(path).unlink(missing_ok=True)
        if completed.returncode != 0:
            raise DocumentTextExtractionError(
                "ocr_failed", "O OCR local não conseguiu processar a página. Verifique os idiomas instalados."
            )
        return completed.stdout


PdfRenderer = Callable[[bytes, int, int], bytes]


@dataclass
class DocumentTextExtractionResult:
    pages: list[PageText]
    warnings: list[str]


class DocumentTextExtractor:
    SUPPORTED_TYPES = {"application/pdf", "image/png", "image/jpeg"}

    def __init__(
        self,
        *,
        ocr_engine: OcrEngine | None = None,
        config: OcrConfig = OcrConfig(),
        max_bytes: int = DEFAULT_MAX_BYTES,
        pdf_renderer: Callable[[bytes, int], bytes] | None = None,
    ) -> None:
        self.config = config
        self.ocr = ocr_engine or TesseractOcrEngine(config)
        self.max_bytes = max_bytes
        self.pdf_renderer = pdf_renderer or self._render_pdf_page

    def extract(self, content: bytes, media_type: str) -> DocumentTextExtractionResult:
        if media_type not in self.SUPPORTED_TYPES:
            raise DocumentTextExtractionError("unsupported_type", "Tipo de arquivo não suportado.")
        if not isinstance(content, bytes) or not content:
            raise DocumentTextExtractionError("invalid_document", "O arquivo está vazio ou inválido.")
        if len(content) > self.max_bytes:
            raise PdfExtractionError(
                "file_too_large",
                f"O arquivo excede o limite configurado de {self.max_bytes / (1024 * 1024):g} MB.",
            )
        if media_type == "application/pdf":
            return self._extract_pdf(content)
        page_image = self._validate_image(content, media_type)
        return DocumentTextExtractionResult(
            pages=[PageText(1, self._recognize(page_image, media_type), "ocr")],
            warnings=[],
        )

    def _extract_pdf(self, content: bytes) -> DocumentTextExtractionResult:
        result: PdfTextExtractionResult = extract_pdf_text(content, max_bytes=self.max_bytes)
        pages: list[PageText] = []
        warnings: list[str] = []
        for page in result.pages:
            native_usable = len(" ".join(page.text.split())) >= self.config.min_native_chars
            if native_usable:
                pages.append(PageText(page.number, page.text, "native"))
                continue
            try:
                rendered = self.pdf_renderer(content, page.number)
                text = self._recognize(rendered, "image/png")
            except DocumentTextExtractionError:
                raise
            except Exception:
                raise DocumentTextExtractionError(
                    "pdf_render_failed", "Não foi possível preparar uma página do PDF para OCR."
                ) from None
            pages.append(PageText(page.number, text, "ocr"))
            if not text.strip():
                warnings.append(f"A página {page.number} não contém texto utilizável após o OCR.")
        return DocumentTextExtractionResult(pages=pages, warnings=warnings)

    def _recognize(self, image: bytes, media_type: str) -> str:
        try:
            return self.ocr.recognize(image, media_type)
        except OcrTimeoutError:
            raise DocumentTextExtractionError(
                "ocr_timeout", "O OCR excedeu o tempo limite configurado."
            ) from None
        except OcrUnavailableError:
            raise DocumentTextExtractionError(
                "ocr_unavailable", "O Tesseract não está instalado ou não está disponível no worker."
            ) from None
        except DocumentTextExtractionError:
            raise
        except Exception:
            raise DocumentTextExtractionError(
                "ocr_failed", "O OCR local não conseguiu processar a página."
            ) from None

    def _validate_image(self, content: bytes, media_type: str) -> bytes:
        try:
            with Image.open(BytesIO(content)) as image:
                expected = "PNG" if media_type == "image/png" else "JPEG"
                if image.format != expected:
                    raise DocumentTextExtractionError(
                        "invalid_image", "A imagem não corresponde ao tipo de arquivo informado."
                    )
                if image.width * image.height > self.config.max_image_pixels:
                    raise DocumentTextExtractionError(
                        "image_too_large", "A resolução da imagem excede o limite permitido."
                    )
                image.load()
                normalized = image.convert("RGB")
                output = BytesIO()
                normalized.save(output, format=expected)
                return output.getvalue()
        except DocumentTextExtractionError:
            raise
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise DocumentTextExtractionError(
                "invalid_image", "Não foi possível ler a imagem. Verifique se o PNG ou JPEG é válido."
            ) from None

    def _render_pdf_page(self, content: bytes, page_number: int) -> bytes:
        document = None
        page = None
        bitmap = None
        image = None
        try:
            document = pdfium.PdfDocument(content)
            if len(document) > MAX_PAGES:
                raise DocumentTextExtractionError(
                    "too_many_pages", f"O documento excede o limite de {MAX_PAGES} páginas."
                )
            page = document[page_number - 1]
            width, height = page.get_size()
            scale = self.config.dpi / 72
            if width * scale * height * scale > self.config.max_image_pixels:
                raise DocumentTextExtractionError(
                    "image_too_large", "A resolução renderizada da página excede o limite permitido."
                )
            bitmap = page.render(scale=scale)
            image = bitmap.to_pil()
            output = BytesIO()
            image.save(output, format="PNG")
            return output.getvalue()
        except DocumentTextExtractionError:
            raise
        except Exception:
            raise DocumentTextExtractionError(
                "pdf_render_failed", "Não foi possível preparar uma página do PDF para OCR."
            ) from None
        finally:
            for resource in (image, bitmap, page, document):
                close = getattr(resource, "close", None)
                if close:
                    close()
