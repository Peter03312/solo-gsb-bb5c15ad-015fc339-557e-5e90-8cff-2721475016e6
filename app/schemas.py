"""HTTP 请求 / 响应模型。所有时间必须携带时区信息，统一转 UTC 存储与计算。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

KindLiteral = Literal["driving", "other_duty", "rest"]


def _require_aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} 必须携带时区偏移（如 2026-10-05T08:00:00Z）")
    return value.astimezone(timezone.utc)


class SegmentInput(BaseModel):
    id: str = Field(min_length=1, description="全局唯一片段 ID")
    driver_id: str = Field(min_length=1)
    start: datetime = Field(description="UTC 起始时间，须带时区")
    end: datetime = Field(description="UTC 结束时间，须带时区")
    kind: KindLiteral

    @field_validator("start", "end")
    @classmethod
    def _aware(cls, v: datetime, info):
        return _require_aware_utc(v, info.field_name)

    def model_post_init(self, __context: Any) -> None:
        if self.end <= self.start:
            raise ValueError("片段时长必须为正：end 必须严格晚于 start")


# 录入与幂等重试共用同一载荷
SegmentCreate = SegmentInput


class SegmentCorrection(BaseModel):
    driver_id: str = Field(min_length=1)
    start: datetime
    end: datetime
    kind: KindLiteral
    expected_revision: int = Field(gt=0, description="预期修订号；与现状不符则拒绝")

    @field_validator("start", "end")
    @classmethod
    def _aware(cls, v: datetime, info):
        return _require_aware_utc(v, info.field_name)

    def model_post_init(self, __context: Any) -> None:
        if self.end <= self.start:
            raise ValueError("片段时长必须为正：end 必须严格晚于 start")


class SegmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    driver_id: str
    start: datetime
    end: datetime
    kind: str
    revision: int
    created_at: datetime
    updated_at: datetime


class ParticipantResponse(BaseModel):
    segment_id: str
    started_at: datetime
    ended_at: datetime
    kind: str
    contribution_seconds: float
    intersects_violation_at: bool


class ViolationResponse(BaseModel):
    rule: str
    title: str
    first_exceed_at: datetime
    limit_seconds: float
    window_start_at: datetime
    window_end_at: datetime
    cumulative_seconds_at_exceed: float
    participants: list[ParticipantResponse]
    qualifying_rest_segment_ids: list[str] = []


class ComplianceReportResponse(BaseModel):
    driver_id: str
    computed_at: datetime
    segment_count: int
    compliant: bool
    violations: list[ViolationResponse]


class PublishRequest(BaseModel):
    driver_id: str = Field(min_length=1)


class SegmentManifestEntry(BaseModel):
    id: str
    revision: int


class PublicationResponse(BaseModel):
    id: int
    driver_id: str
    version: int
    input_hash: str
    segment_manifest: list[SegmentManifestEntry]
    conclusion: dict
    compliant: bool
    published_at: datetime
    idempotent: bool = False


class PublicationSummary(BaseModel):
    id: int
    driver_id: str
    version: int
    input_hash: str
    compliant: bool
    published_at: datetime


class ViolationDiff(BaseModel):
    rule: str
    status: Literal["new", "resolved", "unchanged", "changed"]
    before: dict | None = None
    after: dict | None = None


class PublicationDiffResponse(BaseModel):
    driver_id: str
    base_version: int | None
    target_version: int
    compliant_before: bool | None
    compliant_after: bool
    segment_revisions_changed: bool
    violation_diffs: list[ViolationDiff]
