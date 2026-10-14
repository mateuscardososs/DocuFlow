"""Política comercial imutável aplicada a uma validação."""

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Mapping


DEFAULT_TOTAL_TOLERANCE = Decimal("0.01")
MAX_TOTAL_TOLERANCE = Decimal("100.00")
_CURRENCY_CODE = re.compile(r"^[A-Z]{3}$")
_DECIMAL_VALUE = re.compile(r"^\d+(?:\.\d{1,2})?$")


def normalize_currency_tolerances(values: Mapping[str, str]) -> dict[str, str]:
    if len(values) > 20:
        raise ValueError("Configure no máximo 20 moedas.")
    normalized: dict[str, str] = {}
    for currency, raw_amount in values.items():
        if not _CURRENCY_CODE.fullmatch(currency):
            raise ValueError("Use códigos de moeda com três letras maiúsculas, como BRL.")
        if not isinstance(raw_amount, str) or not _DECIMAL_VALUE.fullmatch(raw_amount):
            raise ValueError("A tolerância deve ser uma string decimal com até duas casas decimais.")
        try:
            amount = Decimal(raw_amount)
        except InvalidOperation:
            raise ValueError("A tolerância informada é inválida.") from None
        if amount > MAX_TOTAL_TOLERANCE:
            raise ValueError("A tolerância máxima é 100.00.")
        normalized[currency] = format(amount.quantize(Decimal("0.01")), ".2f")
    return dict(sorted(normalized.items()))


@dataclass(frozen=True)
class CommercialPolicy:
    version: int = 0
    currency_tolerances: Mapping[str, str] = field(default_factory=dict)
    default_total_tolerance: str = "0.01"

    def __post_init__(self) -> None:
        if self.version < 0:
            raise ValueError("A versão da política não pode ser negativa.")
        values = normalize_currency_tolerances(self.currency_tolerances)
        object.__setattr__(self, "currency_tolerances", MappingProxyType(values))
        default = normalize_currency_tolerances({"BRL": self.default_total_tolerance})["BRL"]
        object.__setattr__(self, "default_total_tolerance", default)

    @classmethod
    def defaults(cls) -> "CommercialPolicy":
        return cls()

    def tolerance_for(self, currency: str | None) -> Decimal:
        value = self.currency_tolerances.get(currency or "", self.default_total_tolerance)
        return Decimal(value)

    def snapshot(self) -> dict[str, object]:
        return {
            "version": self.version,
            "default_total_tolerance": self.default_total_tolerance,
            "currency_tolerances": dict(self.currency_tolerances),
        }

    @classmethod
    def from_snapshot(cls, value: object) -> "CommercialPolicy":
        if not isinstance(value, dict):
            return cls.defaults()
        return cls(
            version=int(value.get("version", 0)),
            default_total_tolerance=str(value.get("default_total_tolerance", "0.01")),
            currency_tolerances=value.get("currency_tolerances", {}),
        )
