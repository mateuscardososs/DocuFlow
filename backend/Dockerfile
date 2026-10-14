FROM python:3.14.4-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    DOCUMENT_STORAGE_DIR=/var/lib/docuflow/documents

WORKDIR /app

COPY requirements.lock ./requirements.lock
RUN python -m pip install --no-cache-dir -r requirements.lock

COPY backend ./backend
RUN addgroup --system --gid 10001 docuflow \
    && adduser --system --uid 10001 --ingroup docuflow --home /nonexistent --no-create-home docuflow \
    && mkdir -p /var/lib/docuflow/documents \
    && chown -R docuflow:docuflow /var/lib/docuflow

USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM runtime AS document-worker
USER root
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        tesseract-ocr=5.3.0-2 \
        tesseract-ocr-eng=1:4.1.0-2 \
        tesseract-ocr-por=1:4.1.0-2 \
    && rm -rf /var/lib/apt/lists/*
USER 10001:10001
