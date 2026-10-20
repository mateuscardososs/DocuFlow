#!/usr/bin/env python3
"""Avaliação explícita do pipeline real com amostras sintéticas e Ollama local."""

from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from importlib import metadata
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.commercial_policy import CommercialPolicy  # noqa: E402
from backend.app.evaluation.extraction_metrics import aggregate_metrics, evaluate_case, flatten_fields  # noqa: E402
from backend.app.integrations.ollama_extraction import OllamaConfig, OllamaExtractionProvider  # noqa: E402
from backend.app.reference_order_data import REFERENCE_PURCHASE_ORDER  # noqa: E402
from backend.app.services.document_processing import DocumentProcessingConfig, DocumentProcessingService  # noqa: E402
from backend.app.services.document_text_extractor import OcrConfig  # noqa: E402
from backend.app.services.structured_extraction import StructuredExtractionConfig  # noqa: E402


SAMPLES_DIR = ROOT / "samples"
MANIFEST_PATH = SAMPLES_DIR / "ground_truth" / "manifest.json"
DOCUMENTS_DIR = SAMPLES_DIR / "documents"
ALLOWED_ENV_KEYS = {
    "DOCUFLOW_OLLAMA_BASE_URL",
    "DOCUFLOW_OLLAMA_MODEL",
    "DOCUFLOW_OLLAMA_TIMEOUT_SECONDS",
    "DOCUFLOW_OCR_LANGUAGE",
    "DOCUFLOW_OCR_DPI",
    "DOCUFLOW_OCR_TIMEOUT_SECONDS",
    "DOCUFLOW_NATIVE_TEXT_MIN_CHARS",
    "DOCUFLOW_TESSERACT_CMD",
}
CSV_FIELDS = [
    "case_id", "filename", "input_type", "document_type", "case_status", "duration_seconds", "field",
    "expected_raw_value", "expected_value", "extracted_raw_value", "extracted_value",
    "expected_absent", "value_match", "correctly_absent", "evidence_page", "evidence_valid",
    "processing_error_code", "processing_error",
]


def load_evaluation_env(path: Path = ROOT / ".env") -> None:
    """Load only harmless provider/OCR settings; database and auth keys are ignored."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.removeprefix("export ").split("=", 1)
        key = key.strip()
        if key not in ALLOWED_ENV_KEYS or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ[key] = value


def _git_metadata() -> dict[str, Any]:
    commit = _git("rev-parse", "HEAD")
    status = _git("status", "--short", "--untracked-files=all")
    return {
        "commit": commit,
        "working_tree_dirty": bool(status),
        "working_tree_changes": status.splitlines(),
    }


def _git(*args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=5, check=True
        )
        return completed.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _memory_bytes() -> int | None:
    if sys.platform == "darwin":
        try:
            output = subprocess.run(
                ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=2, check=True
            ).stdout.strip()
            return int(output)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        return None
    return None


def _binary_version(command: str, args: list[str]) -> str | None:
    path = shutil.which(command)
    if path is None:
        return None
    try:
        result = subprocess.run([path, *args], capture_output=True, text=True, timeout=3, check=False)
        first_line = (result.stdout or result.stderr).strip().splitlines()
        return first_line[0][:200] if first_line else "installed; version output unavailable"
    except (OSError, subprocess.SubprocessError):
        return "installed; version check unavailable"


def _metadata() -> dict[str, Any]:
    package_names = ("pydantic", "pypdf", "pypdfium2", "Pillow", "reportlab")
    versions: dict[str, str | None] = {"python": platform.python_version()}
    for package in package_names:
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = None
    versions["tesseract"] = _binary_version(os.getenv("DOCUFLOW_TESSERACT_CMD", "tesseract"), ["--version"])
    versions["ollama_cli"] = _binary_version("ollama", ["--version"])
    memory = _memory_bytes()
    return {
        **_git_metadata(),
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "hardware": {
            "platform": platform.platform(),
            "architecture": platform.machine(),
            "processor": platform.processor() or None,
            "logical_cpu_count": os.cpu_count(),
            "memory_bytes": memory,
            "source": "detected from the local evaluation host; GPU not queried",
        },
        "versions": versions,
    }


def _local_endpoint(config: OllamaConfig) -> tuple[OllamaConfig | None, str | None]:
    parts = urlsplit(config.base_url)
    host = (parts.hostname or "").lower()
    if (
        parts.scheme != "http"
        or host not in {"localhost", "127.0.0.1", "::1", "host.docker.internal"}
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        return None, "O endpoint configurado não é um endpoint Ollama local permitido."
    local_host = "localhost" if host == "host.docker.internal" else host
    netloc = f"[{local_host}]" if ":" in local_host else local_host
    if parts.port:
        netloc += f":{parts.port}"
    base_url = urlunsplit(("http", netloc, parts.path.rstrip("/"), "", ""))
    return OllamaConfig(config.model, base_url, config.timeout_seconds), None


def _ollama_preflight(config: OllamaConfig) -> dict[str, Any]:
    local_config, rejection = _local_endpoint(config)
    result: dict[str, Any] = {
        "provider": "Ollama local",
        "model": config.model,
        "reachable": False,
        "configured_model_available": False,
        "available_models": [],
    }
    if rejection:
        result["reason"] = rejection
        return result
    assert local_config is not None
    try:
        with urlopen(f"{local_config.base_url}/api/tags", timeout=min(config.timeout_seconds, 3)) as response:
            payload = json.loads(response.read())
    except (OSError, URLError, TimeoutError, ValueError, json.JSONDecodeError):
        result["reason"] = "O serviço Ollama local não respondeu a /api/tags."
        return result
    names = [
        entry.get("name") for entry in payload.get("models", [])
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    ] if isinstance(payload, dict) else []
    configured = config.model
    available = any(name == configured or (":" not in configured and name.startswith(configured + ":")) for name in names)
    result.update({"reachable": True, "configured_model_available": available, "available_models": names})
    if not available:
        result["reason"] = "Ollama respondeu, mas o modelo configurado não está instalado localmente."
    return result


def _case_input_type(case_type: str) -> str:
    return "native_pdf" if case_type == "native_pdf" else "ocr"


def _parse_case_ids(value: str) -> tuple[str, ...]:
    case_ids = tuple(part.strip() for part in value.split(","))
    if not case_ids or any(not case_id for case_id in case_ids):
        raise argparse.ArgumentTypeError(
            "informe IDs separados por vírgula, sem entradas vazias (ex.: 01,06,09)."
        )
    return tuple(dict.fromkeys(case_ids))


def _positive_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("o timeout deve ser um número positivo de segundos.") from None
    if timeout <= 0:
        raise argparse.ArgumentTypeError("o timeout deve ser maior que zero.")
    return timeout


def _select_cases(cases: list[dict[str, Any]], case_ids: tuple[str, ...] | None) -> list[dict[str, Any]]:
    if case_ids is None:
        return cases
    by_id = {case["case_id"]: case for case in cases}
    unknown = [case_id for case_id in case_ids if case_id not in by_id]
    if unknown:
        raise ValueError(f"ID(s) de caso desconhecido(s): {', '.join(unknown)}.")
    return [by_id[case_id] for case_id in case_ids]


def _execution_configs(timeout_seconds: float | None) -> tuple[OllamaConfig, StructuredExtractionConfig]:
    ollama_config = OllamaConfig.from_environment()
    if timeout_seconds is not None:
        ollama_config = replace(ollama_config, timeout_seconds=timeout_seconds)
    return ollama_config, StructuredExtractionConfig()


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run", action="store_true",
        help="confirma explicitamente a avaliação usando o Ollama local configurado",
    )
    parser.add_argument(
        "--case-ids", type=_parse_case_ids,
        help="avalia somente estes IDs separados por vírgula; sem a opção, usa todos os casos distintos",
    )
    parser.add_argument(
        "--timeout-seconds", type=_positive_timeout,
        help="timeout do Ollama por tentativa nesta execução; sem a opção, mantém a configuração atual",
    )
    return parser


def _safe_error(error: Exception) -> tuple[str, str]:
    code = getattr(error, "code", None)
    if isinstance(code, str):
        return code, str(error)
    return type(error).__name__, "Falha inesperada durante a avaliação; detalhes internos foram omitidos."


def _field_csv_rows(case: dict[str, Any]) -> list[dict[str, Any]]:
    processing_error = case.get("processing_error") or {}
    base = {
        "case_id": case["case_id"],
        "filename": case["filename"],
        "input_type": case["input_type"],
        "document_type": case["document_type"],
        "case_status": case["case_status"],
        "duration_seconds": case.get("duration_seconds"),
        "processing_error_code": processing_error.get("code"),
        "processing_error": processing_error.get("message"),
    }
    metrics = case.get("metrics")
    if not metrics:
        absent = set(case["expected_absent_fields"])
        return [
            {
                **base,
                "field": name,
                "expected_raw_value": expected.get("raw_value"),
                "expected_value": expected.get("value"),
                "extracted_raw_value": "",
                "extracted_value": "",
                "expected_absent": name in absent,
                "value_match": "",
                "correctly_absent": "",
                "evidence_page": "",
                "evidence_valid": "",
            }
            for name, expected in flatten_fields(case["expected_document"]).items()
        ]
    rows = []
    for field, values in metrics["fields"].items():
        rows.append({**base, "field": field, **values})
    return rows


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_summary(path: Path, result: dict[str, Any]) -> None:
    lines = [
        "# Execução de avaliação de extração",
        "",
        f"- Estado: **{result['status']}**",
        f"- Amostra: {result['sample']['distinct_documents']} documentos distintos; duplicata exata tratada separadamente.",
        f"- Data: {result['metadata']['recorded_at_utc']}",
        f"- Commit: `{result['metadata']['commit'] or 'indisponível'}`; árvore local alterada: `{result['metadata']['working_tree_dirty']}`.",
        f"- Provedor/modelo: {result['provider']['provider']} / `{result['provider']['model']}`.",
        f"- Ollama local: {'acessível' if result['provider']['reachable'] else 'indisponível'}; modelo configurado: {'instalado' if result['provider']['configured_model_available'] else 'não instalado'}.",
        "",
    ]
    if result["status"] != "completed":
        lines.extend([
            "**Avaliação do modelo: não executado.** Não há métricas de extração nesta execução.",
            "",
            result["provider"].get("reason", "Modelo local indisponível."),
            "",
            "Nenhum modelo foi baixado ou substituído automaticamente.",
        ])
    else:
        metrics = result["metrics"]
        lines.extend([
            "## Por origem da extração",
            "",
            "| Grupo | Vistos | Processados | Falhas | Acerto de campos | Ausências reconhecidas | Evidências localizadas |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ])
        for group, data in [("Geral", metrics["overall"]), *metrics["by_input_type"].items()]:
            lines.append(
                f"| {group} | {data['cases_seen']} | {data['cases']} | {data['processing_failures']} | "
                f"{_format_rate(data['field_accuracy'])} | "
                f"{_format_rate(data['absence_recognition'])} | {_format_rate(data['evidence'])} |"
            )
        format_names = {"native_pdf": "PDF nativo", "scanned_pdf": "PDF digitalizado", "image": "Imagem"}
        lines.extend([
            "",
            "## Por formato do documento",
            "",
            "| Formato | Vistos | Processados | Falhas | Acerto de campos | Ausências reconhecidas | Evidências localizadas |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ])
        for document_type, data in metrics["by_document_type"].items():
            lines.append(
                f"| {format_names.get(document_type, document_type)} | {data['cases_seen']} | {data['cases']} | "
                f"{data['processing_failures']} | {_format_rate(data['field_accuracy'])} | "
                f"{_format_rate(data['absence_recognition'])} | {_format_rate(data['evidence'])} |"
            )
        lines.extend(["", "## Casos", "", "| Caso | Entrada | Tempo (s) | Campos corretos | Ausências | Divergências comerciais esperadas/observadas | Erro |", "|---|---|---:|---:|---:|---|---|"])
        for case in result["cases"]:
            m = case.get("metrics")
            accuracy = _format_rate(m["field_accuracy"]) if m else "—"
            absence = _format_rate(m["absence_recognition"]) if m else "—"
            divergence = "—" if not m else f"{m['divergences']['expected_codes']} / {m['divergences']['observed_codes']}"
            error = case.get("processing_error", {}).get("code", "—")
            lines.append(f"| {case['case_id']} | {case['input_type']} | {case.get('duration_seconds', '—')} | {accuracy} | {absence} | {divergence} | {error} |")
        lines.extend([
            "",
            "Evidência localizada significa apenas que o trecho aparece na página indicada; isso não confirma que a interpretação do campo esteja correta.",
            "A amostra é pequena e sintética; os resultados descrevem somente estes arquivos e esta configuração.",
        ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _format_rate(value: dict[str, Any]) -> str:
    if value["rate"] is None:
        return "n/a"
    return f"{value.get('correct', value.get('valid'))}/{value['total']} ({value['rate']:.1%})"


def run_evaluation(
    output_dir: Path,
    *,
    case_ids: tuple[str, ...] | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    load_evaluation_env()
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    all_distinct = [case for case in manifest["cases"] if case.get("duplicate_of_case_id") is None]
    distinct = _select_cases(all_distinct, case_ids)
    duplicate = next(case for case in manifest["cases"] if case.get("duplicate_of_case_id") is not None)
    ocr_config = OcrConfig.from_environment()
    ollama_config, extraction_config = _execution_configs(timeout_seconds)
    preflight = _ollama_preflight(ollama_config)
    reference_bytes = (DOCUMENTS_DIR / "01_correto.pdf").read_bytes()
    duplicate_bytes = (DOCUMENTS_DIR / duplicate["filename"]).read_bytes()
    duplicate_scenario = {
        "case_id": duplicate["case_id"],
        "fixture_sha256_matches_case_01": sha256(reference_bytes).hexdigest() == sha256(duplicate_bytes).hexdigest(),
        "same_workspace_expected_behavior": duplicate["duplicate_behavior"]["same_workspace"],
        "different_workspace_expected_behavior": duplicate["duplicate_behavior"]["different_workspace"],
        "api_behavior_test_command": ".venv/bin/python -m unittest backend.tests.test_document_persistence.DocumentPersistenceApiTests.test_identical_upload_is_reused_inside_workspace -v",
        "model_evaluation_included": False,
    }

    run: dict[str, Any] = {
        "status": "not_executed",
        "sample": {
            "distinct_documents": len(distinct),
            "available_distinct_documents": len(all_distinct),
            "case_ids": [case["case_id"] for case in distinct],
        },
        "metadata": _metadata(),
        "provider": preflight,
        "generation_config": {
            "temperature": 0,
            "ollama_timeout_seconds": ollama_config.timeout_seconds,
            "structured_max_attempts": extraction_config.max_attempts,
            "ocr_language": ocr_config.language,
            "ocr_dpi": ocr_config.dpi,
            "ocr_timeout_seconds": ocr_config.timeout_seconds,
            "native_text_min_chars": ocr_config.min_native_chars,
            "max_upload_bytes": manifest["limits"]["max_upload_bytes"],
            "max_pages": manifest["limits"]["max_pages"],
            "sample_rasterization": {case["case_id"]: case.get("rasterization") for case in distinct},
        },
        "commercial_policy": manifest["commercial_policy"],
        "duplicate_scenario": duplicate_scenario,
        "cases": [],
        "metrics": None,
        "execution_reason": preflight.get("reason"),
    }
    run["cases"] = [
        {
            "case_id": case["case_id"],
            "filename": case["filename"],
            "document_type": case["type"],
            "input_type": _case_input_type(case["type"]),
            "case_status": "not_executed",
            "duration_seconds": None,
            "expected_document": case["document_truth"]["extracted_document"],
            "expected_absent_fields": case["document_truth"]["absent_fields"],
            "expected_commercial_status": case["commercial_expectation"]["final_status"],
            "expected_divergence_codes": case["commercial_expectation"]["divergence_codes"],
            "extracted_document": None,
            "observed_commercial_status": None,
            "observed_divergence_codes": None,
            "evidence_evaluated": False,
            "processing_error": None,
        }
        for case in distinct
    ]
    if preflight["reachable"] and preflight["configured_model_available"]:
        local_config, rejection = _local_endpoint(ollama_config)
        if rejection or local_config is None:
            run["execution_reason"] = rejection
        else:
            provider = OllamaExtractionProvider(local_config)
            config = DocumentProcessingConfig(extraction=extraction_config, ocr=ocr_config)
            pipeline = DocumentProcessingService(provider, config=config)
            policy = CommercialPolicy.from_snapshot(manifest["commercial_policy"])
            run["status"] = "completed"
            run["cases"] = []
            for case in distinct:
                record: dict[str, Any] = {
                    "case_id": case["case_id"],
                    "filename": case["filename"],
                    "document_type": case["type"],
                    "input_type": _case_input_type(case["type"]),
                    "case_status": "processing",
                    "sha256": case["sha256"],
                    "expected_document": case["document_truth"]["extracted_document"],
                    "expected_absent_fields": case["document_truth"]["absent_fields"],
                    "expected_commercial_status": case["commercial_expectation"]["final_status"],
                    "expected_divergence_codes": case["commercial_expectation"]["divergence_codes"],
                    "evidence_evaluated": False,
                }
                content_type = "image/png" if case["type"] == "image" else "application/pdf"
                start = time.perf_counter()
                try:
                    processed, pages = pipeline.process_with_pages(
                        (DOCUMENTS_DIR / case["filename"]).read_bytes(),
                        REFERENCE_PURCHASE_ORDER,
                        content_type=content_type,
                        commercial_policy=policy,
                    )
                    record["duration_seconds"] = round(time.perf_counter() - start, 3)
                    extracted = processed.document.model_dump(mode="json")
                    observed_divergences = [finding.code for finding in processed.validation.findings if finding.severity == "divergence"]
                    page_map = {page.number: page.text for page in pages}
                    metrics = evaluate_case(
                        case["document_truth"]["extracted_document"],
                        extracted,
                        absent_fields=case["document_truth"]["absent_fields"],
                        page_texts=page_map,
                        expected_divergence_codes=case["commercial_expectation"]["divergence_codes"],
                        observed_divergence_codes=observed_divergences,
                    )
                    record.update({
                        "case_status": "processed",
                        "extracted_document": extracted,
                        "page_texts": [
                            {"page": page.number, "source": page.source, "text": page.text}
                            for page in pages
                        ],
                        "pdf_warnings": processed.pdf_warnings,
                        "commercial_result": {
                            "status": processed.validation.status,
                            "findings": [finding.model_dump(mode="json") for finding in processed.validation.findings],
                            "observed_divergence_codes": observed_divergences,
                            "applied_policy": processed.validation.commercial_policy.model_dump(mode="json"),
                        },
                        "observed_commercial_status": processed.validation.status,
                        "metrics": metrics,
                        "evidence_evaluated": True,
                    })
                except Exception as error:  # safety boundary for a one-off evaluation report
                    record["case_status"] = "processing_error"
                    record["extracted_document"] = None
                    record["observed_divergence_codes"] = None
                    record["duration_seconds"] = round(time.perf_counter() - start, 3)
                    code, message = _safe_error(error)
                    record["processing_error"] = {"code": code, "message": message}
                run["cases"].append(record)
            run["metrics"] = aggregate_metrics(run["cases"])
            run["total_duration_seconds"] = round(sum(case["duration_seconds"] for case in run["cases"]), 3)

    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "results.json").write_text(json.dumps(run, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    csv_rows = [row for case in run["cases"] for row in _field_csv_rows(case)]
    _write_csv(output_dir / "results.csv", csv_rows)
    _write_summary(output_dir / "summary.md", run)
    return run


def main(argv: list[str] | None = None) -> int:
    parser = _build_argument_parser()
    args = parser.parse_args(argv)
    if not args.run:
        parser.error("este comando pode chamar o Ollama local; repita com --run para confirmar")
    run_dir = ROOT / "var" / "evaluations" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        result = run_evaluation(
            run_dir,
            case_ids=args.case_ids,
            timeout_seconds=args.timeout_seconds,
        )
    except (OSError, ValueError, KeyError, StopIteration) as error:
        print(f"Avaliação não iniciada: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    print(f"Estado: {result['status']}")
    print(f"Saída: {run_dir.relative_to(ROOT)}")
    if result["status"] != "completed":
        print("Avaliação do modelo: não executado; nenhuma métrica foi calculada.")
        print(result.get("execution_reason") or "Modelo local não disponível.")
    else:
        print(f"Documentos distintos processados: {len(result['cases'])}")
        print("Relatório resumido:", run_dir.relative_to(ROOT) / "summary.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
