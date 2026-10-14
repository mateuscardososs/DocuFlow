"""Cobertura do agregado operacional sem serviços externos."""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.auth import AuthSettings, hash_password
from backend.app.models.base import Base
from backend.app.models.document import DocumentEntity
from backend.app.models.erp_submission import ERPSubmissionEntity
from backend.app.models.identity import User, Workspace, WorkspaceMembership
from backend.app.models.purchase_order import PurchaseOrderEntity
from backend.app.repositories.documents import SqlAlchemyDocumentRepository
from backend.app.repositories.purchase_orders import SqlAlchemyPurchaseOrderRepository
from backend.app.seed_reference_order import seed_reference_order
from backend.app.services.document_storage import DocumentFileStorage
from backend.tests.inline_queue import InlineDocumentQueue


class DashboardTests(unittest.TestCase):
    def setUp(self):
        from backend.app.api.app import create_app

        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        seed_reference_order(self.sessions)
        with self.sessions.begin() as session:
            first = session.scalar(select(Workspace).where(Workspace.slug == "demo"))
            second = Workspace(slug="other", name="Outro workspace")
            user = User(email="dashboard@example.test", password_hash=hash_password("secret"))
            session.add_all([second, user])
            session.flush()
            session.add_all([
                WorkspaceMembership(user_id=user.id, workspace_id=first.id, role="operator"),
                WorkspaceMembership(user_id=user.id, workspace_id=second.id, role="operator"),
            ])
            self.workspace_id, self.other_workspace_id, self.user_id = first.id, second.id, user.id
            order_id = session.scalar(
                select(PurchaseOrderEntity.id).where(PurchaseOrderEntity.workspace_id == first.id)
            )
            other_order = session.scalar(
                select(PurchaseOrderEntity.id).where(PurchaseOrderEntity.workspace_id == second.id)
            )
            if other_order is None:
                from backend.app.models.purchase_order import Supplier, PurchaseOrderItem
                from decimal import Decimal

                supplier = Supplier(workspace_id=second.id, name="Fornecedor", identifier="F-2")
                session.add(supplier)
                session.flush()
                second_po = PurchaseOrderEntity(
                    workspace_id=second.id, supplier_id=supplier.id, reference="OTHER-1",
                    currency="BRL", total=Decimal("1.00"),
                    items=[PurchaseOrderItem(code="X", description="X", quantity=Decimal("1"), unit_price=Decimal("1"), line_total=Decimal("1"))],
                )
                session.add(second_po)
                session.flush()
                other_order = second_po.id
            self._add_document(session, first.id, order_id, "queued", "pending", "2026-10-13", None)
            self._add_document(session, first.id, order_id, "processing", "pending", "2026-10-13", None)
            self._add_document(session, first.id, order_id, "ready_for_review", "pending", "2026-10-14", None,
                               [{"severity": "divergence", "code": "price_mismatch", "message": "x"}])
            self._add_document(session, first.id, order_id, "ready_for_review", "pending", "2026-10-14", {"saved": True},
                               [{"severity": "divergence", "code": "price_mismatch", "message": "x"}, {"severity": "warning", "code": "missing_note", "message": "x"}])
            self._add_document(session, first.id, order_id, "failed", "pending", "2026-10-14", None)
            self._add_document(session, first.id, order_id, "ready_for_review", "approved", "2026-10-14", {"saved": True})
            foreign = self._add_document(session, second.id, other_order, "failed", "pending", "2026-10-14", None,
                                         [{"severity": "divergence", "code": "foreign_secret", "message": "x"}])
            for state in ("queued", "processing", "succeeded", "failed"):
                session.add(ERPSubmissionEntity(
                    workspace_id=first.id,
                    document_id=self._add_document(session, first.id, order_id, "ready_for_review", "approved", "2026-10-14", {"saved": True}).id,
                    approved_version=1,
                    requested_by_user_id=user.id,
                    state=state,
                ))
            session.add(ERPSubmissionEntity(
                workspace_id=second.id, document_id=foreign.id, approved_version=1,
                requested_by_user_id=user.id, state="failed",
            ))

        queue = InlineDocumentQueue()
        self.tempdir = tempfile.TemporaryDirectory()
        self.app = create_app(
            order_repository=SqlAlchemyPurchaseOrderRepository(self.sessions),
            document_repository=SqlAlchemyDocumentRepository(self.sessions),
            document_storage=DocumentFileStorage(Path(self.tempdir.name)),
            document_queue=queue,
            session_factory=self.sessions,
            auth_settings=AuthSettings("dashboard-test-secret-key-with-32-bytes"),
        )
        token, _ = self.app.state.authentication.issue_access_token(self.user_id)
        self.client = TestClient(self.app, headers={
            "Authorization": f"Bearer {token}", "X-Workspace-ID": str(self.workspace_id),
        })

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()
        self.engine.dispose()

    def _add_document(self, session, workspace_id, order_id, processing, review, day, reviewed, findings=None):
        document = DocumentEntity(
            workspace_id=workspace_id, purchase_order_id=order_id,
            original_filename="private.pdf", size_bytes=10, content_type="application/pdf",
            sha256=f"{workspace_id}-{processing}-{review}-{day}-{id(reviewed)}-{session.new.__len__()}",
            storage_key="private/random", uploaded_at=datetime.fromisoformat(day).replace(tzinfo=timezone.utc),
            processing_status=processing, review_status=review, review_version=1,
            reviewed_document=reviewed,
            validation_result={"status": "divergent", "findings": findings or []},
            final_status="divergent",
        )
        session.add(document)
        session.flush()
        return document

    def test_dashboard_aggregates_workspace_counts_period_volume_and_findings(self):
        response = self.client.get("/dashboard?start_date=2026-10-13&end_date=2026-10-14")

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["documents"], {
            "queued": 1, "processing": 1, "awaiting_review": 1,
            "awaiting_approval": 1, "processing_failed": 1,
        })
        self.assertEqual(result["erp_submissions"], {"pending": 2, "succeeded": 1, "failed": 1})
        self.assertEqual(result["volume_total"], 10)
        self.assertEqual(result["document_volume"], [
            {"date": "2026-10-13", "count": 2}, {"date": "2026-10-14", "count": 8},
        ])
        self.assertEqual(sum(point["count"] for point in result["document_volume"]), result["volume_total"])
        self.assertEqual(
            result["documents"]["awaiting_review"] + result["documents"]["awaiting_approval"], 2
        )
        self.assertEqual(result["common_divergences"][0], {"code": "price_mismatch", "count": 2})
        self.assertNotIn("foreign_secret", json.dumps(result))
        self.assertNotIn("private.pdf", json.dumps(result))

        isolated = self.client.get(
            "/dashboard?start_date=2026-10-13&end_date=2026-10-14",
            headers={"X-Workspace-ID": str(self.other_workspace_id)},
        ).json()
        self.assertEqual(isolated["documents"]["processing_failed"], 1)
        self.assertEqual(isolated["common_divergences"], [{"code": "foreign_secret", "count": 1}])

    def test_dashboard_returns_zeroes_for_empty_period(self):
        response = self.client.get("/dashboard?start_date=2020-01-01&end_date=2020-01-07")

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["volume_total"], 0)
        self.assertEqual(result["document_volume"], [])
        self.assertEqual(result["common_divergences"], [])
        self.assertTrue(all(value == 0 for value in result["erp_submissions"].values()))

    def test_dashboard_requires_workspace_membership(self):
        response = self.client.get(
            "/dashboard?start_date=2026-10-13&end_date=2026-10-14",
            headers={"X-Workspace-ID": "99999"},
        )

        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
