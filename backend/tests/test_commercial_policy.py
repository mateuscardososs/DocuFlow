"""API e persistência das regras comerciais versionadas por workspace."""

import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from backend.app.auth import AuthSettings, hash_password
from backend.app.models.base import Base
from backend.app.models.document import DocumentApprovalEntity, DocumentEntity
from backend.app.models.identity import User, Workspace, WorkspaceMembership
from backend.app.repositories.documents import SqlAlchemyDocumentRepository
from backend.app.repositories.purchase_orders import SqlAlchemyPurchaseOrderRepository
from backend.app.seed_reference_order import seed_reference_order
from backend.app.services.document_processing import DocumentProcessingConfig, DocumentProcessingService
from backend.app.services.document_storage import DocumentFileStorage
from backend.tests.inline_queue import InlineDocumentQueue
from backend.tests.test_api import StubProvider
from backend.tests.test_document_processing import reference_payload
from backend.tests.test_pdf_text_extractor import REFERENCE_TEXT, make_pdf


class CommercialPolicyApiTests(unittest.TestCase):
    def setUp(self):
        from backend.app.api.app import create_app

        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        seed_reference_order(self.sessions)
        with self.sessions.begin() as session:
            workspace = session.scalar(select(Workspace).where(Workspace.slug == "demo"))
            other = Workspace(slug="other-policy", name="Outro workspace")
            operator = User(email="policy-operator@example.test", password_hash=hash_password("secret"))
            approver = User(email="policy-approver@example.test", password_hash=hash_password("secret"))
            outsider = User(email="policy-outsider@example.test", password_hash=hash_password("secret"))
            session.add_all([other, operator, approver, outsider])
            session.flush()
            session.add_all([
                WorkspaceMembership(user_id=operator.id, workspace_id=workspace.id, role="operator"),
                WorkspaceMembership(user_id=operator.id, workspace_id=workspace.id, role="approver"),
                WorkspaceMembership(user_id=approver.id, workspace_id=workspace.id, role="approver"),
                WorkspaceMembership(user_id=outsider.id, workspace_id=other.id, role="operator"),
            ])
            self.workspace_id = workspace.id
            self.other_workspace_id = other.id
            self.operator_id = operator.id
            self.approver_id = approver.id
            self.outsider_id = outsider.id

        self.provider = StubProvider(response=json.dumps(reference_payload(), ensure_ascii=False))
        self.processing = DocumentProcessingService(self.provider, config=DocumentProcessingConfig())
        self.queue = InlineDocumentQueue()
        self.app = create_app(
            document_processing_service=self.processing,
            order_repository=SqlAlchemyPurchaseOrderRepository(self.sessions),
            document_repository=SqlAlchemyDocumentRepository(self.sessions),
            document_storage=DocumentFileStorage(Path(self.tmp.name)),
            document_queue=self.queue,
            session_factory=self.sessions,
            auth_settings=AuthSettings("commercial-policy-test-secret-key-32-bytes"),
        )
        self.queue.app = self.app
        self.operator = self._client(self.operator_id, self.workspace_id)
        self.approver = self._client(self.approver_id, self.workspace_id)
        self.outsider = self._client(self.outsider_id, self.other_workspace_id)

    def tearDown(self):
        self.operator.close()
        self.approver.close()
        self.outsider.close()
        self.engine.dispose()
        self.tmp.cleanup()

    def _client(self, user_id, workspace_id):
        token, _ = self.app.state.authentication.issue_access_token(user_id)
        return TestClient(self.app, headers={
            "Authorization": f"Bearer {token}", "X-Workspace-ID": str(workspace_id),
        })

    def _upload(self, suffix):
        content = make_pdf([REFERENCE_TEXT]) + f"\n%{suffix}".encode()
        return self.operator.post(
            "/documents/analyze",
            data={"purchase_order_reference": "PC-2026-0001"},
            files={"file": (f"{suffix}.pdf", content, "application/pdf")},
        )

    def test_default_policy_and_successive_changes_keep_version_history(self):
        default = self.operator.get("/commercial-policy")

        self.assertEqual(default.status_code, 200, default.text)
        self.assertEqual(default.json()["version"], 0)
        self.assertEqual(default.json()["default_total_tolerance"], "0.01")
        self.assertEqual(default.json()["currency_tolerances"], {})
        self.assertIsNone(default.json()["created_at"])

        first = self.operator.put("/commercial-policy", json={
            "expected_version": 0, "currency_tolerances": {"BRL": "0.05", "USD": "0.10"},
        })
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["version"], 1)
        self.assertEqual(first.json()["created_by_user_id"], self.operator_id)
        self.assertIsNotNone(first.json()["created_at"])

        second = self.operator.put("/commercial-policy", json={
            "expected_version": 1, "currency_tolerances": {"EUR": "0.03"},
        })
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["version"], 2)
        history = self.operator.get("/commercial-policy/history")
        self.assertEqual([item["version"] for item in history.json()], [2, 1])
        self.assertEqual(history.json()[1]["currency_tolerances"], {"BRL": "0.05", "USD": "0.10"})

    def test_policy_endpoints_require_operator_membership_and_are_workspace_scoped(self):
        self.assertEqual(self.approver.get("/commercial-policy").status_code, 403)
        self.assertEqual(self.approver.put("/commercial-policy", json={
            "expected_version": 0, "currency_tolerances": {},
        }).status_code, 403)
        self.assertEqual(self.operator.put("/commercial-policy", json={
            "expected_version": 0, "currency_tolerances": {"BRL": "0.05"},
        }).status_code, 200)
        outsider_policy = self.outsider.get("/commercial-policy")
        self.assertEqual(outsider_policy.status_code, 200)
        self.assertEqual(outsider_policy.json()["currency_tolerances"], {})
        self.assertEqual(self.outsider.get("/commercial-policy/history").json(), [])
        self.assertEqual(self.operator.get(
            "/commercial-policy", headers={"X-Workspace-ID": str(self.other_workspace_id)}
        ).status_code, 403)

    def test_policy_rejects_invalid_currency_and_tolerance_bounds(self):
        invalid_policies = [
            {"BRL": "-0.01"}, {"BRL": "100.01"}, {"BRL": "0.001"},
            {"brl": "0.05"}, {"BRLL": "0.05"}, {"BRL": 0.05},
        ]
        for tolerances in invalid_policies:
            with self.subTest(tolerances=tolerances):
                response = self.operator.put("/commercial-policy", json={
                    "expected_version": 0, "currency_tolerances": tolerances,
                })
                self.assertEqual(response.status_code, 422, response.text)

        boundary = self.operator.put("/commercial-policy", json={
            "expected_version": 0, "currency_tolerances": {"JPY": "0", "USD": "100.00"},
        })
        self.assertEqual(boundary.status_code, 200, boundary.text)
        self.assertEqual(boundary.json()["currency_tolerances"], {"JPY": "0.00", "USD": "100.00"})
        too_many = {f"{chr(65 + index // 26)}{chr(65 + index % 26)}A": "0.01" for index in range(21)}
        self.assertEqual(self.operator.put("/commercial-policy", json={
            "expected_version": 0, "currency_tolerances": too_many,
        }).status_code, 422)

    def test_stale_expected_version_conflicts_instead_of_overwriting(self):
        payload = {"expected_version": 0, "currency_tolerances": {"BRL": "0.05"}}
        self.assertEqual(self.operator.put("/commercial-policy", json=payload).status_code, 200)

        conflict = self.operator.put("/commercial-policy", json=payload)

        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(self.operator.get("/commercial-policy").json()["version"], 1)

    def test_processing_snapshots_policy_and_policy_change_preserves_document_and_approval_history(self):
        uploaded = self._upload("policy-before-change")
        self.assertEqual(uploaded.status_code, 202, uploaded.text)
        document_id = uploaded.json()["document_id"]
        before = self.operator.get(f"/documents/{document_id}").json()
        self.assertEqual(before["validation_result"]["commercial_policy"]["version"], 0)
        approved = self.operator.post(f"/documents/{document_id}/approve", json={"expected_version": 1})
        self.assertEqual(approved.status_code, 200, approved.text)
        with self.sessions() as session:
            original_report = session.get(DocumentEntity, document_id).validation_result
            original_snapshot = session.scalar(select(DocumentApprovalEntity.validation_result).where(
                DocumentApprovalEntity.document_id == document_id
            ))

        changed = self.operator.put("/commercial-policy", json={
            "expected_version": 0, "currency_tolerances": {"BRL": "0.25"},
        })
        self.assertEqual(changed.status_code, 200, changed.text)
        after = self.operator.get(f"/documents/{document_id}").json()
        self.assertEqual(after["validation_result"]["commercial_policy"]["version"], 0)
        self.assertEqual(after["approval"]["validation_result"]["commercial_policy"]["version"], 0)

        new_upload = self._upload("policy-after-change")
        self.assertEqual(new_upload.status_code, 202, new_upload.text)
        next_detail = self.operator.get(f"/documents/{new_upload.json()['document_id']}").json()
        self.assertEqual(next_detail["validation_result"]["commercial_policy"]["version"], 1)
        with self.sessions() as session:
            self.assertEqual(session.get(DocumentEntity, document_id).validation_result, original_report)
            self.assertEqual(session.scalar(select(DocumentApprovalEntity.validation_result).where(
                DocumentApprovalEntity.document_id == document_id
            )), original_snapshot)


if __name__ == "__main__":
    unittest.main()
