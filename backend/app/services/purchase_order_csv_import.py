"""Leitura determinística de CSV para os schemas de pedido já usados pelo DocuFlow."""

import csv
import io
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from backend.app.purchase_order_import_schemas import (
    PurchaseOrderImportLineError,
    PurchaseOrderImportPreview,
    PurchaseOrderImportPreviewOrder,
)
from backend.app.schemas import PurchaseOrder, PurchaseOrderItem


CSV_MAX_BYTES = 5 * 1024 * 1024
CSV_HEADERS = (
    "purchase_order_reference",
    "supplier_identifier",
    "currency",
    "order_total",
    "item_code",
    "item_description",
    "quantity",
    "unit_price",
    "line_total",
)


class PurchaseOrderCsvError(ValueError):
    """Erro seguro de arquivo CSV que pode ser mostrado ao operador."""


class PurchaseOrderImportConflict(Exception):
    def __init__(self, reference: str) -> None:
        self.reference = reference
        super().__init__(f"O pedido {reference} já existe com dados diferentes.")


class PurchaseOrderImportPersistenceError(Exception):
    pass


@dataclass(frozen=True)
class ParsedPurchaseOrder:
    order: PurchaseOrder
    line_numbers: tuple[int, ...]


@dataclass(frozen=True)
class ParsedPurchaseOrderCsv:
    total_rows: int
    valid_rows: int
    orders: tuple[ParsedPurchaseOrder, ...]
    errors: tuple[PurchaseOrderImportLineError, ...]


class PurchaseOrderCsvImportService:
    def __init__(self, repository) -> None:
        self._repository = repository

    def preview(self, content: bytes, workspace_id: int) -> PurchaseOrderImportPreview:
        _, preview = self.analyze(content, workspace_id)
        return preview

    def analyze(
        self, content: bytes, workspace_id: int
    ) -> tuple[ParsedPurchaseOrderCsv, PurchaseOrderImportPreview]:
        parsed = parse_purchase_order_csv(content)
        errors = sorted(parsed.errors, key=lambda error: error.line_number)
        preview_orders: list[PurchaseOrderImportPreviewOrder] = []
        accepted_orders: list[ParsedPurchaseOrder] = []
        accepted_rows = 0
        for parsed_order in parsed.orders:
            order = parsed_order.order
            supplier_name = self._repository.get_supplier_name_by_identifier(
                order.supplier_identifier, workspace_id
            )
            if supplier_name is None:
                errors.extend(
                    PurchaseOrderImportLineError(
                        line_number=line,
                        reason=f"Fornecedor {order.supplier_identifier} não existe neste workspace.",
                    )
                    for line in parsed_order.line_numbers
                )
                continue
            order = order.model_copy(update={"supplier_name": supplier_name})
            stored = self._repository.get_by_reference(order.reference, workspace_id)
            outcome = "new"
            if stored is not None:
                outcome = "duplicate" if purchase_orders_equal(order, stored) else "conflict"
            preview_orders.append(
                PurchaseOrderImportPreviewOrder(
                    reference=order.reference,
                    supplier_identifier=order.supplier_identifier,
                    supplier_name=supplier_name,
                    currency=order.currency,
                    total=order.total,
                    item_count=len(order.items),
                    line_numbers=list(parsed_order.line_numbers),
                    outcome=outcome,
                )
            )
            if outcome == "conflict":
                errors.extend(
                    PurchaseOrderImportLineError(
                        line_number=line,
                        reason=f"O pedido {order.reference} já existe com dados diferentes.",
                    )
                    for line in parsed_order.line_numbers
                )
            accepted_orders.append(ParsedPurchaseOrder(order, parsed_order.line_numbers))
            if outcome != "conflict":
                accepted_rows += len(parsed_order.line_numbers)
        analyzed = ParsedPurchaseOrderCsv(
            total_rows=parsed.total_rows,
            valid_rows=accepted_rows,
            orders=tuple(accepted_orders),
            errors=tuple(errors),
        )
        preview = PurchaseOrderImportPreview(
            total_rows=parsed.total_rows,
            valid_rows=accepted_rows,
            orders=preview_orders,
            errors=sorted(errors, key=lambda error: error.line_number),
        )
        return analyzed, preview


def parse_purchase_order_csv(content: bytes) -> ParsedPurchaseOrderCsv:
    if not content:
        raise PurchaseOrderCsvError("O arquivo CSV está vazio.")
    if len(content) > CSV_MAX_BYTES:
        raise PurchaseOrderCsvError("O arquivo CSV excede o limite de 5 MB.")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise PurchaseOrderCsvError("O arquivo precisa estar codificado em UTF-8.") from None
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    delimiter = ";" if first_line.count(";") > first_line.count(",") else ","
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
        headers = tuple((header or "").strip() for header in (reader.fieldnames or ()))
        if len(headers) != len(set(headers)) or set(headers) != set(CSV_HEADERS):
            expected = ", ".join(CSV_HEADERS)
            raise PurchaseOrderCsvError(f"Cabeçalho CSV inválido. Colunas esperadas: {expected}.")
        reader.fieldnames = list(headers)
        buckets: dict[str, dict] = {}
        row_errors: list[PurchaseOrderImportLineError] = []
        invalid_references: set[str] = set()
        total_rows = 0
        valid_rows = 0
        previous_line_number = reader.line_num
        for raw in reader:
            # Conta a linha física de início, mesmo se a descrição tiver quebras de linha.
            line_number = previous_line_number + 1
            previous_line_number = reader.line_num
            if raw is None or all(not (value or "").strip() for value in raw.values()):
                continue
            total_rows += 1
            if None in raw:
                row_errors.append(PurchaseOrderImportLineError(
                    line_number=line_number, reason="A linha possui mais colunas do que o cabeçalho CSV."
                ))
                reference = (raw.get("purchase_order_reference") or "").strip()
                if reference:
                    invalid_references.add(reference)
                continue
            values = {key: (value or "").strip() for key, value in raw.items() if key is not None}
            try:
                row = _parse_row(values)
            except ValueError as error:
                row_errors.append(PurchaseOrderImportLineError(line_number=line_number, reason=str(error)))
                reference = values.get("purchase_order_reference", "").strip()
                if reference:
                    invalid_references.add(reference)
                continue
            valid_rows += 1
            reference = row["reference"]
            group = buckets.setdefault(
                reference,
                {"supplier_identifier": row["supplier_identifier"], "currency": row["currency"],
                 "total": row["total"], "items": [], "line_numbers": [], "bad": False},
            )
            group["line_numbers"].append(line_number)
            if any(group[key] != row[key] for key in ("supplier_identifier", "currency", "total")):
                group["bad"] = True
                row_errors.append(PurchaseOrderImportLineError(
                    line_number=line_number,
                    reason=f"Os dados gerais do pedido {reference} diferem entre suas linhas.",
                ))
                continue
            if any(item.code == row["item"].code for item in group["items"]):
                group["bad"] = True
                row_errors.append(PurchaseOrderImportLineError(
                    line_number=line_number,
                    reason=f"O item {row['item'].code} aparece mais de uma vez no pedido {reference}.",
                ))
                continue
            group["items"].append(row["item"])
        orders: list[ParsedPurchaseOrder] = []
        for reference, group in buckets.items():
            if reference in invalid_references:
                row_errors.extend(PurchaseOrderImportLineError(
                    line_number=line,
                    reason=f"O pedido {reference} contém outra linha inválida e não será importado.",
                ) for line in group["line_numbers"])
                continue
            if group["bad"]:
                row_errors.extend(PurchaseOrderImportLineError(
                    line_number=line,
                    reason=f"O pedido {reference} contém linhas inconsistentes e não será importado.",
                ) for line in group["line_numbers"])
                continue
            if not group["items"]:
                continue
            orders.append(ParsedPurchaseOrder(
                order=PurchaseOrder(
                    reference=reference,
                    supplier_name="",  # preenchido do cadastro do workspace na confirmação
                    supplier_identifier=group["supplier_identifier"],
                    currency=group["currency"],
                    items=group["items"],
                    total=_decimal_string(group["total"]),
                ),
                line_numbers=tuple(group["line_numbers"]),
            ))
        return ParsedPurchaseOrderCsv(total_rows, valid_rows, tuple(orders), tuple(row_errors))
    except csv.Error:
        raise PurchaseOrderCsvError("O CSV contém uma linha ou aspas malformadas.") from None


def _parse_row(values: dict[str, str]) -> dict:
    reference = values.get("purchase_order_reference", "").strip()
    supplier_identifier = values.get("supplier_identifier", "").strip()
    currency = values.get("currency", "").strip().upper()
    code = values.get("item_code", "").strip()
    description = values.get("item_description", "").strip()
    if not reference or len(reference) > 100:
        raise ValueError("Referência do pedido ausente ou acima de 100 caracteres.")
    if not supplier_identifier or len(supplier_identifier) > 100:
        raise ValueError("Identificador do fornecedor ausente ou inválido.")
    if len(currency) != 3 or not currency.isalpha():
        raise ValueError("Moeda inválida: informe um código de três letras, como BRL.")
    if not code or len(code) > 100 or not description or len(description) > 500:
        raise ValueError("Código ou descrição do item ausente ou acima do limite permitido.")
    total = _decimal(values.get("order_total", ""), "total do pedido", scale=2, positive=False)
    quantity = _decimal(values.get("quantity", ""), "quantidade", scale=3, positive=True)
    unit_price = _decimal(values.get("unit_price", ""), "preço unitário", scale=2, positive=False)
    line_total = _decimal(values.get("line_total", ""), "total do item", scale=2, positive=False)
    item = PurchaseOrderItem(
        code=code,
        description=description,
        quantity=_decimal_string(quantity),
        unit_price=_decimal_string(unit_price),
        line_total=_decimal_string(line_total),
    )
    return {"reference": reference, "supplier_identifier": supplier_identifier,
            "currency": currency, "total": total, "item": item}


def _decimal(value: str, label: str, *, scale: int, positive: bool) -> Decimal:
    raw = value.strip().replace(" ", "")
    if not raw:
        raise ValueError(f"{label.capitalize()} ausente.")
    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    else:
        raw = raw.replace(",", ".")
    try:
        number = Decimal(raw)
    except InvalidOperation:
        raise ValueError(f"{label.capitalize()} inválido: use um número decimal.") from None
    if not number.is_finite():
        raise ValueError(f"{label.capitalize()} inválido: use um número decimal finito.")
    if number < 0 or (positive and number <= 0):
        adjective = "positiva" if positive else "não negativo"
        raise ValueError(f"{label.capitalize()} precisa ser {adjective}.")
    if len(number.as_tuple().digits) > 14 or (
        number != 0 and number.adjusted() >= 14 - scale
    ):
        raise ValueError(f"{label.capitalize()} excede o limite numérico permitido.")
    try:
        rounded = number.quantize(Decimal(1).scaleb(-scale))
    except InvalidOperation:
        raise ValueError(f"{label.capitalize()} excede o limite numérico permitido.") from None
    if number != rounded:
        raise ValueError(f"{label.capitalize()} aceita no máximo {scale} casas decimais.")
    return number


def _decimal_string(number: Decimal) -> str:
    return format(number, "f")


def purchase_orders_equal(left: PurchaseOrder, right: PurchaseOrder) -> bool:
    def item_map(order: PurchaseOrder) -> dict[str, tuple[str, Decimal, Decimal, Decimal]]:
        return {
            item.code: (
                item.description,
                Decimal(item.quantity),
                Decimal(item.unit_price),
                Decimal(item.line_total),
            )
            for item in order.items
        }

    return (
        left.reference == right.reference
        and left.supplier_identifier == right.supplier_identifier
        and left.supplier_name == right.supplier_name
        and left.currency == right.currency
        and Decimal(left.total) == Decimal(right.total)
        and item_map(left) == item_map(right)
    )
