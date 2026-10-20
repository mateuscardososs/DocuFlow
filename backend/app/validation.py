"""Validações comerciais determinísticas, sem dependências externas."""

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.app.commercial_policy import CommercialPolicy
from backend.app.evidence import document_number_evidence_identifies_value, evidence_is_on_page
from backend.app.schemas import ExtractedField, PurchaseOrder, ReferenceDocument


TOLERANCE = Decimal("0.01")
DECIMAL_PATTERN = re.compile(r"^[+-]?\d+(?:\.\d+)?$")


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: Literal["blocking_error", "divergence", "warning"]
    code: str
    message: str


class ValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["blocked", "divergent", "ready_for_review"]
    findings: list[Finding]
    commercial_policy: "AppliedCommercialPolicy" = Field(default_factory=lambda: AppliedCommercialPolicy())


class AppliedCommercialPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = 0
    default_total_tolerance: str = "0.01"
    currency_tolerances: dict[str, str] = Field(default_factory=dict)


def _decimal(value: str | None) -> Decimal | None:
    if value is None or not DECIMAL_PATTERN.fullmatch(value):
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def validate_document(
    document: ReferenceDocument,
    order: PurchaseOrder,
    page_texts: dict[int, str],
    *,
    commercial_policy: CommercialPolicy | None = None,
) -> ValidationReport:
    applied_policy = commercial_policy or CommercialPolicy.defaults()
    findings: list[Finding] = []

    def add(severity: Literal["blocking_error", "divergence", "warning"], code: str, message: str) -> None:
        findings.append(Finding(severity=severity, code=code, message=message))

    fields: list[tuple[str, ExtractedField]] = [
        (name, getattr(document, name))
        for name in (
            "supplier_name", "supplier_identifier", "document_number", "issue_date",
            "purchase_order_reference", "currency", "document_total",
        )
    ]
    if not document.items:
        add("blocking_error", "missing_items", "O documento precisa conter ao menos um item.")
    for index, item in enumerate(document.items, start=1):
        fields.extend(
            (f"items[{index}].{name}", getattr(item, name))
            for name in ("code", "description", "quantity", "unit_price", "line_total")
        )

    for name, field in fields:
        if field.value is None or not field.value.strip():
            add("blocking_error", "missing_required_field", f"Campo obrigatório ausente: {name}.")
        if field.page is None or field.evidence is None or not field.evidence.strip():
            add("blocking_error", "missing_evidence", f"Evidência ou página ausente: {name}.")
        elif field.page not in page_texts:
            add("blocking_error", "evidence_page_not_found", f"Página {field.page} não encontrada para {name}.")
        elif not evidence_is_on_page(field.evidence, field.page, page_texts):
            add("blocking_error", "evidence_not_found", f"Evidência de {name} não aparece na página {field.page}.")

    document_number = document.document_number
    if (
        document_number.value is not None
        and document_number.page in page_texts
        and document_number.evidence
        and evidence_is_on_page(document_number.evidence, document_number.page, page_texts)
        and not document_number_evidence_identifies_value(
            document_number.value, document_number.evidence
        )
    ):
        add(
            "blocking_error",
            "document_number_evidence_not_specific",
            "A evidência do número do documento precisa identificar esse valor como número de documento, não como referência de pedido.",
        )

    issue_date = document.issue_date.value
    try:
        if issue_date is None or date.fromisoformat(issue_date).isoformat() != issue_date:
            raise ValueError
    except ValueError:
        add("blocking_error", "invalid_date", "Data de emissão ausente ou inválida; use o formato ISO AAAA-MM-DD.")

    parsed_items: list[dict[str, Decimal | str]] = []
    for index, item in enumerate(document.items, start=1):
        quantity = _decimal(item.quantity.value)
        unit_price = _decimal(item.unit_price.value)
        line_total = _decimal(item.line_total.value)
        if quantity is None:
            add("blocking_error", "invalid_decimal", f"Quantidade inválida no item {index}; informe uma string decimal.")
        elif quantity <= 0:
            add("blocking_error", "non_positive_quantity", f"Quantidade deve ser positiva no item {index}.")
        for name, amount in (("preço unitário", unit_price), ("total", line_total)):
            if amount is None:
                add("blocking_error", "invalid_decimal", f"{name.capitalize()} inválido no item {index}; informe uma string decimal.")
            elif amount < 0:
                add("blocking_error", "negative_money", f"{name.capitalize()} não pode ser negativo no item {index}.")
        if quantity is not None and unit_price is not None and line_total is not None:
            if abs(quantity * unit_price - line_total) > applied_policy.tolerance_for(document.currency.value):
                add("divergence", "line_total_mismatch", f"Quantidade × preço não confere com o total do item {index}.")
            parsed_items.append({
                "code": item.code.value or "",
                "quantity": quantity,
                "unit_price": unit_price,
                "line_total": line_total,
            })

    document_total = _decimal(document.document_total.value)
    if document_total is None:
        add("blocking_error", "invalid_decimal", "Total do documento inválido; informe uma string decimal.")
    elif document_total < 0:
        add("blocking_error", "negative_money", "Total do documento não pode ser negativo.")
    elif len(parsed_items) == len(document.items):
        if abs(sum((item["line_total"] for item in parsed_items), Decimal("0")) - document_total) > applied_policy.tolerance_for(document.currency.value):
            add("divergence", "document_total_mismatch", "A soma dos itens não confere com o total do documento.")

    for name, actual, expected in (
        ("fornecedor", document.supplier_name.value, order.supplier_name),
        ("identificador do fornecedor", document.supplier_identifier.value, order.supplier_identifier),
        ("referência do pedido", document.purchase_order_reference.value, order.reference),
        ("moeda", document.currency.value, order.currency),
    ):
        if actual is not None and actual.strip().casefold() != expected.strip().casefold():
            add("divergence", "order_field_mismatch", f"{name.capitalize()} diverge do pedido cadastrado.")

    document_by_code = {item.code.value: item for item in document.items if item.code.value is not None}
    order_by_code = {item.code: item for item in order.items}
    if set(document_by_code) != set(order_by_code):
        add("divergence", "order_items_mismatch", "Os códigos de itens divergem do pedido cadastrado.")
    for code in set(document_by_code) & set(order_by_code):
        extracted_item = document_by_code[code]
        ordered_item = order_by_code[code]
        quantity = _decimal(extracted_item.quantity.value)
        expected_quantity = _decimal(ordered_item.quantity)
        price = _decimal(extracted_item.unit_price.value)
        expected_price = _decimal(ordered_item.unit_price)
        if expected_quantity is None or expected_price is None or _decimal(ordered_item.line_total) is None:
            add("blocking_error", "invalid_order_decimal", f"Pedido cadastrado tem valor decimal inválido para o item {code}.")
            continue
        if quantity is not None and quantity != expected_quantity:
            add("divergence", "order_quantity_mismatch", f"Quantidade do item {code} diverge do pedido cadastrado.")
        if price is not None and abs(price - expected_price) > TOLERANCE:
            add("divergence", "order_price_mismatch", f"Preço do item {code} diverge do pedido cadastrado.")

    expected_total = _decimal(order.total)
    if expected_total is None:
        add("blocking_error", "invalid_order_decimal", "Total do pedido cadastrado não é uma string decimal válida.")
    elif document_total is not None and abs(document_total - expected_total) > applied_policy.tolerance_for(document.currency.value):
        add("divergence", "order_total_mismatch", "Total do documento diverge do total do pedido cadastrado.")

    for element in document.unsupported_elements:
        add("warning", "unsupported_element", f"Elemento requer revisão: {element}.")

    if any(f.severity == "blocking_error" for f in findings):
        status = "blocked"
    elif any(f.severity == "divergence" for f in findings):
        status = "divergent"
    else:
        status = "ready_for_review"
    return ValidationReport(
        status=status,
        findings=findings,
        commercial_policy=AppliedCommercialPolicy.model_validate(applied_policy.snapshot()),
    )
