"""Verificações usadas pelos healthchecks dos containers do Compose."""

from __future__ import annotations

import os
import sys
from urllib.request import urlopen

from redis import Redis
from rq import Worker
from sqlalchemy import text

from backend.app.db import create_database


def check(role: str) -> None:
    database = create_database()
    redis = Redis.from_url(os.environ["REDIS_URL"])
    try:
        with database.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        redis.ping()

        if role == "api":
            with urlopen("http://127.0.0.1:8000/health", timeout=2) as response:
                if response.status != 200:
                    raise RuntimeError("API indisponível")
        else:
            expected_name = os.environ["DOCUFLOW_RQ_WORKER_NAME"]
            if not any(worker.name == expected_name for worker in Worker.all(connection=redis)):
                raise RuntimeError("Worker RQ indisponível")
    finally:
        redis.close()
        database.engine.dispose()


if __name__ == "__main__":
    try:
        check(sys.argv[1])
    except Exception:
        raise SystemExit(1) from None
