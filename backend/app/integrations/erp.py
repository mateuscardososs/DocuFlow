"""Contrato e adaptador local para simular o cadastro de uma nota fiscal."""

from hashlib import sha256
from typing import Protocol


class ERPClient(Protocol):
    def create_invoice(self, *, idempotency_key: str, document: dict) -> str: ...


class DemoERPClient:
    """Retorna um ID estável por chave, sem conectar a um ERP externo."""

    def create_invoice(self, *, idempotency_key: str, document: dict) -> str:
        digest = sha256(idempotency_key.encode("utf-8")).hexdigest()[:16].upper()
        return f"ERP-DEMO-{digest}"
