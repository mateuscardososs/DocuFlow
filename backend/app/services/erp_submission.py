"""Regras de solicitação e execução idempotente do envio ao ERP de demonstração."""

from dataclasses import dataclass
from uuid import uuid4

from backend.app.integrations.erp import ERPClient
from backend.app.models.erp_submission import ERPSubmissionEntity
from backend.app.repositories.erp_submissions import ERPSubmissionRepository
from backend.app.services.erp_queue import ERPSubmissionQueue


class ERPSubmissionError(Exception):
    pass


class ERPSubmissionNotFound(ERPSubmissionError):
    pass


class ERPSubmissionNotEligible(ERPSubmissionError):
    pass


class ERPSubmissionEnqueueError(ERPSubmissionError):
    def __init__(self, submission_id: int) -> None:
        super().__init__("A solicitação foi salva, mas não foi possível iniciar o envio.")
        self.submission_id = submission_id


@dataclass(frozen=True)
class ERPSubmissionRequest:
    submission: ERPSubmissionEntity
    enqueued: bool


class ERPSubmissionService:
    def __init__(
        self,
        repository: ERPSubmissionRepository,
        queue: ERPSubmissionQueue,
    ) -> None:
        self.repository = repository
        self.queue = queue

    def request(
        self,
        *,
        document_id: int,
        workspace_id: int,
        expected_version: int,
        requested_by_user_id: int,
    ) -> ERPSubmissionRequest:
        job_id = uuid4().hex
        submission, should_enqueue = self.repository.request(
            document_id=document_id,
            workspace_id=workspace_id,
            expected_version=expected_version,
            requested_by_user_id=requested_by_user_id,
            job_id=job_id,
        )
        if not should_enqueue:
            current = self.repository.get_by_id(submission.id, workspace_id)
            return ERPSubmissionRequest(current or submission, False)
        try:
            self.queue.enqueue(submission.id, job_id=job_id)
        except Exception:
            safe_message = "O envio não foi iniciado; a fila ERP está indisponível."
            self.repository.fail_enqueue(submission.id, job_id, safe_message)
            raise ERPSubmissionEnqueueError(submission.id) from None
        current = self.repository.get_by_id(submission.id, workspace_id)
        return ERPSubmissionRequest(current or submission, True)


class ERPSubmissionWorkerService:
    SAFE_FAILURE_MESSAGE = "O ERP de demonstração não concluiu o cadastro da nota fiscal."

    def __init__(self, repository: ERPSubmissionRepository, client: ERPClient) -> None:
        self.repository = repository
        self.client = client

    def process(self, *, submission_id: int, job_id: str) -> str:
        claimed = self.repository.claim(submission_id, job_id)
        if claimed is None:
            return "skipped"
        submission, approved_snapshot = claimed
        idempotency_key = (
            f"docuflow:{submission.workspace_id}:{submission.document_id}:"
            f"{submission.approved_version}"
        )
        try:
            erp_identifier = self.client.create_invoice(
                idempotency_key=idempotency_key,
                document=approved_snapshot,
            )
            if not isinstance(erp_identifier, str) or not erp_identifier.strip():
                raise ValueError("ERP identifier is empty")
        except Exception:
            self.repository.fail(
                submission_id, job_id, self.SAFE_FAILURE_MESSAGE
            )
            return "failed"
        if not self.repository.succeed(submission_id, job_id, erp_identifier):
            return "skipped"
        return "succeeded"

    def mark_unexpected_failure(self, submission_id: int, job_id: str) -> None:
        self.repository.fail(submission_id, job_id, self.SAFE_FAILURE_MESSAGE)
