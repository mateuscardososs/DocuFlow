"""Ponto de entrada ASGI: uvicorn backend.app.main:app."""

from backend.app.api.app import create_app

app = create_app()
