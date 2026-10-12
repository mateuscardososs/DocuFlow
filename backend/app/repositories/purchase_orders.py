"""Consulta de pedidos; o adaptador em memória é apenas para a demonstração."""

from typing import Protocol

from backend.app.schemas import PurchaseOrder


class PurchaseOrderRepository(Protocol):
    def get_by_reference(self, reference: str) -> PurchaseOrder | None: ...


class InMemoryPurchaseOrderRepository:
    """Pedido fictício de docs/01-fluxo-minimo.md.

    Quando houver PostgreSQL, substitua este adaptador por uma implementação
    desta interface; as rotas não precisam conhecer a origem dos pedidos.
    """

    def __init__(self) -> None:
        order = PurchaseOrder.model_validate(
            {
                "reference": "PC-2026-0001",
                "supplier_name": "Metalúrgica Horizonte Ltda.",
                "supplier_identifier": "FORN-0001",
                "currency": "BRL",
                "items": [
                    {
                        "code": "ARR-001",
                        "description": "Arruela zincada M8",
                        "quantity": "10",
                        "unit_price": "85.50",
                        "line_total": "855.00",
                    },
                    {
                        "code": "PAR-002",
                        "description": "Parafuso sextavado M8",
                        "quantity": "5",
                        "unit_price": "79.00",
                        "line_total": "395.00",
                    },
                ],
                "total": "1250.00",
            }
        )
        self._orders = {order.reference: order}

    def get_by_reference(self, reference: str) -> PurchaseOrder | None:
        return self._orders.get(reference)
