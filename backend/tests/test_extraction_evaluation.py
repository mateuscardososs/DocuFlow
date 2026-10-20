"""Métricas puras da avaliação; não iniciam Ollama nem OCR."""

import argparse
import unittest
from unittest.mock import patch

from backend.app.evaluation.extraction_metrics import aggregate_metrics, evaluate_case
from scripts.evaluate_extraction import (
    _build_argument_parser,
    _field_csv_rows,
    _execution_configs,
    _parse_case_ids,
    _select_cases,
)


def field(value=None, *, evidence=None, page=None, raw=None):
    return {
        "raw_value": raw if raw is not None else value,
        "value": value,
        "page": page,
        "evidence": evidence,
    }


class ExtractionEvaluationMetricsTests(unittest.TestCase):
    def test_cli_selects_only_requested_case_ids(self):
        cases = [
            {"case_id": "01", "filename": "first.pdf"},
            {"case_id": "02", "filename": "second.pdf"},
            {"case_id": "06", "filename": "sixth.pdf"},
        ]
        args = _build_argument_parser().parse_args([
            "--run", "--case-ids", "01,06", "--timeout-seconds", "180",
        ])

        self.assertEqual(args.case_ids, ("01", "06"))
        self.assertEqual([case["case_id"] for case in _select_cases(cases, args.case_ids)], ["01", "06"])
        self.assertEqual(args.timeout_seconds, 180)

    def test_cli_rejects_empty_case_ids(self):
        for value in ("", ",", "01,,06", " , "):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                _parse_case_ids(value)

    def test_unknown_case_id_is_rejected_clearly(self):
        with self.assertRaisesRegex(ValueError, "ID.*99"):
            _select_cases([{"case_id": "01"}], ("99",))

    def test_timeout_override_is_per_run_and_attempt_default_stays_two(self):
        with patch.dict("os.environ", {}, clear=True):
            ollama, extraction = _execution_configs(timeout_seconds=180)

        self.assertEqual(ollama.timeout_seconds, 180)
        self.assertEqual(extraction.max_attempts, 2)

    def test_omitted_cli_options_preserve_existing_timeout_and_attempt_defaults(self):
        args = _build_argument_parser().parse_args(["--run"])
        with patch.dict("os.environ", {}, clear=True):
            ollama, extraction = _execution_configs(args.timeout_seconds)

        self.assertIsNone(args.case_ids)
        self.assertEqual(ollama.timeout_seconds, 60)
        self.assertEqual(extraction.max_attempts, 2)

    def test_csv_rows_accept_a_not_executed_case_with_no_error(self):
        expected = {"supplier_name": field("Fornecedor")}
        case = {
            "case_id": "01",
            "filename": "01_correto.pdf",
            "input_type": "native_pdf",
            "document_type": "native_pdf",
            "case_status": "not_executed",
            "expected_document": expected,
            "expected_absent_fields": [],
            "processing_error": None,
            "metrics": None,
        }

        rows = _field_csv_rows(case)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["expected_value"], "Fornecedor")
        self.assertIsNone(rows[0]["processing_error_code"])
        self.assertEqual(rows[0]["case_status"], "not_executed")

    def test_values_are_compared_after_explicit_normalization_and_absences_are_separate(self):
        expected = {
            "supplier_name": field("Metalúrgica Horizonte Ltda."),
            "document_total": field("1250.00"),
            "document_number": field(None),
        }
        actual = {
            "supplier_name": field("  METALÚRGICA   HORIZONTE LTDA. ", evidence="Fornecedor", page=1),
            "document_total": field("1250.0", evidence="Total", page=1),
            "document_number": field(None),
        }

        result = evaluate_case(
            expected,
            actual,
            absent_fields=["document_number"],
            page_texts={1: "Fornecedor\nTotal"},
            expected_divergence_codes=[],
            observed_divergence_codes=[],
        )

        self.assertEqual(result["field_accuracy"], {"correct": 2, "total": 2, "rate": 1.0})
        self.assertEqual(result["absence_recognition"], {"correct": 1, "total": 1, "rate": 1.0})
        self.assertEqual(result["evidence"], {"valid": 2, "total": 2, "rate": 1.0})
        self.assertIsNone(result["fields"]["document_number"]["value_match"])
        self.assertTrue(result["fields"]["document_number"]["correctly_absent"])

    def test_unrecognized_absence_and_missing_expected_value_are_reported_without_counting_absence_as_field(self):
        expected = {
            "document_number": field(None),
            "issue_date": field("2026-10-10"),
        }
        actual = {
            "document_number": field("GUESSED-001", evidence="Número: GUESSED-001", page=1),
            "issue_date": field(None),
        }

        result = evaluate_case(
            expected,
            actual,
            absent_fields=["document_number"],
            page_texts={1: "Número: GUESSED-001"},
            expected_divergence_codes=[],
            observed_divergence_codes=[],
        )

        self.assertEqual(result["field_accuracy"], {"correct": 0, "total": 1, "rate": 0.0})
        self.assertEqual(result["absence_recognition"], {"correct": 0, "total": 1, "rate": 0.0})
        self.assertEqual(result["evidence"], {"valid": 0, "total": 1, "rate": 0.0})
        self.assertFalse(result["fields"]["document_number"]["correctly_absent"])
        self.assertIsNone(result["fields"]["document_number"]["evidence_valid"])
        self.assertFalse(result["fields"]["issue_date"]["value_match"])

    def test_evidence_requires_the_reported_page_and_divergences_preserve_repeated_codes(self):
        doc = {"supplier_name": field("Nome", evidence="Fornecedor: Nome", page=2)}
        result = evaluate_case(
            doc,
            doc,
            absent_fields=[],
            page_texts={1: "Fornecedor: Nome", 2: "Outro texto"},
            expected_divergence_codes=["order_field_mismatch", "order_field_mismatch"],
            observed_divergence_codes=["order_field_mismatch"],
        )

        self.assertEqual(result["evidence"], {"valid": 0, "total": 1, "rate": 0.0})
        self.assertFalse(result["fields"]["supplier_name"]["evidence_valid"])
        self.assertFalse(result["divergences"]["match"])

    def test_aggregate_separates_native_pdf_and_ocr_without_inventing_empty_rates(self):
        base = {
            "field_accuracy": {"correct": 2, "total": 2, "rate": 1.0},
            "absence_recognition": {"correct": 0, "total": 0, "rate": None},
            "evidence": {"valid": 2, "total": 2, "rate": 1.0},
        }
        scanned = {
            "field_accuracy": {"correct": 1, "total": 2, "rate": 0.5},
            "absence_recognition": {"correct": 1, "total": 1, "rate": 1.0},
            "evidence": {"valid": 1, "total": 2, "rate": 0.5},
        }
        photo = {
            "field_accuracy": {"correct": 2, "total": 2, "rate": 1.0},
            "absence_recognition": {"correct": 0, "total": 0, "rate": None},
            "evidence": {"valid": 2, "total": 2, "rate": 1.0},
        }

        result = aggregate_metrics([
            {"input_type": "native_pdf", "document_type": "native_pdf", "metrics": base},
            {"input_type": "ocr", "document_type": "scanned_pdf", "metrics": scanned},
            {"input_type": "ocr", "document_type": "image", "metrics": photo},
            {
                "input_type": "native_pdf", "document_type": "native_pdf",
                "case_status": "processing_error", "metrics": None,
            },
        ])

        self.assertEqual(result["overall"]["cases_seen"], 4)
        self.assertEqual(result["overall"]["cases"], 3)
        self.assertEqual(result["overall"]["processing_failures"], 1)
        self.assertEqual(result["overall"]["field_accuracy"], {"correct": 5, "total": 6, "rate": 5 / 6})
        self.assertEqual(result["by_input_type"]["native_pdf"]["cases_seen"], 2)
        self.assertEqual(result["by_input_type"]["native_pdf"]["processing_failures"], 1)
        self.assertEqual(result["by_input_type"]["ocr"]["cases"], 2)
        self.assertEqual(result["by_input_type"]["ocr"]["field_accuracy"], {
            "correct": 3, "total": 4, "rate": 0.75,
        })
        self.assertEqual(result["by_document_type"]["scanned_pdf"]["cases_seen"], 1)
        self.assertEqual(result["by_document_type"]["scanned_pdf"]["cases"], 1)
        self.assertEqual(result["by_document_type"]["image"]["cases"], 1)
        empty = aggregate_metrics([])
        self.assertIsNone(empty["overall"]["field_accuracy"]["rate"])
        self.assertIsNone(empty["overall"]["absence_recognition"]["rate"])

        failed_only = aggregate_metrics([{
            "input_type": "ocr", "document_type": "scanned_pdf",
            "case_status": "processing_error", "metrics": None,
        }])
        scanned_result = failed_only["by_document_type"]["scanned_pdf"]
        self.assertEqual(scanned_result["cases_seen"], 1)
        self.assertEqual(scanned_result["cases"], 0)
        self.assertEqual(scanned_result["processing_failures"], 1)
        self.assertIsNone(scanned_result["field_accuracy"]["rate"])


if __name__ == "__main__":
    unittest.main()
