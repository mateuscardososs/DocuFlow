"""Cálculos determinísticos para avaliar saídas do extrator contra gabaritos."""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from backend.app.evidence import normalize_spaces


_DECIMAL_FIELDS = {"quantity", "unit_price", "line_total", "document_total"}
_DECIMAL_PATTERN = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
_TOP_LEVEL_FIELDS = (
    "supplier_name", "supplier_identifier", "document_number", "issue_date",
    "purchase_order_reference", "currency", "document_total",
)
_ITEM_FIELDS = ("code", "description", "quantity", "unit_price", "line_total")


def flatten_fields(document: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """Retorna cada campo do documento com um caminho estável para relatórios."""
    fields: dict[str, Mapping[str, Any]] = {}
    for name in _TOP_LEVEL_FIELDS:
        value = document.get(name)
        if isinstance(value, Mapping):
            fields[name] = value
    items = document.get("items")
    if isinstance(items, Sequence) and not isinstance(items, (str, bytes)):
        for index, item in enumerate(items, start=1):
            if not isinstance(item, Mapping):
                continue
            for name in _ITEM_FIELDS:
                value = item.get(name)
                if isinstance(value, Mapping):
                    fields[f"items[{index}].{name}"] = value
    return fields


def _normalise(path: str, value: Any) -> str | Decimal | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    if path.rsplit(".", 1)[-1] in _DECIMAL_FIELDS:
        if not _DECIMAL_PATTERN.fullmatch(value):
            return f"invalid:{value}"
        try:
            return Decimal(value)
        except InvalidOperation:
            return f"invalid:{value}"
    normalized = unicodedata.normalize("NFKC", value)
    normalized = re.sub(r"\s+", " ", normalized).strip().casefold()
    return normalized


def _is_absent(field: Mapping[str, Any] | None) -> bool:
    if field is None:
        return True
    return all(field.get(key) is None for key in ("raw_value", "value", "page", "evidence"))


def evaluate_case(
    expected_document: Mapping[str, Any],
    actual_document: Mapping[str, Any],
    *,
    absent_fields: Sequence[str],
    page_texts: Mapping[int, str],
    expected_divergence_codes: Sequence[str],
    observed_divergence_codes: Sequence[str],
) -> dict[str, Any]:
    """Compara valores e evidências sem confundir extração com validade comercial."""
    expected = flatten_fields(expected_document)
    actual = flatten_fields(actual_document)
    absent = set(absent_fields)
    all_names = list(dict.fromkeys([*expected, *actual]))
    fields: dict[str, dict[str, Any]] = {}
    value_correct = value_total = 0
    absent_correct = evidence_valid = evidence_total = 0

    for name in all_names:
        expected_field = expected.get(name)
        actual_field = actual.get(name)
        expected_value = expected_field.get("value") if expected_field else None
        actual_value = actual_field.get("value") if actual_field else None
        is_expected_absent = name in absent

        if is_expected_absent:
            correctly_absent = _is_absent(actual_field)
            absent_correct += int(correctly_absent)
            value_match = None
        else:
            correctly_absent = None
            value_total += 1
            value_match = (
                expected_value is not None
                and actual_value is not None
                and _normalise(name, expected_value) == _normalise(name, actual_value)
            )
            value_correct += int(value_match)

        page = actual_field.get("page") if actual_field else None
        evidence = actual_field.get("evidence") if actual_field else None
        has_actual_value = actual_value is not None and str(actual_value).strip() != ""
        evidence_is_valid = None
        if not is_expected_absent:
            evidence_total += 1
            evidence_is_valid = (
                has_actual_value
                and isinstance(page, int)
                and page in page_texts
                and isinstance(evidence, str)
                and bool(evidence.strip())
                and normalize_spaces(evidence) in normalize_spaces(page_texts[page])
            )
            evidence_valid += int(evidence_is_valid)

        fields[name] = {
            "expected_raw_value": expected_field.get("raw_value") if expected_field else None,
            "expected_value": expected_value,
            "extracted_raw_value": actual_field.get("raw_value") if actual_field else None,
            "extracted_value": actual_value,
            "expected_absent": is_expected_absent,
            "value_match": value_match,
            "correctly_absent": correctly_absent,
            "evidence_page": page,
            "evidence": evidence,
            "evidence_valid": evidence_is_valid,
        }

    expected_codes = sorted(expected_divergence_codes)
    observed_codes = sorted(observed_divergence_codes)
    return {
        "fields": fields,
        "field_accuracy": _ratio(value_correct, value_total),
        "absence_recognition": _ratio(absent_correct, len(absent)),
        "evidence": _ratio(evidence_valid, evidence_total, numerator_name="valid"),
        "divergences": {
            "expected_codes": expected_codes,
            "observed_codes": observed_codes,
            "match": expected_codes == observed_codes,
        },
    }


def aggregate_metrics(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Agrega taxas somente dos casos processados e mantém falhas nos denominadores de amostra."""
    input_groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    document_groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for case in cases:
        input_groups[str(case.get("input_type", "unknown"))].append(case)
        document_groups[str(case.get("document_type", "unknown"))].append(case)

    return {
        "overall": _aggregate_group(cases),
        "by_input_type": {
            key: _aggregate_group(value) for key, value in input_groups.items()
        },
        "by_document_type": {
            key: _aggregate_group(value) for key, value in document_groups.items()
        },
    }


def _ratio(correct: int, total: int, *, numerator_name: str = "correct") -> dict[str, Any]:
    return {
        numerator_name: correct,
        "total": total,
        "rate": correct / total if total else None,
    }


def _aggregate_group(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    field_correct = field_total = absent_correct = absent_total = evidence_valid = evidence_total = 0
    processed = [case for case in cases if isinstance(case.get("metrics"), Mapping)]
    for case in processed:
        metrics = case["metrics"]
        field_correct += metrics["field_accuracy"]["correct"]
        field_total += metrics["field_accuracy"]["total"]
        absent_correct += metrics["absence_recognition"]["correct"]
        absent_total += metrics["absence_recognition"]["total"]
        evidence_valid += metrics["evidence"]["valid"]
        evidence_total += metrics["evidence"]["total"]
    return {
        "cases": len(processed),
        "cases_seen": len(cases),
        "processing_failures": len(cases) - len(processed),
        "field_accuracy": _ratio(field_correct, field_total),
        "absence_recognition": _ratio(absent_correct, absent_total),
        "evidence": _ratio(evidence_valid, evidence_total, numerator_name="valid"),
    }
