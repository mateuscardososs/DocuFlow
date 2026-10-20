"""Contrato compartilhado do pedido fictício usado no seed e nas amostras."""

from backend.app.schemas import PurchaseOrder, PurchaseOrderItem


REFERENCE_PURCHASE_ORDER = PurchaseOrder(
    reference="PC-2026-0001",
    supplier_name="Metalúrgica Horizonte Ltda.",
    supplier_identifier="FORN-0001",
    currency="BRL",
    items=[
        PurchaseOrderItem(
            code="ARR-001",
            description="Arruela zincada M8",
            quantity="10",
            unit_price="85.50",
            line_total="855.00",
        ),
        PurchaseOrderItem(
            code="PAR-002",
            description="Parafuso sextavado M8",
            quantity="5",
            unit_price="79.00",
            line_total="395.00",
        ),
    ],
    total="1250.00",
)
