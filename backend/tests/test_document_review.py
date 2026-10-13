import copy
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
from backend.app.models.base import Base
from backend.app.models.document import (
    DocumentApprovalEntity,
    DocumentAuditEventEntity,
    DocumentEntity,
    DocumentReviewRevisionEntity,
)
from backend.app.models.identity import User, Workspace, WorkspaceMembership
from backend.app.models.purchase_order import PurchaseOrderEntity, PurchaseOrderItem, Supplier
from backend.app.repositories.documents import SqlAlchemyDocumentRepository
from backend.app.repositories.purchase_orders import SqlAlchemyPurchaseOrderRepository
from backend.app.services.document_processing import DocumentProcessingService
from backend.app.services.document_storage import DocumentFileStorage
from backend.tests.test_api import StubProvider
from backend.tests.test_document_processing import reference_payload
from backend.tests.test_pdf_text_extractor import REFERENCE_TEXT, make_pdf


class DocumentReviewApiTests(unittest.TestCase):
    def setUp(self):
        self.storage_dir = tempfile.TemporaryDirectory()
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False)
        with self.session_factory.begin() as session:
            workspace = Workspace(slug="demo", name="Workspace demo")
            session.add(workspace)
            session.flush()
            self.workspace_id = workspace.id
            self.user_ids = {}
            for email, roles in (
                ("operator@example.test", ["operator"]),
                ("approver@example.test", ["approver"]),
                ("both@example.test", ["operator", "approver"]),
                ("other@example.test", ["operator", "approver"]),
            ):
                user = User(email=email, password_hash=hash_password("test-password"))
                session.add(user)
                session.flush()
                self.user_ids[email] = user.id
                target_workspace_id = self.workspace_id
                if email == "other@example.test":
                    other = Workspace(slug="other", name="Outro workspace")
                    session.add(other)
                    session.flush()
                    target_workspace_id = other.id
                    self.other_workspace_id = other.id
                for role in roles:
                    session.add(
                        WorkspaceMembership(
                            user_id=user.id, workspace_id=target_workspace_id, role=role
                        )
                    )

            supplier = Supplier(
                workspace_id=self.workspace_id,
                name="Metalúrgica Horizonte Ltda.",
                identifier="FORN-0001",
            )
            session.add(supplier)
            session.flush()
            session.add(
                PurchaseOrderEntity(
                    workspace_id=self.workspace_id,
                    reference="PC-2026-0001",
                    supplier_id=supplier.id,
                    currency="BRL",
                    total=Decimal("1250.00"),
                    items=[
                        PurchaseOrderItem(
                            code="ARR-001", description="Arruela zincada M8",
                            quantity=Decimal("10"), unit_price=Decimal("85.50"),
                            line_total=Decimal("855.00"),
                        ),
                        PurchaseOrderItem(
                            code="PAR-002", description="Parafuso sextavado M8",
                            quantity=Decimal("5"), unit_price=Decimal("79.00"),
                            line_total=Decimal("395.00"),
                        ),
                    ],
                )
            )

        provider = StubProvider(response=json.dumps(reference_payload(), ensure_ascii=False))
        from backend.app.api.app import create_app

        self.app = create_app(
            extraction_provider=provider,
            order_repository=SqlAlchemyPurchaseOrderRepository(self.session_factory),
            document_repository=SqlAlchemyDocumentRepository(self.session_factory),
            document_storage=DocumentFileStorage(Path(self.storage_dir.name)),
            session_factory=self.session_factory,
            auth_settings=AuthSettings("test-only-secret-key-with-at-least-32-bytes"),
        )
        self.tokens = {
            email: self.app.state.authentication.issue_access_token(user_id)[0]
            for email, user_id in self.user_ids.items()
        }
        self.client = TestClient(self.app)
        response = self.client.post(
            "/documents/analyze",
            headers=self._headers("operator@example.test"),
            data={"purchase_order_reference": "PC-2026-0001"},
            files={"file": ("invoice.pdf", make_pdf([REFERENCE_TEXT]), "application/pdf")},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.document_id = response.json()["document_id"]

    def tearDown(self):
        self.client.close()
        self.engine.dispose()
        self.storage_dir.cleanup()

    def _headers(self, email, workspace_id=None):
        return {
            "Authorization": f"Bearer {self.tokens[email]}",
            "X-Workspace-ID": str(workspace_id or self.workspace_id),
        }

    def _detail(self):
        return self.client.get(
            f"/documents/{self.document_id}", headers=self._headers("operator@example.test")
        ).json()

    def _reviewed_document(self):
        return copy.deepcopy(reference_payload())

    def _correct(
        self,
        *,
        version=1,
        document=None,
        justification="Conferido com a nota.",
        email="operator@example.test",
        workspace_id=None,
    ):
        return self.client.patch(
            f"/documents/{self.document_id}/review",
            headers=self._headers(email, workspace_id),
            json={
                "expected_version": version,
                "justification": justification,
                "reviewed_document": self._reviewed_document() if document is None else document,
            },
        )

    def test_operator_can_correct_and_validation_uses_corrected_data(self):
        revised = self._reviewed_document()
        revised["items"][0]["quantity"]["value"] = "9"

        response = self._correct(document=revised)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["review_version"], 2)
        self.assertEqual(response.json()["validation"]["status"], "divergent")
        detail = self._detail()
        self.assertEqual(detail["extracted_document"]["items"][0]["quantity"]["value"], "10")
        self.assertEqual(detail["reviewed_document"]["items"][0]["quantity"]["value"], "9")
        self.assertEqual(detail["validation_result"]["status"], "divergent")
        events = self.client.get(
            f"/documents/{self.document_id}/audit",
            headers=self._headers("operator@example.test"),
        ).json()
        correction = next(event for event in events if event["event_type"] == "correction")
        self.assertEqual(correction["version"], 2)
        self.assertEqual(correction["actor_user_id"], self.user_ids["operator@example.test"])
        self.assertEqual(correction["justification"], "Conferido com a nota.")
        self.assertTrue(correction["created_at"])

    def test_operator_cannot_approve(self):
        response = self.client.post(
            f"/documents/{self.document_id}/approve",
            headers=self._headers("operator@example.test"),
            json={"expected_version": 1},
        )

        self.assertEqual(response.status_code, 403)

    def test_approver_without_operator_cannot_correct(self):
        response = self._correct(email="approver@example.test")

        self.assertEqual(response.status_code, 403)

    def test_approver_can_approve_with_immutable_snapshot(self):
        response = self.client.post(
            f"/documents/{self.document_id}/approve",
            headers=self._headers("approver@example.test"),
            json={"expected_version": 1},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["review_status"], "approved")
        self.assertEqual(response.json()["approval"]["approved_version"], 1)
        self.assertEqual(response.json()["approval"]["approver_user_id"], self.user_ids["approver@example.test"])
        self.assertTrue(response.json()["approval"]["approved_at"])
        self.assertEqual(response.json()["approval"]["approved_document"]["document_number"]["value"], "DOC-2026-1042")
        detail = self.client.get(
            f"/documents/{self.document_id}", headers=self._headers("approver@example.test")
        )
        self.assertEqual(detail.json()["approval"]["approved_version"], 1)

    def test_approval_is_blocked_when_revalidated_data_has_blocking_error(self):
        revised = self._reviewed_document()
        revised["supplier_name"]["value"] = None
        self.assertEqual(self._correct(document=revised).status_code, 200)

        response = self.client.post(
            f"/documents/{self.document_id}/approve",
            headers=self._headers("approver@example.test"),
            json={"expected_version": 2},
        )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self._detail()["review_status"], "pending")

    def test_rejection_requires_reason_and_records_rejecting_user(self):
        empty = self.client.post(
            f"/documents/{self.document_id}/reject",
            headers=self._headers("approver@example.test"),
            json={"expected_version": 1, "justification": "   "},
        )
        self.assertEqual(empty.status_code, 422)

        response = self.client.post(
            f"/documents/{self.document_id}/reject",
            headers=self._headers("approver@example.test"),
            json={"expected_version": 1, "justification": "Dados ilegíveis."},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["review_status"], "rejected")
        events = self.client.get(
            f"/documents/{self.document_id}/audit",
            headers=self._headers("approver@example.test"),
        ).json()
        rejection = next(event for event in events if event["event_type"] == "rejection")
        self.assertEqual(rejection["actor_user_id"], self.user_ids["approver@example.test"])
        self.assertEqual(rejection["justification"], "Dados ilegíveis.")
        self.assertTrue(rejection["created_at"])

    def test_audit_masks_secret_like_values_in_justification(self):
        response = self.client.post(
            f"/documents/{self.document_id}/reject",
            headers=self._headers("approver@example.test"),
            json={"expected_version": 1, "justification": "token=sample-secret-value"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        events = self.client.get(
            f"/documents/{self.document_id}/audit",
            headers=self._headers("approver@example.test"),
        ).json()
        self.assertNotIn("sample-secret-value", json.dumps(events))
        self.assertIn("[REDACTED]", json.dumps(events))

    def test_stale_review_version_returns_conflict(self):
        self.assertEqual(self._correct().status_code, 200)

        response = self._correct(version=1)

        self.assertEqual(response.status_code, 409)
        stale_approval = self.client.post(
            f"/documents/{self.document_id}/approve",
            headers=self._headers("approver@example.test"),
            json={"expected_version": 1},
        )
        self.assertEqual(stale_approval.status_code, 409)

    def test_correction_after_approval_invalidates_approval_but_keeps_history(self):
        approved = self.client.post(
            f"/documents/{self.document_id}/approve",
            headers=self._headers("approver@example.test"),
            json={"expected_version": 1},
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        revised = self._reviewed_document()
        revised["document_number"]["value"] = "DOC-EDITADO"

        corrected = self._correct(version=1, document=revised, email="both@example.test")

        self.assertEqual(corrected.status_code, 200, corrected.text)
        self.assertEqual(corrected.json()["review_status"], "pending")
        self.assertIsNone(corrected.json()["approval"])
        audit = self.client.get(
            f"/documents/{self.document_id}/audit", headers=self._headers("both@example.test")
        ).json()
        self.assertEqual([event["event_type"] for event in audit].count("approval"), 1)
        self.assertTrue(any(event["event_type"] == "correction" for event in audit))
        with self.session_factory() as session:
            approved_snapshot = session.scalar(
                select(DocumentApprovalEntity).where(
                    DocumentApprovalEntity.document_id == self.document_id
                )
            )
            revisions = list(
                session.scalars(
                    select(DocumentReviewRevisionEntity).where(
                        DocumentReviewRevisionEntity.document_id == self.document_id
                    )
                ).all()
            )
        self.assertEqual(approved_snapshot.approved_document["document_number"]["value"], "DOC-2026-1042")
        self.assertEqual(len(revisions), 1)

    def test_user_with_both_roles_can_correct_and_approve(self):
        corrected = self._correct(email="both@example.test")
        self.assertEqual(corrected.status_code, 200, corrected.text)

        approved = self.client.post(
            f"/documents/{self.document_id}/approve",
            headers=self._headers("both@example.test"),
            json={"expected_version": 2},
        )

        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["review_status"], "approved")

    def test_document_cannot_be_reviewed_from_another_workspace(self):
        response = self.client.get(
            f"/documents/{self.document_id}/audit",
            headers=self._headers("other@example.test", self.other_workspace_id),
        )

        self.assertEqual(response.status_code, 404)
        correction = self._correct(
            email="other@example.test", workspace_id=self.other_workspace_id
        )
        self.assertEqual(correction.status_code, 404)

    def test_failed_document_cannot_be_reviewed(self):
        with self.session_factory.begin() as session:
            record = session.get(DocumentEntity, self.document_id)
            record.processing_status = "failed"
        response = self._correct()

        self.assertEqual(response.status_code, 409)


if __name__ == "__main__":
    unittest.main()
