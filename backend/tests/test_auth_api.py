import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import jwt
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.auth import AuthSettings, hash_password
from backend.app.models.base import Base
from backend.app.repositories.documents import SqlAlchemyDocumentRepository
from backend.app.models.identity import User, Workspace, WorkspaceMembership
from backend.app.models.purchase_order import PurchaseOrderEntity, PurchaseOrderItem, Supplier
from backend.app.repositories.purchase_orders import SqlAlchemyPurchaseOrderRepository
from backend.app.services.document_storage import DocumentFileStorage
from backend.app.services.document_processing import (
    DocumentProcessingConfig,
    DocumentProcessingService,
)
from backend.tests.test_document_processing import reference_payload
from backend.tests.test_pdf_text_extractor import REFERENCE_TEXT, make_pdf
from backend.tests.inline_queue import InlineDocumentQueue


JWT_SECRET = "test-only-secret-key-with-at-least-32-bytes"


class StubProvider:
    def __init__(self):
        self.calls = 0

    def extract(self, *, prompt, output_schema):
        self.calls += 1
        return json.dumps(reference_payload(), ensure_ascii=False)


class AuthApiTests(unittest.TestCase):
    def setUp(self):
        self.storage_dir = tempfile.TemporaryDirectory()
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False)
        self.auth_settings = AuthSettings(
            jwt_secret_key=JWT_SECRET,
            access_token_expire_minutes=30,
        )
        self.provider = StubProvider()
        self.app = self._make_app()
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()
        self.storage_dir.cleanup()

    def _make_app(self):
        from backend.app.api.app import create_app

        service = DocumentProcessingService(self.provider, config=DocumentProcessingConfig())
        queue = InlineDocumentQueue()
        app = create_app(
            document_processing_service=service,
            order_repository=SqlAlchemyPurchaseOrderRepository(self.session_factory),
            document_repository=SqlAlchemyDocumentRepository(self.session_factory),
            document_storage=DocumentFileStorage(Path(self.storage_dir.name)),
            document_queue=queue,
            session_factory=self.session_factory,
            auth_settings=self.auth_settings,
        )
        queue.app = app
        return app

    def _add_member(self, email, slug, roles, *, with_order=False):
        with self.session_factory.begin() as session:
            user = User(email=email, password_hash=hash_password("correct horse battery staple"))
            workspace = Workspace(slug=slug, name=f"Workspace {slug}")
            session.add_all([user, workspace])
            session.flush()
            for role in roles:
                session.add(
                    WorkspaceMembership(user_id=user.id, workspace_id=workspace.id, role=role)
                )
            if with_order:
                supplier = Supplier(
                    workspace_id=workspace.id,
                    name="Metalúrgica Horizonte Ltda.",
                    identifier="FORN-0001",
                )
                session.add(supplier)
                session.flush()
                session.add(
                    PurchaseOrderEntity(
                        workspace_id=workspace.id,
                        reference="PC-2026-0001",
                        supplier_id=supplier.id,
                        currency="BRL",
                        total=Decimal("1250.00"),
                        items=[
                            PurchaseOrderItem(
                                code="ARR-001",
                                description="Arruela zincada M8",
                                quantity=Decimal("10"),
                                unit_price=Decimal("85.50"),
                                line_total=Decimal("855.00"),
                            ),
                            PurchaseOrderItem(
                                code="PAR-002",
                                description="Parafuso sextavado M8",
                                quantity=Decimal("5"),
                                unit_price=Decimal("79.00"),
                                line_total=Decimal("395.00"),
                            ),
                        ],
                    )
                )
            return user.id, workspace.id

    def _login(self, email="operator@example.test", password="correct horse battery staple"):
        return self.client.post("/auth/token", data={"username": email, "password": password})

    def _analyze(self, token=None, workspace_id=None):
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if workspace_id is not None:
            headers["X-Workspace-ID"] = str(workspace_id)
        return self.client.post(
            "/documents/analyze",
            headers=headers,
            data={"purchase_order_reference": "PC-2026-0001"},
            files={"file": ("documento.pdf", make_pdf([REFERENCE_TEXT]), "application/pdf")},
        )

    def test_valid_login_and_me_return_both_workspace_roles(self):
        self._add_member("operator@example.test", "demo", ["operator", "approver"])

        login = self._login()
        self.assertEqual(login.status_code, 200, login.text)
        self.assertEqual(login.json()["token_type"], "bearer")

        me = self.client.get(
            "/auth/me",
            headers={"Authorization": f"Bearer {login.json()['access_token']}"},
        )
        self.assertEqual(me.status_code, 200, me.text)
        self.assertEqual(me.json()["email"], "operator@example.test")
        self.assertEqual(me.json()["workspaces"][0]["roles"], ["approver", "operator"])

    def test_jwt_secret_must_be_configured_in_environment(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "JWT_SECRET_KEY"):
                AuthSettings.from_environment()

    def test_invalid_password_is_rejected(self):
        self._add_member("operator@example.test", "demo", ["operator"])

        response = self._login(password="wrong password")

        self.assertEqual(response.status_code, 401)
        self.assertNotIn("wrong password", response.text)

    def test_analyze_requires_a_token(self):
        _, workspace_id = self._add_member(
            "operator@example.test", "demo", ["operator"], with_order=True
        )

        response = self._analyze(workspace_id=workspace_id)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.provider.calls, 0)

    def test_invalid_and_expired_tokens_are_rejected(self):
        _, workspace_id = self._add_member(
            "operator@example.test", "demo", ["operator"], with_order=True
        )
        expired = jwt.encode(
            {
                "sub": "1",
                "exp": datetime.now(timezone.utc) - timedelta(minutes=1),
            },
            JWT_SECRET,
            algorithm="HS256",
        )

        for token in ("not-a-jwt", expired):
            with self.subTest(token=token[:10]):
                response = self._analyze(token, workspace_id)
                self.assertEqual(response.status_code, 401)
        self.assertEqual(self.provider.calls, 0)

    def test_approver_without_operator_cannot_process(self):
        _, workspace_id = self._add_member(
            "operator@example.test", "demo", ["approver"], with_order=True
        )
        token = self._login().json()["access_token"]

        response = self._analyze(token, workspace_id)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.provider.calls, 0)

    def test_operator_can_process_order_from_own_workspace(self):
        _, workspace_id = self._add_member(
            "operator@example.test", "demo", ["operator"], with_order=True
        )
        token = self._login().json()["access_token"]

        response = self._analyze(token, workspace_id)

        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["state"], "queued")
        self.assertEqual(self.provider.calls, 1)

    def test_other_workspace_order_is_hidden_and_workspace_access_is_denied(self):
        self._add_member("operator@example.test", "own", ["operator"])
        _, foreign_workspace_id = self._add_member(
            "other@example.test", "foreign", ["operator"], with_order=True
        )
        token = self._login().json()["access_token"]

        response = self._analyze(token, foreign_workspace_id)
        hidden_reference = self._analyze(token, self._workspace_id_for("own"))

        self.assertEqual(response.status_code, 403)
        self.assertEqual(hidden_reference.status_code, 404)
        self.assertEqual(self.provider.calls, 0)

    def _workspace_id_for(self, slug):
        from sqlalchemy import select

        with self.session_factory() as session:
            return session.scalar(select(Workspace.id).where(Workspace.slug == slug))

    def test_user_with_both_roles_can_process(self):
        _, workspace_id = self._add_member(
            "operator@example.test", "demo", ["operator", "approver"], with_order=True
        )
        token = self._login().json()["access_token"]

        response = self._analyze(token, workspace_id)

        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["state"], "queued")


if __name__ == "__main__":
    unittest.main()
