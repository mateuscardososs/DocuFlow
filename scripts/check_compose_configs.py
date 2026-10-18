#!/usr/bin/env python3
"""Render and assert the development and standalone production Compose contracts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
URL_SAFE_TEST_SECRET = "x" * 64


def _write_env(path: Path, values: dict[str, str]) -> None:
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")
    path.chmod(0o600)


def _render(compose_file: str, env_path: Path, project: str) -> dict:
    command = [
        "docker", "compose", "--project-name", project,
        "--env-file", str(env_path), "--file", str(ROOT / compose_file),
        "config", "--format", "json",
    ]
    try:
        result = subprocess.run(command, cwd=ROOT, check=False, capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError("Docker Compose não está instalado ou não está no PATH.") from None
    if result.returncode:
        raise RuntimeError(f"docker compose config falhou para {compose_file}.")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"docker compose config não retornou JSON válido para {compose_file}.") from None


def _published_ports(service: dict) -> list[dict]:
    return service.get("ports", []) or []


def verify_development_config(config: dict) -> None:
    services = config["services"]
    for service_name in ("postgres", "redis", "api", "frontend"):
        ports = _published_ports(services[service_name])
        if not ports or any(port.get("host_ip") != "127.0.0.1" for port in ports):
            raise RuntimeError(f"A exposição local de {service_name} deixou de ficar vinculada a loopback.")


def verify_production_config(config: dict, domain: str) -> None:
    services = config["services"]
    public_services = {name for name, service in services.items() if _published_ports(service)}
    if public_services != {"proxy"}:
        raise RuntimeError("A configuração de produção publica uma porta fora do proxy Caddy.")
    published = {str(port.get("published")) for port in _published_ports(services["proxy"])}
    if published != {"80", "443"}:
        raise RuntimeError("O proxy Caddy precisa publicar apenas as portas HTTP 80 e HTTPS 443.")

    networks = config["networks"]
    app_network = networks["application"]
    if not app_network.get("internal"):
        raise RuntimeError("A rede de aplicação precisa ser interna.")
    if set(services["postgres"].get("networks", {})) != {"application"}:
        raise RuntimeError("PostgreSQL não pode sair da rede interna da aplicação.")
    if set(services["redis"].get("networks", {})) != {"application"}:
        raise RuntimeError("Redis não pode sair da rede interna da aplicação.")
    if set(services["proxy"].get("networks", {})) != {"application", "proxy_egress"}:
        raise RuntimeError("Somente o proxy deve ter a rede de saída TLS.")
    for name in ("api", "document-worker"):
        if set(services[name].get("networks", {})) != {"application", "ollama_private"}:
            raise RuntimeError(f"{name} precisa usar a rede interna e a rede privada do Ollama.")
        if services[name].get("extra_hosts"):
            raise RuntimeError(f"{name} não pode depender de extra_hosts para alcançar o Ollama.")

    origin = f"https://{domain}"
    if services["api"]["environment"].get("DOCUFLOW_FRONTEND_ORIGIN") != origin:
        raise RuntimeError("A origem CORS da API precisa ser a origem HTTPS do frontend.")
    frontend_args = services["frontend"]["build"]["args"]
    if frontend_args.get("VITE_API_BASE_URL") != origin:
        raise RuntimeError("O frontend precisa compilar a URL HTTPS configurada para a API.")
    for name in ("api", "document-worker"):
        if services[name]["environment"].get("DOCUFLOW_OLLAMA_BASE_URL") != "http://ollama:11434":
            raise RuntimeError("Ollama não recebeu a URL privada explícita de teste.")

    if services["api"]["volumes"] != services["document-worker"]["volumes"]:
        raise RuntimeError("API e worker documental precisam compartilhar o mesmo volume privado.")

    caddyfile = (ROOT / "deploy/Caddyfile").read_text(encoding="utf-8")
    for api_path in ("/health", "/auth/*", "/documents*", "/purchase-orders*", "/commercial-policy*", "/dashboard*", "/erp-submissions*", "/workspaces/*"):
        if api_path not in caddyfile:
            raise RuntimeError(f"A rota {api_path} não está roteada pelo proxy para a API.")


def main() -> int:
    development_values = {
        "POSTGRES_DB": "docuflow_config_check",
        "POSTGRES_USER": "docuflow_check",
        "POSTGRES_PASSWORD": URL_SAFE_TEST_SECRET,
        "JWT_SECRET_KEY": URL_SAFE_TEST_SECRET,
        "DOCUFLOW_CONTAINER_DATABASE_URL": "postgresql+psycopg://docuflow_check:" + URL_SAFE_TEST_SECRET + "@postgres:5432/docuflow_config_check",
        "DOCUFLOW_CONTAINER_REDIS_URL": "redis://redis:6379/0",
    }
    production_values = {
        "DOCUFLOW_COMPOSE_PROJECT_NAME": "docuflow-config-check",
        "DOCUFLOW_DOMAIN": "pilot.docuflow.app",
        "DOCUFLOW_ACME_EMAIL": "ops@docuflow-pilot.org",
        "DOCUFLOW_ACME_CA": "https://acme-v02.api.letsencrypt.org/directory",
        "POSTGRES_DB": "docuflow_config_check",
        "POSTGRES_USER": "docuflow_check",
        "POSTGRES_PASSWORD": URL_SAFE_TEST_SECRET,
        "REDIS_PASSWORD": URL_SAFE_TEST_SECRET,
        "JWT_SECRET_KEY": URL_SAFE_TEST_SECRET,
        "DOCUFLOW_OLLAMA_BASE_URL": "http://ollama:11434",
        "DOCUFLOW_OLLAMA_MODEL": "model-config-check",
        "DOCUFLOW_OLLAMA_DOCKER_NETWORK": "docuflow_ollama_config_check",
    }

    with tempfile.TemporaryDirectory(prefix="docuflow-compose-check-") as tmp:
        tmp_dir = Path(tmp)
        dev_env = tmp_dir / "development.env"
        prod_env = tmp_dir / "production.env"
        _write_env(dev_env, development_values)
        _write_env(prod_env, production_values)
        try:
            dev = _render("compose.yaml", dev_env, "docuflow-dev-config-check")
            prod = _render("compose.production.yaml", prod_env, "docuflow-production-config-check")
            verify_development_config(dev)
            verify_production_config(prod, production_values["DOCUFLOW_DOMAIN"])
        except (KeyError, RuntimeError) as error:
            print(f"Verificação Compose falhou: {error}", file=sys.stderr)
            return 1

    print("Compose de desenvolvimento: válido; portas locais permanecem em loopback.")
    print("Compose de produção: válido; somente Caddy publica HTTP/HTTPS e a API usa a URL HTTPS configurada.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
