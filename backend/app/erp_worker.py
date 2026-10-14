"""Entradas RQ para solicitações do ERP de demonstração."""

from rq import get_current_job

from backend.app.db import create_database
from backend.app.integrations.erp import DemoERPClient
from backend.app.repositories.erp_submissions import SqlAlchemyERPSubmissionRepository
from backend.app.services.erp_submission import ERPSubmissionWorkerService


def _worker() -> tuple[ERPSubmissionWorkerService, object]:
    database = create_database()
    repository = SqlAlchemyERPSubmissionRepository(database.session_factory)
    return ERPSubmissionWorkerService(repository, DemoERPClient()), database.engine


def process_erp_submission_job(submission_id: int) -> str:
    """RQ recebe somente o ID persistido da solicitação ERP."""
    job = get_current_job()
    if job is None:
        raise RuntimeError("Execução sem contexto RQ.")
    worker, engine = _worker()
    try:
        return worker.process(submission_id=submission_id, job_id=job.id)
    finally:
        engine.dispose()


def mark_erp_job_failed(job, connection, exc_type, exc_value, traceback) -> None:
    """Finaliza com uma mensagem segura se o worker falhar fora do adaptador ERP."""
    if not job.args:
        return
    worker, engine = _worker()
    try:
        worker.mark_unexpected_failure(int(job.args[0]), job.id)
    finally:
        engine.dispose()
