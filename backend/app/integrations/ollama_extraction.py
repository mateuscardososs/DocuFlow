"""Adaptador HTTP para a API local de chat do Ollama."""

import json
import os
import socket
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from backend.app.services.structured_extraction import (
    ExtractionProviderError,
    ExtractionTimeoutError,
)


@dataclass(frozen=True)
class OllamaConfig:
    model: str
    base_url: str = "http://localhost:11434"
    timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("Informe o nome do modelo Ollama.")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds precisa ser positivo.")

    @classmethod
    def from_environment(cls) -> "OllamaConfig":
        return cls(
            model=os.getenv("DOCUFLOW_OLLAMA_MODEL", "gemma4"),
            base_url=os.getenv("DOCUFLOW_OLLAMA_BASE_URL", "http://localhost:11434"),
            timeout_seconds=float(os.getenv("DOCUFLOW_OLLAMA_TIMEOUT_SECONDS", "60")),
        )


class OllamaExtractionProvider:
    def __init__(self, config: OllamaConfig) -> None:
        self.config = config

    def extract(self, *, prompt: str, output_schema: dict[str, Any]) -> str:
        body = json.dumps(
            {
                "model": self.config.model,
                "messages": [{"role": "user", "content": prompt}],
                "format": output_schema,
                "stream": False,
                "options": {"temperature": 0},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(
            f"{self.config.base_url.rstrip('/')}/api/chat",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            response = urlopen(request, timeout=self.config.timeout_seconds)
            try:
                payload = json.loads(response.read())
            finally:
                response.close()
        except HTTPError as error:
            error.close()
            raise ExtractionProviderError(f"Ollama respondeu com HTTP {error.code}.") from None
        except (TimeoutError, socket.timeout):
            raise ExtractionTimeoutError("Tempo limite de comunicação com Ollama.") from None
        except URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise ExtractionTimeoutError("Tempo limite de comunicação com Ollama.") from None
            raise ExtractionProviderError("Não foi possível conectar ao Ollama local.") from None
        except (json.JSONDecodeError, UnicodeDecodeError, OSError, ValueError):
            raise ExtractionProviderError("O Ollama retornou uma resposta ilegível.") from None

        try:
            content = payload["message"]["content"]
        except (KeyError, TypeError):
            raise ExtractionProviderError("A resposta do Ollama não contém o texto esperado.") from None
        if not isinstance(content, str):
            raise ExtractionProviderError("A resposta do Ollama não contém o texto esperado.")
        return content
