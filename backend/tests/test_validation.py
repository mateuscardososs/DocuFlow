import unittest

from pydantic import ValidationError

from backend.app.commercial_policy import CommercialPolicy
from backend.app.schemas import PurchaseOrder, ReferenceDocument
from backend.app.validation import validate_document


def extracted(raw_value, value, evidence, page=1):
    return {
        "raw_value": raw_value,
        "value": value,
        "page": page,
        "evidence": evidence,
    }


def reference_document():
    return {
        "supplier_name": extracted("METALÚRGICA HORIZONTE LTDA.", "Metalúrgica Horizonte Ltda.", "METALÚRGICA HORIZONTE LTDA."),
        "supplier_identifier": extracted("FORN-0001", "FORN-0001", "Identificador: FORN-0001"),
        "document_number": extracted("DOC-2026-1042", "DOC-2026-1042", "Documento comercial: DOC-2026-1042"),
        "issue_date": extracted("10/10/2026", "2026-10-10", "Data de emissão: 10/10/2026"),
        "purchase_order_reference": extracted("PC-2026-0001", "PC-2026-0001", "Pedido de compra: PC-2026-0001"),
        "currency": extracted("BRL", "BRL", "Moeda: BRL"),
        "items": [
            {
                "code": extracted("ARR-001", "ARR-001", "ARR-001"),
                "description": extracted("Arruela zincada M8", "Arruela zincada M8", "Arruela zincada M8"),
                "quantity": extracted("10", "10", "10"),
                "unit_price": extracted("R$ 85,50", "85.50", "R$ 85,50"),
                "line_total": extracted("R$ 855,00", "855.00", "R$ 855,00"),
            },
            {
                "code": extracted("PAR-002", "PAR-002", "PAR-002"),
                "description": extracted("Parafuso sextavado M8", "Parafuso sextavado M8", "Parafuso sextavado M8"),
                "quantity": extracted("5", "5", "5"),
                "unit_price": extracted("R$ 79,00", "79.00", "R$ 79,00"),
                "line_total": extracted("R$ 395,00", "395.00", "R$ 395,00"),
            },
        ],
        "document_total": extracted("R$ 1.250,00", "1250.00", "Total do documento: R$ 1.250,00"),
        "unsupported_elements": [],
    }


def purchase_order():
    return {
        "reference": "PC-2026-0001",
        "supplier_name": "Metalúrgica Horizonte Ltda.",
        "supplier_identifier": "FORN-0001",
        "currency": "BRL",
        "items": [
            {"code": "ARR-001", "description": "Arruela zincada M8", "quantity": "10", "unit_price": "85.50", "line_total": "855.00"},
            {"code": "PAR-002", "description": "Parafuso sextavado M8", "quantity": "5", "unit_price": "79.00", "line_total": "395.00"},
        ],
        "total": "1250.00",
    }


PAGE_TEXT = {
    1: "METALÚRGICA HORIZONTE LTDA. Identificador: FORN-0001 Documento comercial: DOC-2026-1042 "
    "Data de emissão: 10/10/2026 Pedido de compra: PC-2026-0001 Moeda: BRL "
    "ARR-001 Arruela zincada M8 10 R$ 85,50 R$ 855,00 "
    "PAR-002 Parafuso sextavado M8 5 R$ 79,00 R$ 395,00 Total do documento: R$ 1.250,00"
}


class ValidationTests(unittest.TestCase):
    def validate(self, document=None, order=None, pages=None):
        return validate_document(
            ReferenceDocument.model_validate(document or reference_document()),
            PurchaseOrder.model_validate(order or purchase_order()),
            pages or PAGE_TEXT,
        )

    def test_reference_case_is_ready_for_human_review(self):
        report = self.validate()
        self.assertEqual(report.status, "ready_for_review")
        self.assertEqual(report.findings, [])

    def test_missing_required_field_is_blocking(self):
        data = reference_document()
        data["document_number"] = extracted(None, None, None, None)
        report = self.validate(data)
        self.assertEqual(report.status, "blocked")
        self.assertTrue(any("document_number" in finding.message for finding in report.findings))

    def test_evidence_not_found_on_indicated_page_is_blocking(self):
        data = reference_document()
        data["supplier_name"]["evidence"] = "Texto que não aparece"
        report = self.validate(data)
        self.assertEqual(report.status, "blocked")
        self.assertTrue(any(finding.code == "evidence_not_found" for finding in report.findings))

    def test_invalid_date_is_blocking(self):
        data = reference_document()
        data["issue_date"]["value"] = "2026-02-30"
        report = self.validate(data)
        self.assertEqual(report.status, "blocked")
        self.assertTrue(any(finding.code == "invalid_date" for finding in report.findings))

    def test_malformed_money_is_blocking(self):
        data = reference_document()
        data["items"][0]["unit_price"]["value"] = "R$ 85,50"
        report = self.validate(data)
        self.assertEqual(report.status, "blocked")
        self.assertTrue(any(finding.code == "invalid_decimal" for finding in report.findings))

    def test_incorrect_line_total_is_divergence(self):
        data = reference_document()
        data["items"][0]["line_total"]["value"] = "854.00"
        report = self.validate(data)
        self.assertEqual(report.status, "divergent")
        self.assertTrue(any(finding.code == "line_total_mismatch" for finding in report.findings))

    def test_incorrect_document_total_is_divergence(self):
        data = reference_document()
        data["document_total"]["value"] = "1251.00"
        report = self.validate(data)
        self.assertEqual(report.status, "divergent")
        self.assertTrue(any(finding.code == "document_total_mismatch" for finding in report.findings))

    def test_money_difference_of_one_cent_is_within_tolerance(self):
        data = reference_document()
        data["items"][0]["line_total"]["value"] = "855.01"
        data["document_total"]["value"] = "1250.01"
        report = self.validate(data)
        self.assertEqual(report.status, "ready_for_review")
        self.assertEqual(report.findings, [])

    def test_configured_currency_tolerance_applies_to_total_checks_and_is_snapshotted(self):
        data = reference_document()
        data["items"][0]["line_total"]["value"] = "855.04"
        data["document_total"]["value"] = "1250.04"
        policy = CommercialPolicy(
            version=4,
            currency_tolerances={"BRL": "0.05", "USD": "0.02"},
        )

        report = validate_document(
            ReferenceDocument.model_validate(data),
            PurchaseOrder.model_validate(purchase_order()),
            PAGE_TEXT,
            commercial_policy=policy,
        )

        self.assertEqual(report.status, "ready_for_review")
        self.assertEqual(report.commercial_policy.version, 4)
        self.assertEqual(report.commercial_policy.currency_tolerances["BRL"], "0.05")

    def test_currency_without_override_keeps_default_tolerance(self):
        data = reference_document()
        data["items"][0]["line_total"]["value"] = "855.03"
        data["document_total"]["value"] = "1250.03"
        policy = CommercialPolicy(version=2, currency_tolerances={"USD": "0.10"})

        report = validate_document(
            ReferenceDocument.model_validate(data),
            PurchaseOrder.model_validate(purchase_order()),
            PAGE_TEXT,
            commercial_policy=policy,
        )

        self.assertEqual(report.status, "divergent")
        self.assertEqual(report.commercial_policy.version, 2)
        self.assertTrue(any(f.code == "line_total_mismatch" for f in report.findings))

    def test_currency_policy_does_not_change_fixed_unit_price_comparison(self):
        data = reference_document()
        data["items"][0]["unit_price"]["value"] = "85.53"
        data["items"][0]["line_total"]["value"] = "855.30"
        data["document_total"]["value"] = "1250.30"
        policy = CommercialPolicy(version=1, currency_tolerances={"BRL": "0.50"})

        report = validate_document(
            ReferenceDocument.model_validate(data),
            PurchaseOrder.model_validate(purchase_order()),
            PAGE_TEXT,
            commercial_policy=policy,
        )

        self.assertEqual(report.status, "divergent")
        self.assertTrue(any(f.code == "order_price_mismatch" for f in report.findings))
        self.assertFalse(any(f.code == "line_total_mismatch" for f in report.findings))

    def test_default_commercial_policy_preserves_one_cent_tolerance(self):
        report = self.validate()

        self.assertEqual(report.commercial_policy.version, 0)
        self.assertEqual(report.commercial_policy.default_total_tolerance, "0.01")

    def test_unsupported_element_is_a_warning_for_human_review(self):
        data = reference_document()
        data["unsupported_elements"] = ["frete"]
        report = self.validate(data)
        self.assertEqual(report.status, "ready_for_review")
        self.assertEqual(report.findings[0].severity, "warning")

    def test_order_mismatch_is_divergence_and_never_approval(self):
        data = reference_document()
        data["items"][0]["quantity"]["value"] = "11"
        report = self.validate(data)
        self.assertEqual(report.status, "divergent")
        self.assertNotEqual(report.status, "approved")
        self.assertTrue(any(finding.code == "order_quantity_mismatch" for finding in report.findings))

    def test_supplier_identifier_order_currency_code_and_price_are_compared(self):
        cases = [
            ("supplier_name", "Outro fornecedor", "order_field_mismatch"),
            ("supplier_identifier", "FORN-9999", "order_field_mismatch"),
            ("purchase_order_reference", "PC-2026-9999", "order_field_mismatch"),
            ("currency", "USD", "order_field_mismatch"),
        ]
        for document_field, wrong_value, expected_code in cases:
            with self.subTest(field=document_field):
                data = reference_document()
                data[document_field]["value"] = wrong_value
                report = self.validate(data)
                self.assertEqual(report.status, "divergent")
                self.assertTrue(any(f.code == expected_code for f in report.findings))

        data = reference_document()
        data["items"][0]["code"]["value"] = "ARR-999"
        self.assertTrue(any(f.code == "order_items_mismatch" for f in self.validate(data).findings))
        data = reference_document()
        data["items"][0]["unit_price"]["value"] = "90.00"
        self.assertTrue(any(f.code == "order_price_mismatch" for f in self.validate(data).findings))

    def test_non_positive_quantity_is_blocking(self):
        data = reference_document()
        data["items"][0]["quantity"]["value"] = "0"
        report = self.validate(data)
        self.assertEqual(report.status, "blocked")
        self.assertTrue(any(f.code == "non_positive_quantity" for f in report.findings))

    def test_schema_rejects_unexpected_properties_and_numeric_values(self):
        data = reference_document()
        data["unexpected"] = "não permitido"
        with self.assertRaises(ValidationError):
            ReferenceDocument.model_validate(data)
        data = reference_document()
        data["items"][0]["quantity"]["value"] = 10
        with self.assertRaises(ValidationError):
            ReferenceDocument.model_validate(data)


if __name__ == "__main__":
    unittest.main()
