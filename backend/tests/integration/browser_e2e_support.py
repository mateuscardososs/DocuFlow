"""Fixtures identificáveis e limpeza limitada ao cenário Playwright."""

import base64
import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

from alembic import command
from alembic.config import Config
from redis import Redis
from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from backend.app.auth import hash_password
from backend.app.models.document import (
    DocumentApprovalEntity,
    DocumentAuditEventEntity,
    DocumentEntity,
    DocumentReviewRevisionEntity,
)
from backend.app.models.erp_submission import ERPSubmissionEntity
from backend.app.models.identity import User, Workspace, WorkspaceMembership
from backend.app.models.purchase_order import PurchaseOrderEntity, PurchaseOrderItem, Supplier
from backend.tests.test_pdf_text_extractor import REFERENCE_TEXT, make_pdf


ROOT = Path(__file__).resolve().parents[2]


def _database_url() -> str:
    value = os.environ.get("TEST_DATABASE_URL", "")
    url = make_url(value)
    if (
        url.get_backend_name() != "postgresql"
        or url.host not in {"localhost", "127.0.0.1", "::1"}
        or url.database != "docuflow_test"
    ):
        raise ValueError("TEST_DATABASE_URL precisa apontar ao PostgreSQL local docuflow_test.")
    app_value = os.environ.get("DOCUFLOW_TEST_MAIN_DATABASE_URL")
    if app_value is None:
        app_value = os.getenv("DATABASE_URL")
    if app_value:
        app_url = make_url(app_value)
        if (
            app_url.get_backend_name() == "postgresql"
            and app_url.host == url.host
            and (app_url.port or 5432) == (url.port or 5432)
            and app_url.database == url.database
        ):
            raise ValueError("docuflow_test não pode ser a mesma base configurada para a aplicação.")
    return value


def _redis_url() -> str:
    value = os.environ.get("TEST_REDIS_URL", "")
    parsed = urlparse(value)
    if parsed.scheme not in {"redis", "rediss"} or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("TEST_REDIS_URL precisa apontar para Redis local.")
    test_db = parsed.path.strip("/") or "0"
    for main_name, runtime_name in (
        ("DOCUFLOW_TEST_MAIN_REDIS_URL", "REDIS_URL"),
        ("DOCUFLOW_TEST_MAIN_ERP_REDIS_URL", "ERP_REDIS_URL"),
    ):
        main_url = os.environ.get(main_name)
        if main_url is None:
            main_url = os.getenv(runtime_name, "")
        normal = urlparse(main_url)
        if normal.scheme and (
            parsed.hostname == normal.hostname
            and (parsed.port or 6379) == (normal.port or 6379)
            and test_db == (normal.path.strip("/") or "0")
        ):
            raise ValueError("TEST_REDIS_URL precisa usar uma base lógica diferente da fila normal.")
    return value


def check() -> int:
    try:
        database_url = _database_url()
        redis_url = _redis_url()
    except Exception as error:
        print(str(error))
        return 3

    engine = create_engine(database_url, pool_pre_ping=True)
    redis = Redis.from_url(redis_url)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
        redis.ping()
        return 0
    except Exception:
        print("Ignorado: PostgreSQL docuflow_test ou Redis configurado em TEST_REDIS_URL está indisponível.")
        return 2
    finally:
        redis.close()
        engine.dispose()


def _migrate(database_url: str) -> None:
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        config = Config(str(ROOT / "alembic.ini"))
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    finally:
        engine.dispose()


def setup() -> None:
    database_url = _database_url()
    _redis_url()
    run_id = os.environ["DOCUFLOW_TEST_RUN_ID"]
    email = os.environ["DOCUFLOW_TEST_USER_EMAIL"]
    password = os.environ["DOCUFLOW_TEST_USER_PASSWORD"]
    order_reference = os.environ["DOCUFLOW_TEST_ORDER_REFERENCE"]
    pdf_path = Path(os.environ["DOCUFLOW_TEST_PDF_PATH"])
    if not password or not run_id or not order_reference.startswith("BROWSER-"):
        raise ValueError("Dados temporários do teste de navegador estão incompletos.")

    _migrate(database_url)
    engine = create_engine(database_url, pool_pre_ping=True)
    sessions = sessionmaker(engine, expire_on_commit=False)
    try:
        with sessions.begin() as session:
            user = User(email=email, password_hash=hash_password(password))
            target = Workspace(
                slug=f"browser-e2e-{run_id}",
                name=f"DocuFlow navegador {run_id}",
            )
            decoy = Workspace(
                slug=f"browser-e2e-aux-{run_id}",
                name=f"Workspace auxiliar {run_id}",
            )
            session.add_all([user, target, decoy])
            session.flush()
            session.add_all([
                WorkspaceMembership(user_id=user.id, workspace_id=target.id, role="operator"),
                WorkspaceMembership(user_id=user.id, workspace_id=target.id, role="approver"),
                WorkspaceMembership(user_id=user.id, workspace_id=decoy.id, role="operator"),
            ])
            supplier = Supplier(
                workspace_id=target.id,
                name="Metalúrgica Horizonte Ltda.",
                identifier="FORN-0001",
            )
            session.add(supplier)
            session.flush()
            order = PurchaseOrderEntity(
                workspace_id=target.id,
                reference=order_reference,
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
            session.add(order)
            target_name = target.name

        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        text = REFERENCE_TEXT.replace("PC-2026-0001", order_reference)
        pdf = make_pdf([text])
        pdf_path.write_bytes(pdf)
        print(json.dumps({
            "email": email,
            "workspace_name": target_name,
            "order_reference": order_reference,
            "pdf_base64": base64.b64encode(pdf).decode("ascii"),
        }))
    finally:
        engine.dispose()


def cleanup_redis_queues() -> None:
    from rq import Queue
    from rq.job import Job
    from rq.registry import (
        CanceledJobRegistry,
        DeferredJobRegistry,
        FailedJobRegistry,
        FinishedJobRegistry,
        ScheduledJobRegistry,
        StartedJobRegistry,
    )

    redis = Redis.from_url(_redis_url())
    registry_types = (
        StartedJobRegistry, FinishedJobRegistry, FailedJobRegistry,
        DeferredJobRegistry, ScheduledJobRegistry, CanceledJobRegistry,
    )
    try:
        for env_name in ("TEST_RQ_QUEUE_NAME", "TEST_ERP_RQ_QUEUE_NAME"):
            queue_name = os.environ.get(env_name, "")
            if not queue_name.startswith("docuflow_browser_"):
                continue
            queue = Queue(queue_name, connection=redis)
            job_ids = set(queue.job_ids)
            registries = [registry_type(queue_name, connection=redis) for registry_type in registry_types]
            for registry in registries:
                job_ids.update(registry.get_job_ids())
            for job_id in job_ids:
                try:
                    Job.fetch(job_id, connection=redis).delete(remove_from_queue=True)
                except Exception:
                    pass
            queue.empty()
            for registry in registries:
                redis.delete(registry.key)
            redis.delete(queue.key)
    finally:
        redis.close()


def cleanup_database() -> None:
    database_url = _database_url()
    run_id = os.environ["DOCUFLOW_TEST_RUN_ID"]
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            target_id = connection.scalar(select(Workspace.id).where(Workspace.slug == f"browser-e2e-{run_id}"))
            decoy_id = connection.scalar(select(Workspace.id).where(Workspace.slug == f"browser-e2e-aux-{run_id}"))
            user_id = connection.scalar(select(User.id).where(User.email == os.environ["DOCUFLOW_TEST_USER_EMAIL"]))
            workspace_ids = [row_id for row_id in (target_id, decoy_id) if row_id is not None]
            if workspace_ids:
                document_ids = list(connection.scalars(
                    select(DocumentEntity.id).where(DocumentEntity.workspace_id.in_(workspace_ids))
                ))
                if document_ids:
                    connection.execute(delete(ERPSubmissionEntity).where(ERPSubmissionEntity.document_id.in_(document_ids)))
                    connection.execute(delete(DocumentAuditEventEntity).where(DocumentAuditEventEntity.document_id.in_(document_ids)))
                    connection.execute(delete(DocumentApprovalEntity).where(DocumentApprovalEntity.document_id.in_(document_ids)))
                    connection.execute(delete(DocumentReviewRevisionEntity).where(DocumentReviewRevisionEntity.document_id.in_(document_ids)))
                    connection.execute(delete(DocumentEntity).where(DocumentEntity.id.in_(document_ids)))
                order_ids = list(connection.scalars(
                    select(PurchaseOrderEntity.id).where(PurchaseOrderEntity.workspace_id.in_(workspace_ids))
                ))
                supplier_ids = list(connection.scalars(
                    select(Supplier.id).where(Supplier.workspace_id.in_(workspace_ids))
                ))
                if order_ids:
                    connection.execute(delete(PurchaseOrderItem).where(PurchaseOrderItem.purchase_order_id.in_(order_ids)))
                    connection.execute(delete(PurchaseOrderEntity).where(PurchaseOrderEntity.id.in_(order_ids)))
                if supplier_ids:
                    connection.execute(delete(Supplier).where(Supplier.id.in_(supplier_ids)))
                connection.execute(delete(WorkspaceMembership).where(WorkspaceMembership.workspace_id.in_(workspace_ids)))
                connection.execute(delete(Workspace).where(Workspace.id.in_(workspace_ids)))
            if user_id is not None:
                connection.execute(delete(WorkspaceMembership).where(WorkspaceMembership.user_id == user_id))
                connection.execute(delete(User).where(User.id == user_id))
    finally:
        engine.dispose()


def cleanup() -> None:
    try:
        cleanup_redis_queues()
    finally:
        cleanup_database()


if __name__ == "__main__":
    command_name = sys.argv[1] if len(sys.argv) > 1 else ""
    if command_name == "check":
        raise SystemExit(check())
    if command_name == "setup":
        setup()
    elif command_name == "cleanup":
        cleanup()
    else:
        raise SystemExit("Uso: python -m backend.tests.integration.browser_e2e_support check|setup|cleanup")
