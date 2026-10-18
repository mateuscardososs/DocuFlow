from __future__ import annotations

import unittest

from scripts.validate_production_env import ProductionEnvironmentError, validate_environment


def valid_environment() -> dict[str, str]:
    return {
        "DOCUFLOW_DOMAIN": "pilot.docuflow.app",
        "DOCUFLOW_ACME_EMAIL": "ops@docuflow-pilot.org",
        "DOCUFLOW_ACME_CA": "https://acme-v02.api.letsencrypt.org/directory",
        "POSTGRES_DB": "docuflow",
        "POSTGRES_USER": "docuflow",
        "POSTGRES_PASSWORD": "a" * 64,
        "REDIS_PASSWORD": "b" * 64,
        "JWT_SECRET_KEY": "c" * 64,
        "DOCUFLOW_OLLAMA_BASE_URL": "http://ollama:11434",
        "DOCUFLOW_OLLAMA_MODEL": "qwen3:8b",
        "DOCUFLOW_OLLAMA_DOCKER_NETWORK": "docuflow_ollama_private",
    }


class ProductionEnvironmentTests(unittest.TestCase):
    def test_accepts_explicit_domain_secrets_and_private_ollama_endpoint(self):
        validate_environment(valid_environment())

    def test_rejects_missing_required_environment_variables(self):
        values = valid_environment()
        del values["DOCUFLOW_OLLAMA_BASE_URL"]

        with self.assertRaises(ProductionEnvironmentError) as raised:
            validate_environment(values)

        self.assertIn("DOCUFLOW_OLLAMA_BASE_URL", str(raised.exception))

    def test_rejects_illustrative_domain_and_secret_values(self):
        values = valid_environment()
        values["DOCUFLOW_DOMAIN"] = "example.com"
        values["JWT_SECRET_KEY"] = "CHANGE_ME"

        with self.assertRaises(ProductionEnvironmentError) as raised:
            validate_environment(values)

        self.assertIn("DOCUFLOW_DOMAIN", str(raised.exception))
        self.assertIn("JWT_SECRET_KEY", str(raised.exception))
        self.assertNotIn("CHANGE_ME", str(raised.exception))

    def test_rejects_host_docker_internal_and_loopback_ollama_urls(self):
        for url in (
            "http://host.docker.internal:11434",
            "http://127.0.0.1:11434",
            "http://localhost:11434",
        ):
            with self.subTest(url=url):
                values = valid_environment()
                values["DOCUFLOW_OLLAMA_BASE_URL"] = url
                with self.assertRaises(ProductionEnvironmentError):
                    validate_environment(values)

    def test_rejects_public_ollama_hostnames(self):
        values = valid_environment()
        values["DOCUFLOW_OLLAMA_BASE_URL"] = "https://ollama.vendor.com"

        with self.assertRaises(ProductionEnvironmentError) as raised:
            validate_environment(values)

        self.assertIn("DOCUFLOW_OLLAMA_BASE_URL", str(raised.exception))

    def test_rejects_short_or_url_unsafe_passwords(self):
        values = valid_environment()
        values["POSTGRES_PASSWORD"] = "tiny"
        values["REDIS_PASSWORD"] = "password with spaces and @"

        with self.assertRaises(ProductionEnvironmentError) as raised:
            validate_environment(values)

        self.assertIn("POSTGRES_PASSWORD", str(raised.exception))
        self.assertIn("REDIS_PASSWORD", str(raised.exception))
        self.assertNotIn("password with spaces", str(raised.exception))

    def test_rejects_database_identifiers_that_break_internal_urls(self):
        values = valid_environment()
        values["POSTGRES_USER"] = "user/name"
        values["POSTGRES_DB"] = "db name"

        with self.assertRaises(ProductionEnvironmentError) as raised:
            validate_environment(values)

        self.assertIn("POSTGRES_USER", str(raised.exception))
        self.assertIn("POSTGRES_DB", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
