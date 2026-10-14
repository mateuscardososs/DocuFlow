"""Contratos públicos da prévia e da confirmação da importação CSV."""

from typing import Literal

from pydantic import Field

from backend.app.schemas import StrictModel


class PurchaseOrderImportLineError(StrictModel):
    line_number: int = Field(ge=2)
    reason: str


class PurchaseOrderImportPreviewOrder(StrictModel):
    reference: str
    supplier_identifier: str
    supplier_name: str
    currency: str
    total: str
    item_count: int = Field(ge=1)
    line_numbers: list[int]
    outcome: Literal["new", "duplicate", "conflict"]


class PurchaseOrderImportPreview(StrictModel):
    total_rows: int = Field(ge=0)
    valid_rows: int = Field(ge=0)
    orders: list[PurchaseOrderImportPreviewOrder]
    errors: list[PurchaseOrderImportLineError]


class PurchaseOrderImportConfirmation(StrictModel):
    total_rows: int = Field(ge=0)
    valid_rows: int = Field(ge=0)
    imported: list[str]
    ignored_duplicates: list[str]
    errors: list[PurchaseOrderImportLineError]
