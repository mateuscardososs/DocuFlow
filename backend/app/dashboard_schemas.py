"""Contrato agregado do painel operacional."""

from datetime import date

from pydantic import BaseModel, ConfigDict


class DashboardDocumentCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    queued: int
    processing: int
    awaiting_review: int
    awaiting_approval: int
    processing_failed: int


class DashboardERPCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pending: int
    succeeded: int
    failed: int


class DashboardVolumePoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: date
    count: int


class DashboardDivergenceCount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    count: int


class WorkspaceDashboard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_date: date
    end_date: date
    documents: DashboardDocumentCounts
    erp_submissions: DashboardERPCounts
    volume_total: int
    document_volume: list[DashboardVolumePoint]
    common_divergences: list[DashboardDivergenceCount]
