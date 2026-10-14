"""Testes da solicitação de envio aprovado ao ERP local de demonstração."""

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.auth import AuthSettings, hash_password
from backend.app.integrations.erp import DemoERPClient
from backend.app.models.base import Base
from backend.app.models.document import DocumentApprovalEntity, DocumentEntity, DocumentAuditEventEntity
from backend.app.models.erp_submission import ERPSubmissionEntity
from backend.app.models.identity import User, Workspace, WorkspaceMembership
from backend.app.models.purchase_order import PurchaseOrderEntity, PurchaseOrderItem, Supplier
from backend.app.repositories.documents import SqlAlchemyDocumentRepository
from backend.app.repositories.purchase_orders import SqlAlchemyPurchaseOrderRepository
from backend.app.services.document_storage import DocumentFileStorage
from backend.app.services.erp_submission import ERPSubmissionWorkerService
from backend.tests.inline_queue import InlineDocumentQueue
from backend.tests.test_api import StubProvider
from backend.tests.test_document_processing import reference_payload
from backend.tests.test_pdf_text_extractor import REFERENCE_TEXT, make_pdf


class RecordingERPQueue:
    def __init__(self):
        self.calls = []
        self.error = None

    def enqueue(self, submission_id: int, *, job_id: str) -> None:
        self.calls.append((submission_id, job_id))
        if self.error:
            raise self.error


class DeterministicERPClient:
    def __init__(self):
        self.records = {}
        self.received_documents = []
        self.fail_after_create = False

    def create_invoice(self, *, idempotency_key: str, document: dict) -> str:
        self.received_documents.append(document)
        erp_id = self.records.setdefault(idempotency_key, f"ERP-DEMO-{len(self.records) + 1:06d}")
        if self.fail_after_create:
            self.fail_after_create = False
            raise RuntimeError("private ERP connection detail")
        return erp_id


class ERPSubmissionApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        with self.sessions.begin() as session:
            workspace = Workspace(slug="erp-demo", name="ERP demo")
            approver = User(email="approver@example.test", password_hash=hash_password("test-password"))
            operator = User(email="operator@example.test", password_hash=hash_password("test-password"))
            outsider = User(email="outsider@example.test", password_hash=hash_password("test-password"))
            other_workspace = Workspace(slug="other-erp", name="Other ERP")
            session.add_all([workspace, approver, operator, outsider, other_workspace])
            session.flush()
            session.add_all([
                WorkspaceMembership(user_id=approver.id, workspace_id=workspace.id, role="approver"),
                WorkspaceMembership(user_id=operator.id, workspace_id=workspace.id, role="operator"),
                WorkspaceMembership(user_id=outsider.id, workspace_id=other_workspace.id, role="approver"),
            ])
            supplier = Supplier(
                workspace_id=workspace.id,
                name="Metalúrgica Horizonte Ltda.",
                identifier="FORN-0001",
            )
            session.add(supplier)
            session.flush()
            session.add(PurchaseOrderEntity(
                workspace_id=workspace.id,
                reference="PC-2026-0001",
                supplier_id=supplier.id,
                currency="BRL",
                total=Decimal("1250.00"),
                items=[
                    PurchaseOrderItem(
                        code="ARR-001", description="Arruela zincada M8", quantity=Decimal("10"),
                        unit_price=Decimal("85.50"), line_total=Decimal("855.00"),
                    ),
                    PurchaseOrderItem(
                        code="PAR-002", description="Parafuso sextavado M8", quantity=Decimal("5"),
                        unit_price=Decimal("79.00"), line_total=Decimal("395.00"),
                    ),
                ],
            ))
            self.workspace_id = workspace.id
            self.other_workspace_id = other_workspace.id
            self.approver_id = approver.id
            self.operator_id = operator.id
            self.outsider_id = outsider.id

        self.storage = DocumentFileStorage(Path(self.tmp.name))
        self.document_queue = InlineDocumentQueue()
        self.erp_queue = RecordingERPQueue()
        self.erp_client = DeterministicERPClient()
        from backend.app.api.app import create_app

        self.app = create_app(
            extraction_provider=StubProvider(
                response=json.dumps(reference_payload(), ensure_ascii=False)
            ),
            order_repository=SqlAlchemyPurchaseOrderRepository(self.sessions),
            document_repository=SqlAlchemyDocumentRepository(self.sessions),
            document_storage=self.storage,
            document_queue=self.document_queue,
            session_factory=self.sessions,
            auth_settings=AuthSettings("test-only-secret-key-with-at-least-32-bytes"),
            erp_submission_queue=self.erp_queue,
        )
        self.document_queue.app = self.app
        self.tokens = {
            name: self.app.state.authentication.issue_access_token(user_id)[0]
            for name, user_id in (
                ("approver", self.approver_id),
                ("operator", self.operator_id),
                ("outsider", self.outsider_id),
            )
        }
        self.client = TestClient(self.app)
        uploaded = self.client.post(
            "/documents/analyze",
            headers=self._headers("operator"),
            data={"purchase_order_reference": "PC-2026-0001"},
            files={"file": ("invoice.pdf", make_pdf([REFERENCE_TEXT]), "application/pdf")},
        )
        self.assertEqual(uploaded.status_code, 202, uploaded.text)
        self.document_id = uploaded.json()["document_id"]
        approved = self.client.post(
            f"/documents/{self.document_id}/approve",
            headers=self._headers("approver"),
            json={"expected_version": 1},
        )
        self.assertEqual(approved.status_code, 200, approved.text)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()
        self.tmp.cleanup()

    def _headers(self, user="approver", workspace_id=None):
        return {
            "Authorization": f"Bearer {self.tokens[user]}",
            "X-Workspace-ID": str(workspace_id or self.workspace_id),
        }

    def _request(self, *, user="approver", version=1, document_id=None, workspace_id=None):
        return self.client.post(
            f"/documents/{document_id or self.document_id}/erp-submission",
            headers=self._headers(user, workspace_id),
            json={"expected_version": version},
        )

    def _worker(self):
        return ERPSubmissionWorkerService(
            self.app.state.erp_submission_repository,
            self.erp_client,
        )

    def test_demo_client_returns_stable_fictitious_identifier(self):
        client = DemoERPClient()
        first = client.create_invoice(
            idempotency_key="docuflow:1:2:3", document=reference_payload()
        )
        repeated = client.create_invoice(
            idempotency_key="docuflow:1:2:3", document={"changed": True}
        )
        another_version = client.create_invoice(
            idempotency_key="docuflow:1:2:4", document=reference_payload()
        )
        self.assertTrue(first.startswith("ERP-DEMO-"))
        self.assertEqual(first, repeated)
        self.assertNotEqual(first, another_version)

    def test_approver_can_request_erp_submission_for_approved_snapshot(self):
        response = self._request()

        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["state"], "queued")
        self.assertEqual(len(self.erp_queue.calls), 1)
        self.assertEqual(self.erp_queue.calls[0][0], response.json()["id"])

    def test_only_approver_can_request_or_consult_submission(self):
        response = self._request(user="operator")
        self.assertEqual(response.status_code, 403)

        response = self.client.post(
            f"/documents/{self.document_id}/erp-submission",
            json={"expected_version": 1},
        )
        self.assertEqual(response.status_code, 401)

        requested = self._request()
        submission_id = requested.json()["id"]
        self.assertEqual(
            self.client.get(
                f"/erp-submissions/{submission_id}", headers=self._headers("operator")
            ).status_code,
            403,
        )

    def test_pending_rejected_missing_snapshot_and_stale_version_are_refused(self):
        for state in ("pending", "rejected"):
            with self.subTest(state=state):
                with self.sessions.begin() as session:
                    document = session.get(DocumentEntity, self.document_id)
                    document.review_status = state
                self.assertEqual(self._request().status_code, 409)
                with self.sessions.begin() as session:
                    session.get(DocumentEntity, self.document_id).review_status = "approved"

        self.assertEqual(self._request(version=2).status_code, 409)
        with self.sessions.begin() as session:
            approval = session.scalar(
                select(DocumentApprovalEntity).where(
                    DocumentApprovalEntity.document_id == self.document_id
                )
            )
            session.delete(approval)
        self.assertEqual(self._request().status_code, 409)
        self.assertEqual(self.erp_queue.calls, [])

    def test_workspace_scoping_hides_submission_and_document(self):
        self.assertEqual(
            self._request(user="outsider", workspace_id=self.other_workspace_id).status_code,
            404,
        )
        created = self._request()
        submission_id = created.json()["id"]
        hidden = self.client.get(
            f"/erp-submissions/{submission_id}",
            headers=self._headers("outsider", self.other_workspace_id),
        )
        self.assertEqual(hidden.status_code, 404)

    def test_worker_uses_immutable_approval_snapshot_and_duplicate_job_is_idempotent(self):
        requested = self._request()
        submission_id = requested.json()["id"]
        job_id = self.erp_queue.calls[0][1]
        with self.sessions.begin() as session:
            document = session.get(DocumentEntity, self.document_id)
            document.extracted_document["document_number"]["value"] = "UNAPPROVED-EDIT"
            document.reviewed_document = {"document_number": {"value": "UNAPPROVED-EDIT"}}
            approval = session.scalar(
                select(DocumentApprovalEntity).where(
                    DocumentApprovalEntity.document_id == self.document_id
                )
            )
            approved_snapshot = approval.approved_document

        worker = self._worker()
        self.assertEqual(worker.process(submission_id=submission_id, job_id=job_id), "succeeded")
        self.assertEqual(worker.process(submission_id=submission_id, job_id=job_id), "skipped")
        self.assertEqual(self.erp_client.received_documents, [approved_snapshot])
        self.assertEqual(len(self.erp_client.records), 1)
        with self.sessions() as session:
            stored = session.get(ERPSubmissionEntity, submission_id)
            self.assertEqual(stored.state, "succeeded")
            self.assertEqual(stored.attempts, 1)
            events = list(session.scalars(
                select(DocumentAuditEventEntity)
                .where(DocumentAuditEventEntity.document_id == self.document_id)
            ))
        self.assertEqual(
            [event.event_type for event in events if event.event_type.startswith("erp_")],
            ["erp_submission", "erp_attempt", "erp_success"],
        )
        audit_response = self.client.get(
            f"/documents/{self.document_id}/audit", headers=self._headers()
        )
        self.assertEqual(audit_response.status_code, 200, audit_response.text)
        self.assertEqual(
            [event["event_type"] for event in audit_response.json() if event["event_type"].startswith("erp_")],
            ["erp_submission", "erp_attempt", "erp_success"],
        )
        repeated = self._request()
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(repeated.json()["id"], submission_id)
        self.assertEqual(len(self.erp_queue.calls), 1)

    def test_worker_does_not_send_if_approval_was_invalidated_after_request(self):
        response = self._request()
        submission_id = response.json()["id"]
        job_id = self.erp_queue.calls[0][1]
        with self.sessions.begin() as session:
            document = session.get(DocumentEntity, self.document_id)
            document.review_status = "pending"
            document.review_version = 2

        self.assertEqual(
            self._worker().process(submission_id=submission_id, job_id=job_id),
            "skipped",
        )
        persisted = self.app.state.erp_submission_repository.get_by_id(
            submission_id, self.workspace_id
        )
        self.assertEqual(persisted.state, "failed")
        self.assertIn("aprovação", persisted.failure_message)
        self.assertEqual(self.erp_client.received_documents, [])

    def test_failed_attempt_is_safe_and_retry_reuses_submission_and_erp_identifier(self):
        self.erp_client.fail_after_create = True
        first = self._request()
        submission_id = first.json()["id"]
        first_job_id = self.erp_queue.calls[0][1]
        worker = self._worker()
        self.assertEqual(worker.process(submission_id=submission_id, job_id=first_job_id), "failed")
        failed = self.client.get(
            f"/erp-submissions/{submission_id}", headers=self._headers()
        ).json()
        self.assertEqual(failed["state"], "failed")
        self.assertNotIn("private", failed["failure_message"])

        retry = self._request()
        self.assertEqual(retry.status_code, 202)
        self.assertEqual(retry.json()["id"], submission_id)
        second_job_id = self.erp_queue.calls[1][1]
        self.assertNotEqual(first_job_id, second_job_id)
        self.assertEqual(worker.process(submission_id=submission_id, job_id=second_job_id), "succeeded")
        succeeded = self.client.get(
            f"/erp-submissions/{submission_id}", headers=self._headers()
        ).json()
        self.assertEqual(succeeded["attempts"], 2)
        self.assertEqual(succeeded["state"], "succeeded")
        self.assertEqual(len(self.erp_client.records), 1)
        approved_snapshot = self.app.state.document_repository.current_approval(
            self.document_id, self.workspace_id, 1
        ).approved_document
        self.assertEqual(self.erp_client.received_documents, [approved_snapshot, approved_snapshot])
        with self.sessions() as session:
            event_types = list(session.scalars(
                select(DocumentAuditEventEntity.event_type).where(
                    DocumentAuditEventEntity.document_id == self.document_id
                )
            ))
        self.assertEqual(
            [event for event in event_types if event.startswith("erp_")],
            [
                "erp_submission", "erp_attempt", "erp_failure",
                "erp_submission", "erp_attempt", "erp_success",
            ],
        )

    def test_enqueue_failure_is_persisted_without_internal_details(self):
        self.erp_queue.error = RuntimeError("password=private-detail")
        response = self._request()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private-detail", response.text)
        submission = response.json()["submission"]
        self.assertEqual(submission["state"], "failed")
        self.assertIn("fila ERP", submission["failure_message"])
        self.assertEqual(self.erp_client.received_documents, [])
        with self.sessions() as session:
            event_type = session.scalar(
                select(DocumentAuditEventEntity.event_type).where(
                    DocumentAuditEventEntity.document_id == self.document_id,
                    DocumentAuditEventEntity.event_type == "erp_failure",
                )
            )
        self.assertEqual(event_type, "erp_failure")


if __name__ == "__main__":
    unittest.main()
