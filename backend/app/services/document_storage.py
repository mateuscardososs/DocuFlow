"""Armazenamento privado de PDFs com chaves internas aleatórias."""

from pathlib import Path
import os
from uuid import uuid4


class DocumentFileStorage:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    def new_key(self, workspace_id: int) -> str:
        return f"{workspace_id}/{uuid4().hex}.pdf"

    def store(self, key: str, content: bytes) -> Path:
        path = self.path_for(key)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        file_descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(file_descriptor, "wb") as stored_file:
            stored_file.write(content)
        return path

    def path_for(self, key: str) -> Path:
        path = (self.root / key).resolve()
        try:
            path.relative_to(self.root)
        except ValueError:
            raise ValueError("A chave de armazenamento não é válida.") from None
        return path
