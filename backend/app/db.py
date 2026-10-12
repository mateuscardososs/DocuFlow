"""Configuração SQLAlchemy carregada explicitamente de DATABASE_URL."""

import os
from dataclasses import dataclass

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import sessionmaker


@dataclass(frozen=True)
class Database:
    engine: Engine
    session_factory: sessionmaker


def database_url_from_environment() -> str:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("Configure DATABASE_URL antes de iniciar o DocuFlow.")
    return database_url


def create_database(database_url: str | None = None) -> Database:
    url = database_url or database_url_from_environment()
    engine = create_engine(url, pool_pre_ping=True)
    return Database(
        engine=engine,
        session_factory=sessionmaker(bind=engine, expire_on_commit=False),
    )
