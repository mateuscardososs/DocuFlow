"""Comando: python -m backend.app.recover_documents [--stale-seconds N]."""

import argparse
from dataclasses import replace

from backend.app.db import create_database
from backend.app.repositories.documents import SqlAlchemyDocumentRepository
from backend.app.services.document_queue import DocumentQueueConfig, RQDocumentQueue
from backend.app.services.document_recovery import DocumentRecoveryService


def main() -> None:
    parser = argparse.ArgumentParser(description="Recupera documentos presos em processing.")
    parser.add_argument("--stale-seconds", type=int, default=None)
    arguments = parser.parse_args()

    config = DocumentQueueConfig.from_environment()
    if arguments.stale_seconds is not None:
        config = replace(config, stale_after_seconds=arguments.stale_seconds)
    database = create_database()
    try:
        service = DocumentRecoveryService(
            SqlAlchemyDocumentRepository(database.session_factory),
            RQDocumentQueue(config),
            config,
        )
        print(service.recover())
    finally:
        database.engine.dispose()


if __name__ == "__main__":
    main()
