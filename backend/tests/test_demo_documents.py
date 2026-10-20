import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image
from pypdf import PdfReader


ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "scripts" / "generate_demo_documents.py"
DOCUMENT_FILENAMES = {
    "01_correto.pdf",
    "02_preco_divergente.pdf",
    "03_total_incorreto.pdf",
    "04_quantidade_divergente.pdf",
    "05_fornecedor_divergente.pdf",
    "06_numero_ausente.pdf",
    "07_data_ausente.pdf",
    "08_correto_digitalizado.pdf",
    "09_correto_foto.png",
    "10_duplicata_exata.pdf",
}


class DemoDocumentGeneratorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="docuflow-demo-docs-")
        root = Path(self.temp.name)
        self.documents_dir = root / "documents"
        self.ground_truth_dir = root / "ground_truth"

    def tearDown(self):
        self.temp.cleanup()

    def generate(self):
        return subprocess.run(
            [
                sys.executable,
                str(GENERATOR),
                "--documents-dir",
                str(self.documents_dir),
                "--ground-truth-dir",
                str(self.ground_truth_dir),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_generates_reproducible_inputs_and_matching_manifest(self):
        generated = self.generate()
        self.assertEqual(generated.returncode, 0, generated.stdout + generated.stderr)

        document_paths = sorted(self.documents_dir.iterdir())
        self.assertEqual({path.name for path in document_paths}, DOCUMENT_FILENAMES)
        files_before = {path.name: path.read_bytes() for path in document_paths}
        self.assertEqual(files_before["01_correto.pdf"], files_before["10_duplicata_exata.pdf"])

        manifest_path = self.ground_truth_dir / "manifest.json"
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        cases = {case["filename"]: case for case in manifest["cases"]}
        self.assertEqual(set(cases), DOCUMENT_FILENAMES)
        for filename, content in files_before.items():
            self.assertEqual(cases[filename]["sha256"], hashlib.sha256(content).hexdigest())

        generated_again = self.generate()
        self.assertEqual(generated_again.returncode, 0, generated_again.stdout + generated_again.stderr)
        self.assertEqual(
            {path.name: path.read_bytes() for path in sorted(self.documents_dir.iterdir())},
            files_before,
        )
        self.assertEqual(manifest_path.read_bytes(), manifest_bytes)

    def test_manifest_separates_document_truth_from_commercial_expectations(self):
        generated = self.generate()
        self.assertEqual(generated.returncode, 0, generated.stdout + generated.stderr)
        manifest = json.loads((self.ground_truth_dir / "manifest.json").read_text(encoding="utf-8"))
        cases = {case["case_id"]: case for case in manifest["cases"]}

        self.assertEqual(manifest["commercial_policy"]["default_total_tolerance"], "0.01")
        self.assertEqual(cases["02"]["document_truth"]["extracted_document"]["items"][0]["unit_price"]["value"], "90.00")
        self.assertEqual(cases["02"]["commercial_expectation"]["final_status"], "divergent")
        self.assertIn("order_price_mismatch", cases["02"]["commercial_expectation"]["finding_codes"])
        self.assertEqual(cases["03"]["document_truth"]["extracted_document"]["document_total"]["value"], "1300.00")
        self.assertIn("document_total_mismatch", cases["03"]["commercial_expectation"]["finding_codes"])
        self.assertEqual(cases["03"]["document_truth"]["printed_items_sum"], "1250.00")
        self.assertFalse(cases["03"]["document_truth"]["items_sum_matches_printed_total_with_0_01_tolerance"])
        self.assertEqual(cases["02"]["document_truth"]["item_arithmetic"][0], {
            "quantity": "10",
            "unit_price": "90.00",
            "printed_line_total": "900.00",
            "calculated_line_total": "900.00",
            "matches_with_0_01_tolerance": True,
        })
        self.assertEqual(
            cases["10"]["duplicate_behavior"],
            {
                "same_workspace": "return_existing_document",
                "different_workspace": "create_document",
                "existing_case_id": "01",
            },
        )
        self.assertEqual(cases["06"]["document_truth"]["extracted_document"]["document_number"], {
            "raw_value": None,
            "value": None,
            "page": None,
            "evidence": None,
        })
        self.assertIn("document_number", cases["06"]["document_truth"]["absent_fields"])

    def test_native_pdf_has_text_scanned_pdf_has_no_text_and_image_is_gray(self):
        generated = self.generate()
        self.assertEqual(generated.returncode, 0, generated.stdout + generated.stderr)

        for path in self.documents_dir.iterdir():
            self.assertLessEqual(path.stat().st_size, 10 * 1024 * 1024)
            if path.suffix.lower() == ".pdf":
                self.assertEqual(len(PdfReader(str(path)).pages), 1, path.name)

        native = PdfReader(str(self.documents_dir / "01_correto.pdf"))
        self.assertEqual(len(native.pages), 1)
        native_text = native.pages[0].extract_text() or ""
        self.assertIn("DOC-DEMO-001", native_text)
        self.assertIn("DOCUMENTO FICTÍCIO PARA TESTES", native_text)

        scanned = PdfReader(str(self.documents_dir / "08_correto_digitalizado.pdf"))
        self.assertNotEqual(
            (self.documents_dir / "01_correto.pdf").read_bytes(),
            (self.documents_dir / "08_correto_digitalizado.pdf").read_bytes(),
        )
        self.assertEqual(len(scanned.pages), 1)
        self.assertEqual((scanned.pages[0].extract_text() or "").strip(), "")
        width = float(scanned.pages[0].mediabox.width)
        height = float(scanned.pages[0].mediabox.height)
        self.assertAlmostEqual(width, 612, delta=2)
        self.assertAlmostEqual(height, 792, delta=2)

        with Image.open(self.documents_dir / "09_correto_foto.png") as image:
            self.assertEqual(image.mode, "L")
            self.assertGreaterEqual(image.width, 1600)
            self.assertGreaterEqual(image.height, 2000)
            self.assertTrue(180 <= image.info["dpi"][0] <= 260)

    def test_conflicting_existing_file_is_not_overwritten(self):
        first = self.generate()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        conflict = self.documents_dir / "03_total_incorreto.pdf"
        conflict.write_bytes(b"preserve this pre-existing file")

        retry = self.generate()
        self.assertNotEqual(retry.returncode, 0)
        self.assertEqual(conflict.read_bytes(), b"preserve this pre-existing file")


if __name__ == "__main__":
    unittest.main()
