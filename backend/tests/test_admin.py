import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.models.base import Base
from backend.app.models.identity import User, Workspace, WorkspaceMembership


class AdminCommandTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False)
        with self.session_factory.begin() as session:
            session.add(Workspace(slug="demo", name="Workspace de demonstração"))

    def tearDown(self):
        self.engine.dispose()

    def test_user_is_created_with_argon2_hash_and_multiple_workspace_roles(self):
        from backend.app.admin import create_user

        create_user(
            self.session_factory,
            email=" Operator@Example.Test ",
            workspace_slug="demo",
            roles=["operator", "approver"],
            password="correct horse battery staple",
        )

        with self.session_factory() as session:
            user = session.scalar(select(User))
            roles = set(session.scalars(select(WorkspaceMembership.role)))
        self.assertEqual(user.email, "operator@example.test")
        self.assertTrue(user.password_hash.startswith("$argon2id$"))
        self.assertNotEqual(user.password_hash, "correct horse battery staple")
        self.assertEqual(roles, {"operator", "approver"})

    def test_create_user_requires_existing_workspace_and_valid_roles(self):
        from backend.app.admin import create_user

        with self.assertRaisesRegex(ValueError, "workspace"):
            create_user(
                self.session_factory,
                email="user@example.test",
                workspace_slug="missing",
                roles=["operator"],
                password="correct horse battery staple",
            )
        with self.assertRaisesRegex(ValueError, "papel"):
            create_user(
                self.session_factory,
                email="user@example.test",
                workspace_slug="demo",
                roles=["admin"],
                password="correct horse battery staple",
            )

    def test_existing_user_can_be_associated_with_another_workspace(self):
        from backend.app.admin import add_membership, create_user, create_workspace

        create_user(
            self.session_factory,
            email="member@example.test",
            workspace_slug="demo",
            roles=["operator"],
            password="correct horse battery staple",
        )
        create_workspace(self.session_factory, slug="branch", name="Filial")
        add_membership(
            self.session_factory,
            email="member@example.test",
            workspace_slug="branch",
            roles=["approver", "operator"],
        )

        with self.session_factory() as session:
            user = session.scalar(select(User).where(User.email == "member@example.test"))
            memberships = session.scalars(
                select(WorkspaceMembership).where(WorkspaceMembership.user_id == user.id)
            ).all()
        self.assertEqual(len(memberships), 3)


if __name__ == "__main__":
    unittest.main()
