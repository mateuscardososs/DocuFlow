"""Testes da prévia e confirmação da importação de pedidos por CSV."""

import unittest
from unittest.mock import patch

from sqlalchemy import event
from sqlalchemy.orm import Session
from sqlalchemy import select

from backend.app.models.identity import User, Workspace, WorkspaceMembership
from backend.app.models.purchase_order import PurchaseOrderImportAuditEntity, PurchaseOrderItem
from backend.tests.test_api import create_client


HEADER = (
    "purchase_order_reference,supplier_identifier,currency,order_total,"
    "item_code,item_description,quantity,unit_price,line_total\n"
)


def csv_bytes(*rows: str, header: str = HEADER, encoding: str = "utf-8") -> bytes:
    return (header + "\n".join(rows) + "\n").encode(encoding)


def send_preview(client, contents: bytes):
    return client.post(
        "/purchase-orders/import/preview",
        files={"file": ("pedidos.csv", contents, "text/csv")},
    )


def send_confirmation(client, contents: bytes):
    return client.post(
        "/purchase-orders/import/confirm",
        files={"file": ("pedidos.csv", contents, "text/csv")},
    )


class PurchaseOrderCsvImportApiTests(unittest.TestCase):
    def test_preview_does_not_persist_and_confirmation_imports_decimal_items(self):
        client, _, _ = create_client(self)
        contents = csv_bytes(
            "PC-CSV-001,FORN-0001,BRL,20.00,ITEM-01,Chapa fina,2.5,8.00,20.00"
        )

        preview = send_preview(client, contents)

        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["valid_rows"], 1)
        self.assertEqual(preview.json()["errors"], [])
        self.assertIsNone(
            client.app.state.order_repository.get_by_reference("PC-CSV-001", 1)
        )
        with client.app.state.order_repository._session_factory() as session:
            self.assertEqual(session.scalar(select(PurchaseOrderImportAuditEntity)), None)

        result = send_confirmation(client, contents)

        self.assertEqual(result.status_code, 200, result.text)
        order = client.app.state.order_repository.get_by_reference("PC-CSV-001", 1)
        self.assertIsNotNone(order)
        self.assertEqual(order.items[0].quantity, "2.500")
        self.assertEqual(order.items[0].unit_price, "8.00")
        with client.app.state.order_repository._session_factory() as session:
            audit = session.scalar(select(PurchaseOrderImportAuditEntity))
            actor = session.scalar(select(User).where(User.email == "operator@example.test"))
            self.assertEqual(audit.actor_user_id, actor.id)
            self.assertEqual(audit.imported_orders, 1)
            self.assertFalse(hasattr(audit, "file_name"))

    def test_accepts_semicolon_bom_and_decimal_comma(self):
        client, _, _ = create_client(self)
        content = (
            "\ufeffpurchase_order_reference;supplier_identifier;currency;order_total;"
            "item_code;item_description;quantity;unit_price;line_total\n"
            "PC-CSV-002;FORN-0001;BRL;20,00;ITEM-02;Chapa fina;2,5;8,00;20,00\n"
        ).encode("utf-8")

        preview = send_preview(client, content)
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["orders"][0]["reference"], "PC-CSV-002")
        imported = send_confirmation(client, content)
        self.assertEqual(imported.status_code, 200, imported.text)
        self.assertEqual(
            client.app.state.order_repository.get_by_reference("PC-CSV-002", 1)
            .items[0].quantity,
            "2.500",
        )

    def test_missing_supplier_and_malformed_order_have_line_errors(self):
        client, _, _ = create_client(self)
        contents = csv_bytes(
            "PC-CSV-003,UNKNOWN,BRL,20.00,ITEM-01,Chapa fina,2,10.00,20.00",
            " ,FORN-0001,BRL,invalid,ITEM-02,Peça,1,10.00,10.00",
        )

        response = send_preview(client, contents)

        self.assertEqual(response.status_code, 200, response.text)
        errors = response.json()["errors"]
        self.assertEqual([error["line_number"] for error in errors], [2, 3])
        self.assertIn("fornecedor", errors[0]["reason"].lower())
        self.assertIn("referência", errors[1]["reason"].lower())

    def test_extra_columns_on_blank_row_return_a_line_error(self):
        client, _, _ = create_client(self)
        extra_column_row = ",".join([""] * 9 + ["valor inesperado"])

        response = send_preview(client, csv_bytes(extra_column_row))

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["errors"][0]["line_number"], 2)
        self.assertIn("mais colunas", response.json()["errors"][0]["reason"])

    def test_error_line_number_counts_physical_lines_after_quoted_newline(self):
        client, _, _ = create_client(self)
        content = (
            HEADER
            + 'PC-CSV-MULTI,FORN-0001,BRL,10.00,ITEM-1,"Descrição em duas linhas\ncontinuação",1,10.00,10.00\n'
            + ',FORN-0001,BRL,10.00,ITEM-2,Sem referência,1,10.00,10.00\n'
        ).encode("utf-8")

        response = send_preview(client, content)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["errors"][0]["line_number"], 4)

    def test_exact_duplicate_is_ignored_but_changed_reference_is_conflict(self):
        client, _, _ = create_client(self)
        identical = csv_bytes(
            "PC-2026-0001,FORN-0001,BRL,1250.00,ARR-001,Arruela zincada M8,10,85.50,855.00",
            "PC-2026-0001,FORN-0001,BRL,1250.00,PAR-002,Parafuso sextavado M8,5,79.00,395.00",
        )

        duplicate = send_confirmation(client, identical)

        self.assertEqual(duplicate.status_code, 200, duplicate.text)
        self.assertEqual(duplicate.json()["ignored_duplicates"], ["PC-2026-0001"])
        changed = csv_bytes(
            "PC-2026-0001,FORN-0001,BRL,20.00,ITEM-X,Peça diferente,2,10.00,20.00"
        )
        conflict = send_confirmation(client, changed)
        self.assertEqual(conflict.status_code, 409)
        self.assertIn("PC-2026-0001", conflict.json()["detail"])

    def test_item_database_failure_rolls_back_order(self):
        client, _, _ = create_client(self)
        contents = csv_bytes(
            "PC-CSV-ROLLBACK,FORN-0001,BRL,20.00,FAIL-ITEM,Chapa fina,2,10.00,20.00"
        )
        session_factory = client.app.state.order_repository._session_factory

        def fail_before_flush(session, _flush_context, _instances):
            if any(
                isinstance(item, PurchaseOrderItem) and item.code == "FAIL-ITEM"
                for item in session.new
            ):
                raise RuntimeError("simulated persistence failure")

        event.listen(Session, "before_flush", fail_before_flush)
        self.addCleanup(event.remove, Session, "before_flush", fail_before_flush)
        response = send_confirmation(client, contents)

        self.assertEqual(response.status_code, 503, response.text)
        self.assertIsNone(
            client.app.state.order_repository.get_by_reference("PC-CSV-ROLLBACK", 1)
        )

    def test_workspace_without_operator_cannot_preview_or_confirm(self):
        client, _, _ = create_client(self)
        # The route must require the existing operator role even for a read-only preview.
        with patch.object(client.app.state.authentication, "roles_for", return_value=["approver"]):
            content = csv_bytes(
                "PC-CSV-004,FORN-0001,BRL,10.00,ITEM-01,Peça,1,10.00,10.00"
            )
            preview = send_preview(client, content)
            confirm = send_confirmation(client, content)
        self.assertEqual(preview.status_code, 403)
        self.assertEqual(confirm.status_code, 403)

    def test_preview_and_confirmation_require_authentication(self):
        client, _, _ = create_client(self)
        client.headers.pop("Authorization")
        contents = csv_bytes(
            "PC-CSV-AUTH,FORN-0001,BRL,10.00,ITEM-01,Peça,1,10.00,10.00"
        )

        preview = send_preview(client, contents)
        confirmation = send_confirmation(client, contents)

        self.assertEqual(preview.status_code, 401)
        self.assertEqual(confirmation.status_code, 401)

    def test_supplier_and_order_from_another_workspace_are_not_visible(self):
        client, _, _ = create_client(self)
        repository = client.app.state.order_repository
        sessions = repository._session_factory
        with sessions.begin() as session:
            other_workspace = Workspace(slug="csv-isolation", name="Isolamento CSV")
            session.add(other_workspace)
            session.flush()
            operator = session.scalar(select(User).where(User.email == "operator@example.test"))
            session.add(WorkspaceMembership(
                user_id=operator.id, workspace_id=other_workspace.id, role="operator"
            ))
            other_workspace_id = other_workspace.id
        client.headers["X-Workspace-ID"] = str(other_workspace_id)
        contents = csv_bytes(
            "PC-CSV-ISOLATION,FORN-0001,BRL,20.00,ITEM-01,Peça,2,10.00,20.00"
        )

        response = send_preview(client, contents)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["orders"], [])
        self.assertIn("workspace", response.json()["errors"][0]["reason"].lower())
        self.assertIsNone(repository.get_by_reference("PC-2026-0001", other_workspace_id))


if __name__ == "__main__":
    unittest.main()
