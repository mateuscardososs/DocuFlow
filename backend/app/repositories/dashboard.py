"""Consultas agregadas do painel, sempre limitadas ao workspace."""

from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import JSON, String, and_, case, cast, func, select, text
from sqlalchemy.orm import sessionmaker

from backend.app.models.document import DocumentEntity
from backend.app.models.erp_submission import ERPSubmissionEntity


class SqlAlchemyDashboardRepository:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def summarize(self, workspace_id: int, start_date: date, end_date: date) -> dict[str, Any]:
        start = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
        end = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone.utc)
        with self._session_factory() as session:
            dialect = session.get_bind().dialect.name
            reviewed_is_null = (
                cast(DocumentEntity.reviewed_document, String) == "null"
                if dialect == "postgresql"
                else and_(
                    DocumentEntity.reviewed_document.is_not(None),
                    DocumentEntity.reviewed_document == JSON.NULL,
                )
            )
            no_saved_review = DocumentEntity.reviewed_document.is_(None) | reviewed_is_null
            document_counts = session.execute(
                select(
                    func.sum(case((DocumentEntity.processing_status == "queued", 1), else_=0)),
                    func.sum(case((DocumentEntity.processing_status == "processing", 1), else_=0)),
                    func.sum(case((
                        (DocumentEntity.processing_status == "ready_for_review")
                        & (DocumentEntity.review_status == "pending")
                        & no_saved_review, 1
                    ), else_=0)),
                    func.sum(case((
                        (DocumentEntity.processing_status == "ready_for_review")
                        & (DocumentEntity.review_status == "pending")
                        & ~no_saved_review, 1
                    ), else_=0)),
                    func.sum(case((DocumentEntity.processing_status == "failed", 1), else_=0)),
                ).where(DocumentEntity.workspace_id == workspace_id)
            ).one()

            volume_rows = session.execute(
                select(func.date(DocumentEntity.uploaded_at), func.count(DocumentEntity.id))
                .where(
                    DocumentEntity.workspace_id == workspace_id,
                    DocumentEntity.uploaded_at >= start,
                    DocumentEntity.uploaded_at < end,
                )
                .group_by(func.date(DocumentEntity.uploaded_at))
                .order_by(func.date(DocumentEntity.uploaded_at))
            ).all()
            volume = [
                {"date": value if isinstance(value, date) else date.fromisoformat(str(value)), "count": count}
                for value, count in volume_rows
            ]

            erp_rows = session.execute(
                select(ERPSubmissionEntity.state, func.count(ERPSubmissionEntity.id))
                .where(
                    ERPSubmissionEntity.workspace_id == workspace_id,
                    ERPSubmissionEntity.requested_at >= start,
                    ERPSubmissionEntity.requested_at < end,
                )
                .group_by(ERPSubmissionEntity.state)
            ).all()
            erp = {state: count for state, count in erp_rows}

            if dialect == "postgresql":
                divergence_sql = """
                    SELECT finding.value->>'code' AS code, COUNT(*) AS count
                    FROM documents AS document
                    CROSS JOIN LATERAL json_array_elements(
                        COALESCE(document.validation_result->'findings', '[]'::json)
                    ) AS finding(value)
                    WHERE document.workspace_id = :workspace_id
                      AND document.uploaded_at >= :start
                      AND document.uploaded_at < :end
                      AND finding.value->>'severity' = 'divergence'
                    GROUP BY finding.value->>'code'
                    ORDER BY count DESC, code ASC
                    LIMIT 5
                """
            else:
                divergence_sql = """
                    SELECT json_extract(finding.value, '$.code') AS code, COUNT(*) AS count
                    FROM documents AS document,
                         json_each(document.validation_result, '$.findings') AS finding
                    WHERE document.workspace_id = :workspace_id
                      AND document.uploaded_at >= :start
                      AND document.uploaded_at < :end
                      AND json_extract(finding.value, '$.severity') = 'divergence'
                    GROUP BY json_extract(finding.value, '$.code')
                    ORDER BY count DESC, code ASC
                    LIMIT 5
                """
            divergence_rows = session.execute(
                text(divergence_sql),
                {"workspace_id": workspace_id, "start": start, "end": end},
            ).all()

        return {
            "documents": {
                "queued": document_counts[0] or 0,
                "processing": document_counts[1] or 0,
                "awaiting_review": document_counts[2] or 0,
                "awaiting_approval": document_counts[3] or 0,
                "processing_failed": document_counts[4] or 0,
            },
            "erp_submissions": {
                "pending": erp.get("queued", 0) + erp.get("processing", 0),
                "succeeded": erp.get("succeeded", 0),
                "failed": erp.get("failed", 0),
            },
            "volume_total": sum(point["count"] for point in volume),
            "document_volume": volume,
            "common_divergences": [
                {"code": row.code, "count": row.count} for row in divergence_rows if row.code
            ],
        }
