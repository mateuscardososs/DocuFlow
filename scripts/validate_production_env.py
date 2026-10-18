#!/usr/bin/env python3
"""Validate the non-secret production settings before Docker Compose is run."""

from __future__ import annotations

import argparse
import ipaddress
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit


REQUIRED_KEYS = (
    "DOCUFLOW_DOMAIN",
    "DOCUFLOW_ACME_EMAIL",
    "DOCUFLOW_ACME_CA",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "REDIS_PASSWORD",
    "JWT_SECRET_KEY",
    "DOCUFLOW_OLLAMA_BASE_URL",
    "DOCUFLOW_OLLAMA_MODEL",
    "DOCUFLOW_OLLAMA_DOCKER_NETWORK",
)
PLACEHOLDER = re.compile(
    r"(?:change[_ -]?me|replace[_ -]?with|your[_ -]|example\.(?:com|net|org)|"
    r"\.example\b|\.invalid\b|\.test\b|<[^>]+>)",
    re.IGNORECASE,
)
SAFE_SECRET = re.compile(r"^[A-Za-z0-9_-]+$")
DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)
DOCKER_NETWORK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")


class ProductionEnvironmentError(ValueError):
    """Configuration failure with messages that never include secret values."""


def load_environment_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        raise ProductionEnvironmentError("Não foi possível ler o arquivo de ambiente indicado.") from None

    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ProductionEnvironmentError(f"Linha {line_number} do arquivo de ambiente tem formato inválido.")
        if key in values:
            raise ProductionEnvironmentError(f"A variável {key} aparece mais de uma vez.")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def _is_placeholder(key: str, value: str) -> bool:
    return bool(PLACEHOLDER.search(value)) or (key.endswith(("_PASSWORD", "_SECRET_KEY")) and len(value) < 32)


def _valid_domain(value: str) -> bool:
    if not value or value.endswith(".") or len(value) > 253 or "://" in value or "/" in value:
        return False
    labels = value.split(".")
    if len(labels) < 2 or any(not DNS_LABEL.fullmatch(label) for label in labels):
        return False
    return labels[-1].lower() not in {"localhost", "local", "internal", "lan"}


def _private_provider_host(hostname: str) -> bool:
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        host = hostname.lower().rstrip(".")
        return host != "localhost" and (
            "." not in host or host.endswith((".internal", ".lan", ".local"))
        )
    return address.is_private and not address.is_loopback and not address.is_link_local


def validate_environment(values: dict[str, str]) -> None:
    errors: list[str] = []
    for key in REQUIRED_KEYS:
        value = values.get(key, "").strip()
        if not value:
            errors.append(f"{key} é obrigatória.")
        elif _is_placeholder(key, value):
            errors.append(f"{key} ainda contém um valor ilustrativo ou inseguro.")

    domain = values.get("DOCUFLOW_DOMAIN", "").strip()
    if domain and not _valid_domain(domain):
        errors.append("DOCUFLOW_DOMAIN precisa ser um domínio público válido, sem protocolo ou caminho.")

    email = values.get("DOCUFLOW_ACME_EMAIL", "").strip()
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        errors.append("DOCUFLOW_ACME_EMAIL precisa ser um endereço de e-mail válido.")

    for key in ("POSTGRES_PASSWORD", "REDIS_PASSWORD"):
        value = values.get(key, "")
        if value and (len(value) < 32 or not SAFE_SECRET.fullmatch(value)):
            errors.append(f"{key} precisa ter ao menos 32 caracteres URL-safe.")

    jwt_secret = values.get("JWT_SECRET_KEY", "")
    if jwt_secret and (len(jwt_secret) < 64 or not SAFE_SECRET.fullmatch(jwt_secret)):
        errors.append("JWT_SECRET_KEY precisa ter ao menos 64 caracteres URL-safe.")

    acme_ca = values.get("DOCUFLOW_ACME_CA", "").strip()
    if acme_ca:
        parsed_ca = urlsplit(acme_ca)
        if parsed_ca.scheme != "https" or not parsed_ca.hostname:
            errors.append("DOCUFLOW_ACME_CA precisa ser uma URL HTTPS.")

    provider_url = values.get("DOCUFLOW_OLLAMA_BASE_URL", "").strip()
    if provider_url:
        try:
            parsed_provider = urlsplit(provider_url)
            provider_host = parsed_provider.hostname or ""
            parsed_provider.port
        except ValueError:
            provider_host = ""
            parsed_provider = None
        if (
            parsed_provider is None
            or parsed_provider.scheme not in {"http", "https"}
            or not provider_host
            or parsed_provider.username is not None
            or parsed_provider.password is not None
            or parsed_provider.query
            or parsed_provider.fragment
            or provider_host.lower() == "host.docker.internal"
            or not _private_provider_host(provider_host)
        ):
            errors.append(
                "DOCUFLOW_OLLAMA_BASE_URL precisa apontar por HTTP(S) a um host privado, "
                "sem credenciais, query ou fragmento."
            )

    network_name = values.get("DOCUFLOW_OLLAMA_DOCKER_NETWORK", "").strip()
    if network_name and not DOCKER_NETWORK.fullmatch(network_name):
        errors.append("DOCUFLOW_OLLAMA_DOCKER_NETWORK tem formato inválido.")

    if errors:
        raise ProductionEnvironmentError("\n".join(errors))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("env_file", type=Path, help="Arquivo privado de ambiente de produção")
    args = parser.parse_args()
    try:
        validate_environment(load_environment_file(args.env_file))
    except ProductionEnvironmentError as error:
        print(f"Configuração de produção inválida:\n{error}", file=sys.stderr)
        return 2
    print("Configuração de produção validada.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
