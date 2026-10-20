#!/usr/bin/env python3
"""Gera documentos de demonstração e gabaritos sem acessar serviços externos."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import tempfile
from dataclasses import dataclass, replace
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pypdfium2 as pdfium
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import letter
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.lib.utils import ImageReader

from backend.app.commercial_policy import CommercialPolicy
from backend.app.evidence import normalize_spaces
from backend.app.reference_order_data import REFERENCE_PURCHASE_ORDER
from backend.app.schemas import DocumentItem, ExtractedField, ReferenceDocument
from backend.app.services.pdf_text_extractor import DEFAULT_MAX_BYTES, MAX_PAGES
from backend.app.validation import validate_document


DISCLAIMER = "DOCUMENTO FICTÍCIO PARA TESTES — SEM VALIDADE FISCAL"
PAGE_SIZE = letter
RASTER_DPI = 200
IMAGE_SEED = 20261020
FONT_REGULAR = "DocuFlowVera"
FONT_BOLD = "DocuFlowVeraBold"
MONEY_TOLERANCE = Decimal("0.01")


@dataclass(frozen=True)
class CaseSpec:
    case_id: str
    filename: str
    kind: str
    document_number: str | None
    issue_date: str | None
    supplier_name: str
    supplier_identifier: str
    item_values: tuple[tuple[str, str, str, str, str], ...]
    document_total: str
    absent_fields: tuple[str, ...] = ()
    duplicate_of: str | None = None


@dataclass(frozen=True)
class BuiltCase:
    spec: CaseSpec
    document: ReferenceDocument
    page_text: str
    item_calculations: list[dict[str, str | bool]]
    rotation_degrees: float | None = None


def _register_fonts() -> Path:
    import reportlab

    font_dir = Path(reportlab.__file__).parent / "fonts"
    regular = font_dir / "Vera.ttf"
    bold = font_dir / "VeraBd.ttf"
    if not regular.is_file() or not bold.is_file():
        raise RuntimeError("As fontes Vera incluídas no ReportLab não foram encontradas.")
    pdfmetrics.registerFont(TTFont(FONT_REGULAR, str(regular)))
    pdfmetrics.registerFont(TTFont(FONT_BOLD, str(bold)))
    return regular


def _brl(amount: Decimal) -> str:
    formatted = f"{amount:,.2f}"
    return "R$ " + formatted.replace(",", "_").replace(".", ",").replace("_", ".")


def _make_field(raw: str | None, value: str | None, evidence: str | None) -> ExtractedField:
    if raw is None or value is None or evidence is None:
        return ExtractedField(raw_value=None, value=None, page=None, evidence=None)
    return ExtractedField(raw_value=raw, value=value, page=1, evidence=evidence)


def _base_items() -> tuple[tuple[str, str, str, str, str], ...]:
    return tuple(
        (
            item.code,
            item.description,
            str(Decimal(item.quantity)),
            format(Decimal(item.unit_price), ".2f"),
            format(Decimal(item.line_total), ".2f"),
        )
        for item in REFERENCE_PURCHASE_ORDER.items
    )


def _case_specs() -> list[CaseSpec]:
    base_items = _base_items()
    correct = CaseSpec(
        case_id="01",
        filename="01_correto.pdf",
        kind="native_pdf",
        document_number="DOC-DEMO-001",
        issue_date="10/10/2026",
        supplier_name=REFERENCE_PURCHASE_ORDER.supplier_name.upper(),
        supplier_identifier=REFERENCE_PURCHASE_ORDER.supplier_identifier,
        item_values=base_items,
        document_total=REFERENCE_PURCHASE_ORDER.total,
    )

    price_items = list(base_items)
    price_items[0] = (price_items[0][0], price_items[0][1], "10", "90.00", "900.00")
    quantity_items = list(base_items)
    quantity_items[0] = (quantity_items[0][0], quantity_items[0][1], "12", "85.50", "1026.00")

    return [
        correct,
        replace(
            correct,
            case_id="02",
            filename="02_preco_divergente.pdf",
            document_number="DOC-DEMO-002",
            item_values=tuple(price_items),
            document_total="1295.00",
        ),
        replace(
            correct,
            case_id="03",
            filename="03_total_incorreto.pdf",
            document_number="DOC-DEMO-003",
            document_total="1300.00",
        ),
        replace(
            correct,
            case_id="04",
            filename="04_quantidade_divergente.pdf",
            document_number="DOC-DEMO-004",
            item_values=tuple(quantity_items),
            document_total="1421.00",
        ),
        replace(
            correct,
            case_id="05",
            filename="05_fornecedor_divergente.pdf",
            document_number="DOC-DEMO-005",
            supplier_name="Comercial Aurora Fictícia Ltda.",
            supplier_identifier="FORN-0099",
        ),
        replace(
            correct,
            case_id="06",
            filename="06_numero_ausente.pdf",
            document_number=None,
            absent_fields=("document_number",),
        ),
        replace(
            correct,
            case_id="07",
            filename="07_data_ausente.pdf",
            document_number="DOC-DEMO-007",
            issue_date=None,
            absent_fields=("issue_date",),
        ),
        replace(
            correct,
            case_id="08",
            filename="08_correto_digitalizado.pdf",
            kind="scanned_pdf",
            document_number="DOC-DEMO-008",
        ),
        replace(
            correct,
            case_id="09",
            filename="09_correto_foto.png",
            kind="image",
            document_number="DOC-DEMO-009",
        ),
        replace(
            correct,
            case_id="10",
            filename="10_duplicata_exata.pdf",
            duplicate_of="01",
        ),
    ]


def _build_case(spec: CaseSpec) -> BuiltCase:
    item_rows: list[str] = []
    extracted_items: list[DocumentItem] = []
    calculations: list[dict[str, str | bool]] = []

    for code, description, quantity_text, price_text, total_text in spec.item_values:
        quantity = Decimal(quantity_text)
        price = Decimal(price_text)
        line_total = Decimal(total_text)
        row = " | ".join((code, description, quantity_text, _brl(price), _brl(line_total)))
        item_rows.append(row)
        extracted_items.append(
            DocumentItem(
                code=_make_field(code, code, row),
                description=_make_field(description, description, row),
                quantity=_make_field(quantity_text, quantity_text, row),
                unit_price=_make_field(_brl(price), format(price, ".2f"), row),
                line_total=_make_field(_brl(line_total), format(line_total, ".2f"), row),
            )
        )
        calculated = quantity * price
        calculations.append(
            {
                "quantity": quantity_text,
                "unit_price": format(price, ".2f"),
                "printed_line_total": format(line_total, ".2f"),
                "calculated_line_total": format(calculated, ".2f"),
                "matches_with_0_01_tolerance": abs(calculated - line_total) <= MONEY_TOLERANCE,
            }
        )

    supplier_line = spec.supplier_name
    identifier_line = f"Identificador: {spec.supplier_identifier}"
    document_line = f"Documento comercial: {spec.document_number}" if spec.document_number else None
    date_line = f"Data de emissão: {spec.issue_date}" if spec.issue_date else None
    order_line = f"Pedido de compra: {REFERENCE_PURCHASE_ORDER.reference}"
    currency_line = f"Moeda: {REFERENCE_PURCHASE_ORDER.currency}"
    total = Decimal(spec.document_total)
    total_line = f"Total do documento: {_brl(total)}"
    page_lines = [
        DISCLAIMER,
        supplier_line,
        identifier_line,
        *([document_line] if document_line else []),
        *([date_line] if date_line else []),
        order_line,
        currency_line,
        "Código | Descrição | Quantidade | Preço unitário | Total",
        *item_rows,
        total_line,
    ]
    page_text = "\n".join(page_lines)

    document = ReferenceDocument(
        supplier_name=_make_field(spec.supplier_name, spec.supplier_name, supplier_line),
        supplier_identifier=_make_field(spec.supplier_identifier, spec.supplier_identifier, identifier_line),
        document_number=_make_field(spec.document_number, spec.document_number, document_line),
        issue_date=_make_field(
            spec.issue_date,
            "2026-10-10" if spec.issue_date else None,
            date_line,
        ),
        purchase_order_reference=_make_field(
            REFERENCE_PURCHASE_ORDER.reference,
            REFERENCE_PURCHASE_ORDER.reference,
            order_line,
        ),
        currency=_make_field(REFERENCE_PURCHASE_ORDER.currency, REFERENCE_PURCHASE_ORDER.currency, currency_line),
        items=extracted_items,
        document_total=_make_field(_brl(total), format(total, ".2f"), total_line),
        unsupported_elements=[],
    )
    return BuiltCase(spec, document, page_text, calculations)


def _layout_lines(case: BuiltCase) -> list[tuple[str, str]]:
    spec = case.spec
    rows = [item.code.evidence or "" for item in case.document.items]
    lines: list[tuple[str, str]] = [
        ("disclaimer", DISCLAIMER),
        ("supplier", spec.supplier_name),
        ("metadata", f"Identificador: {spec.supplier_identifier}"),
    ]
    if spec.document_number:
        lines.append(("metadata", f"Documento comercial: {spec.document_number}"))
    if spec.issue_date:
        lines.append(("metadata", f"Data de emissão: {spec.issue_date}"))
    lines.extend(
        [
            ("metadata", f"Pedido de compra: {REFERENCE_PURCHASE_ORDER.reference}"),
            ("metadata", f"Moeda: {REFERENCE_PURCHASE_ORDER.currency}"),
            ("spacer", ""),
            ("header", "Código | Descrição | Quantidade | Preço unitário | Total"),
            *[("item", row) for row in rows],
            ("spacer", ""),
            ("total", f"Total do documento: {_brl(Decimal(spec.document_total))}"),
        ]
    )
    return lines


def _draw_native_pdf(case: BuiltCase, font_path: Path) -> bytes:
    output = BytesIO()
    canvas = Canvas(output, pagesize=PAGE_SIZE, pageCompression=1, invariant=1)
    canvas.setTitle("Documento comercial fictício")
    canvas.setAuthor("DocuFlow - dados fictícios")
    width, height = PAGE_SIZE
    y = height - 54
    for role, text in _layout_lines(case):
        if role == "spacer":
            y -= 14
            continue
        if role == "disclaimer":
            canvas.setFont(FONT_BOLD, 9)
            canvas.setFillColor(HexColor("#8a1c1c"))
            canvas.drawCentredString(width / 2, y, text)
            canvas.setFillColor(HexColor("#202124"))
            y -= 36
        elif role == "supplier":
            canvas.setFont(FONT_BOLD, 16)
            canvas.drawString(52, y, text)
            y -= 28
        elif role == "header":
            canvas.setFont(FONT_BOLD, 9)
            canvas.drawString(52, y, text)
            y -= 20
        elif role == "item":
            canvas.setFont(FONT_REGULAR, 8.5)
            canvas.drawString(52, y, text)
            y -= 19
        elif role == "total":
            canvas.setFont(FONT_BOLD, 12)
            canvas.drawString(52, y, text)
        else:
            canvas.setFont(FONT_REGULAR, 10)
            canvas.drawString(52, y, text)
            y -= 19
    canvas.showPage()
    canvas.save()
    return output.getvalue()


def _render_pdf_grayscale(pdf_bytes: bytes) -> Image.Image:
    document = pdfium.PdfDocument(pdf_bytes)
    page = document[0]
    bitmap = page.render(scale=RASTER_DPI / 72, grayscale=True)
    try:
        return bitmap.to_pil().convert("L")
    finally:
        bitmap.close()
        page.close()
        document.close()


def _draw_png(case: BuiltCase, font_path: Path, seed: int) -> tuple[bytes, float]:
    scale = RASTER_DPI / 72
    image = Image.new("L", (round(PAGE_SIZE[0] * scale), round(PAGE_SIZE[1] * scale)), 255)
    draw = ImageDraw.Draw(image)
    y = round(54 * scale)
    margin = round(52 * scale)
    fonts = {
        "disclaimer": ImageFont.truetype(str(font_path), round(9 * scale)),
        "supplier": ImageFont.truetype(str(font_path), round(16 * scale)),
        "metadata": ImageFont.truetype(str(font_path), round(10 * scale)),
        "header": ImageFont.truetype(str(font_path), round(9 * scale)),
        "item": ImageFont.truetype(str(font_path), round(8.5 * scale)),
        "total": ImageFont.truetype(str(font_path), round(12 * scale)),
    }
    bold_font = ImageFont.truetype(str(font_path.parent / "VeraBd.ttf"), round(9 * scale))
    for role, text in _layout_lines(case):
        if role == "spacer":
            y += round(14 * scale)
            continue
        font = fonts.get(role, fonts["metadata"])
        fill = 110 if role == "disclaimer" else 20
        if role == "disclaimer":
            font = bold_font
        if role in {"supplier", "header", "total"}:
            font = ImageFont.truetype(
                str(font_path.parent / "VeraBd.ttf"),
                round((16 if role == "supplier" else 9 if role == "header" else 12) * scale),
            )
        draw.text((margin, y), text, font=font, fill=fill)
        if role == "disclaimer":
            y += round(36 * scale)
        elif role == "supplier":
            y += round(28 * scale)
        elif role == "header":
            y += round(20 * scale)
        elif role == "item":
            y += round(19 * scale)
        elif role != "total":
            y += round(19 * scale)

    rotation = random.Random(seed).uniform(-0.2, 0.2)
    rotated = image.rotate(
        rotation,
        resample=Image.Resampling.BICUBIC,
        expand=True,
        fillcolor=255,
    )
    output = BytesIO()
    rotated.save(output, format="PNG", dpi=(RASTER_DPI, RASTER_DPI), compress_level=9)
    return output.getvalue(), rotation


def _report_for(case: BuiltCase) -> dict[str, Any]:
    policy = CommercialPolicy.defaults()
    report = validate_document(
        case.document,
        REFERENCE_PURCHASE_ORDER,
        {1: case.page_text},
        commercial_policy=policy,
    )
    evidence_checks = []
    for field_path, field in _iter_fields(case.document):
        evidence_checks.append(
            {
                "field": field_path,
                "page": field.page,
                "evidence": field.evidence,
                "matches_canonical_text_after_space_normalization": (
                    field.page == 1
                    and field.evidence is not None
                    and normalize_spaces(field.evidence) in normalize_spaces(case.page_text)
                ),
            }
        )
    amount = Decimal(case.spec.document_total)
    line_sum = sum((Decimal(item["printed_line_total"]) for item in case.item_calculations), Decimal("0"))
    return {
        "case_id": case.spec.case_id,
        "filename": case.spec.filename,
        "type": case.spec.kind,
        "purchase_order_reference": REFERENCE_PURCHASE_ORDER.reference,
        "document_truth": {
            "extracted_document": case.document.model_dump(mode="json"),
            "expected_printed_text_by_page": [{"page": 1, "text": case.page_text}],
            "absent_fields": list(case.spec.absent_fields),
            "item_arithmetic": case.item_calculations,
            "printed_items_sum": format(line_sum, ".2f"),
            "printed_document_total": format(amount, ".2f"),
            "items_sum_matches_printed_total_with_0_01_tolerance": abs(line_sum - amount) <= MONEY_TOLERANCE,
            "evidence_checks_against_canonical_text": evidence_checks,
        },
        "commercial_expectation": {
            "final_status": report.status,
            "finding_codes": [finding.code for finding in report.findings],
            "divergence_codes": [finding.code for finding in report.findings if finding.severity == "divergence"],
            "findings": [
                {"code": finding.code, "severity": finding.severity}
                for finding in report.findings
            ],
        },
        "duplicate_of_case_id": case.spec.duplicate_of,
        "duplicate_behavior": (
            {
                "same_workspace": "return_existing_document",
                "different_workspace": "create_document",
                "existing_case_id": case.spec.duplicate_of,
            }
            if case.spec.duplicate_of is not None
            else {
                "same_workspace": "create_document",
                "different_workspace": "create_document",
                "existing_case_id": None,
            }
        ),
        "rasterization": (
            {
                "dpi": RASTER_DPI,
                "color_mode": "grayscale",
                "rotation_degrees": round(case.rotation_degrees, 4) if case.rotation_degrees is not None else None,
            }
            if case.spec.kind in {"scanned_pdf", "image"}
            else None
        ),
    }


def _iter_fields(document: ReferenceDocument):
    for name in (
        "supplier_name", "supplier_identifier", "document_number", "issue_date",
        "purchase_order_reference", "currency", "document_total",
    ):
        yield name, getattr(document, name)
    for index, item in enumerate(document.items, start=1):
        for name in ("code", "description", "quantity", "unit_price", "line_total"):
            yield f"items[{index}].{name}", getattr(item, name)


def _build_outputs() -> tuple[dict[str, bytes], dict[str, Any]]:
    font_path = _register_fonts()
    specs = _case_specs()
    cases = [_build_case(spec) for spec in specs]
    outputs: dict[str, bytes] = {}
    case_rows: list[dict[str, Any]] = []

    correct_pdf: bytes | None = None
    for case in cases:
        if case.spec.case_id == "10":
            if correct_pdf is None:
                raise RuntimeError("O documento de referência para a duplicata não foi gerado.")
            output_bytes = correct_pdf
        elif case.spec.kind == "native_pdf":
            output_bytes = _draw_native_pdf(case, font_path)
            extracted = PdfReader(BytesIO(output_bytes)).pages[0].extract_text() or ""
            if normalize_spaces(case.page_text) not in normalize_spaces(extracted):
                raise RuntimeError(f"O texto nativo de {case.spec.filename} não corresponde ao gabarito.")
        elif case.spec.kind == "scanned_pdf":
            native_source = _draw_native_pdf(case, font_path)
            raster = _render_pdf_grayscale(native_source)
            raster_pdf = BytesIO()
            canvas = Canvas(raster_pdf, pagesize=PAGE_SIZE, pageCompression=1, invariant=1)
            canvas.setTitle("Documento comercial fictício")
            canvas.setAuthor("DocuFlow - dados fictícios")
            canvas.drawImage(ImageReader(raster), 0, 0, width=PAGE_SIZE[0], height=PAGE_SIZE[1])
            canvas.showPage()
            canvas.save()
            output_bytes = raster_pdf.getvalue()
            pdf_reader = PdfReader(BytesIO(output_bytes))
            if len(pdf_reader.pages) != 1 or (pdf_reader.pages[0].extract_text() or "").strip():
                raise RuntimeError("O PDF digitalizado precisa ter uma página somente-imagem e sem texto extraível.")
            width, height = raster.size
            if abs(width / PAGE_SIZE[0] * 72 - RASTER_DPI) > 1 or abs(height / PAGE_SIZE[1] * 72 - RASTER_DPI) > 1:
                raise RuntimeError("A resolução rasterizada do PDF saiu do alvo aproximado de 200 DPI.")
        elif case.spec.kind == "image":
            output_bytes, rotation = _draw_png(case, font_path, IMAGE_SEED + int(case.spec.case_id))
            case = replace(case, rotation_degrees=rotation)
            with Image.open(BytesIO(output_bytes)) as image:
                if image.mode != "L" or image.width * image.height > 40_000_000:
                    raise RuntimeError("A imagem de demonstração precisa ser grayscale e respeitar o limite de pixels.")
                stored_dpi = image.info.get("dpi") or (0, 0)
                if any(abs(value - RASTER_DPI) > 1 for value in stored_dpi):
                    raise RuntimeError("A imagem PNG não preservou os metadados de 200 DPI.")
        else:
            raise RuntimeError(f"Tipo de amostra desconhecido: {case.spec.kind}")

        if len(output_bytes) > DEFAULT_MAX_BYTES:
            raise RuntimeError(f"{case.spec.filename} excede o limite atual de upload.")
        outputs[case.spec.filename] = output_bytes
        if case.spec.case_id == "01":
            correct_pdf = output_bytes

        row = _report_for(case)
        row["sha256"] = hashlib.sha256(output_bytes).hexdigest()
        case_rows.append(row)

    manifest = {
        "manifest_version": 1,
        "reference_order": REFERENCE_PURCHASE_ORDER.model_dump(mode="json"),
        "commercial_policy": CommercialPolicy.defaults().snapshot(),
        "commercial_policy_description": "Política padrão atual, versão 0, tolerância de totais padrão BRL 0.01; nenhuma sobrescrita de moeda.",
        "evidence_normalization": "A comparação do gabarito normaliza espaços com backend.app.evidence.normalize_spaces: colapsa espaços consecutivos e remove bordas. Isso não altera a validação de produção.",
        "limits": {"max_pages": MAX_PAGES, "max_upload_bytes": DEFAULT_MAX_BYTES},
        "cases": case_rows,
    }
    if outputs["01_correto.pdf"] != outputs["10_duplicata_exata.pdf"]:
        raise RuntimeError("A duplicata exata deve conter os mesmos bytes do caso 01.")
    return outputs, manifest


def _write_new_file(path: Path, content: bytes) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        try:
            os.link(temporary_path, path)
        except FileExistsError:
            if path.read_bytes() != content:
                raise FileExistsError(f"Saída existente diferente da gerada: {path}") from None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def write_package(documents_dir: Path, ground_truth_dir: Path) -> int:
    outputs, manifest = _build_outputs()
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    all_outputs = {
        **{documents_dir / name: content for name, content in outputs.items()},
        ground_truth_dir / "manifest.json": manifest_bytes,
    }

    conflicts = [
        str(path)
        for path, content in all_outputs.items()
        if path.exists() and path.read_bytes() != content
    ]
    if conflicts:
        raise FileExistsError(
            "Arquivos existentes diferem das saídas reproduzidas; mova-os manualmente antes de regenerar: "
            + ", ".join(conflicts)
        )

    documents_dir.mkdir(parents=True, exist_ok=True)
    ground_truth_dir.mkdir(parents=True, exist_ok=True)
    for path, content in all_outputs.items():
        if not path.exists():
            _write_new_file(path, content)
    return len(outputs)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--documents-dir",
        type=Path,
        default=ROOT / "samples" / "documents",
        help="Diretório dos arquivos de entrada (padrão: samples/documents).",
    )
    parser.add_argument(
        "--ground-truth-dir",
        type=Path,
        default=ROOT / "samples" / "ground_truth",
        help="Diretório do manifesto separado (padrão: samples/ground_truth).",
    )
    args = parser.parse_args()
    try:
        count = write_package(args.documents_dir, args.ground_truth_dir)
    except (OSError, RuntimeError, ValueError) as error:
        parser.exit(2, f"Erro: {error}\n")
    print(f"{count} arquivos de entrada e manifest.json gerados em diretórios separados.")
    print(f"Limites usados: {MAX_PAGES} páginas, {DEFAULT_MAX_BYTES} bytes por arquivo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
