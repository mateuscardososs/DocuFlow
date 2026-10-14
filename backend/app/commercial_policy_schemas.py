"""Contratos estritos da configuração de tolerâncias por workspace."""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from backend.app.commercial_policy import normalize_currency_tolerances


class CommercialPolicyUpdate(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    expected_version: int = Field(ge=0)
    currency_tolerances: dict[Annotated[str, StrictStr], StrictStr] = Field(max_length=20)

    @field_validator("currency_tolerances")
    @classmethod
    def validate_tolerances(cls, values: dict[str, str]) -> dict[str, str]:
        return normalize_currency_tolerances(values)


class CommercialPolicyView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    default_total_tolerance: str
    currency_tolerances: dict[str, str]
    created_by_user_id: int | None
    created_by_email: str | None
    created_at: datetime | None
