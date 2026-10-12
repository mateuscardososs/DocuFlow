"""Contratos estritos para a extração e o pedido cadastrado."""

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class ExtractedField(StrictModel):
    raw_value: str | None
    value: str | None
    page: int | None = Field(gt=0)
    evidence: str | None


class DocumentItem(StrictModel):
    code: ExtractedField
    description: ExtractedField
    quantity: ExtractedField
    unit_price: ExtractedField
    line_total: ExtractedField


class ReferenceDocument(StrictModel):
    supplier_name: ExtractedField
    supplier_identifier: ExtractedField
    document_number: ExtractedField
    issue_date: ExtractedField
    purchase_order_reference: ExtractedField
    currency: ExtractedField
    items: list[DocumentItem]
    document_total: ExtractedField
    unsupported_elements: list[str]


class PurchaseOrderItem(StrictModel):
    code: str
    description: str
    quantity: str
    unit_price: str
    line_total: str


class PurchaseOrder(StrictModel):
    reference: str
    supplier_name: str
    supplier_identifier: str
    currency: str
    items: list[PurchaseOrderItem]
    total: str
