"""HTTP 路由：值勤片段录入/更正/查询、合规重算、审核发布与历史对照。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from . import storage
from .compliance.diff import ConclusionView, diff_views
from .db import get_session
from .schemas import (
    ComplianceReportResponse,
    PublicationDiffResponse,
    PublicationResponse,
    PublicationSummary,
    PublishRequest,
    SegmentCorrection,
    SegmentCreate,
    SegmentResponse,
)

router = APIRouter(prefix="/api/v1")


def _segment_response(row: dict) -> SegmentResponse:
    return SegmentResponse(
        id=row["id"],
        driver_id=row["driver_id"],
        start=row["started_at"],
        end=row["ended_at"],
        kind=row["kind"],
        revision=row["revision"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ---------------------------------------------------------------------------
# 值勤片段
# ---------------------------------------------------------------------------


@router.post("/segments", response_model=SegmentResponse, status_code=201)
def create_segment(payload: SegmentCreate, response: Response, session: Session = Depends(get_session)):
    """录入片段。同 ID 同内容重试幂等（200）；异内容冲突（409）。"""
    row, idempotent = storage.create_segment(session, payload)
    if idempotent:
        response.status_code = status.HTTP_200_OK
    return _segment_response(row)


@router.put("/segments/{segment_id}", response_model=SegmentResponse)
def correct_segment(
    segment_id: str,
    payload: SegmentCorrection,
    response: Response,
    session: Session = Depends(get_session),
):
    """按片段 ID 与预期修订号更正；版本不符拒绝（409）。同内容重试幂等（200）。"""
    row, idempotent = storage.correct_segment(session, segment_id, payload)
    response.status_code = status.HTTP_200_OK
    return _segment_response(row)


@router.get("/segments/{segment_id}", response_model=SegmentResponse)
def get_segment(segment_id: str, session: Session = Depends(get_session)):
    row = storage.get_segment(session, segment_id)
    if row is None:
        raise storage.NotFoundError(f"片段 {segment_id} 不存在")
    return _segment_response(row)


@router.get("/drivers/{driver_id}/segments", response_model=list[SegmentResponse])
def list_driver_segments(driver_id: str, session: Session = Depends(get_session)):
    return [_segment_response(r) for r in storage.list_segments(session, driver_id)]


# ---------------------------------------------------------------------------
# 合规核算（按现存记录实时重算）
# ---------------------------------------------------------------------------


@router.get(
    "/drivers/{driver_id}/compliance",
    response_model=ComplianceReportResponse,
)
def driver_compliance(driver_id: str, session: Session = Depends(get_session)):
    """返回违规类型、首次越限时刻、参与片段；补录/更正后自动按现存记录重算。"""
    return storage.compute_report(session, driver_id)


# ---------------------------------------------------------------------------
# 审核发布与历史版本
# ---------------------------------------------------------------------------


@router.post("/publications", response_model=PublicationResponse, status_code=201)
def publish(payload: PublishRequest, response: Response, session: Session = Depends(get_session)):
    """固化当前输入修订与结论。同修订并发/重试发布只留一版（幂等返回 200）。"""
    row, idempotent = storage.publish_driver(session, payload.driver_id)
    if idempotent:
        response.status_code = status.HTTP_200_OK
    return PublicationResponse(
        id=row["id"],
        driver_id=row["driver_id"],
        version=row["version"],
        input_hash=row["input_hash"],
        segment_manifest=row["segment_manifest"],
        conclusion=row["conclusion"],
        compliant=row["compliant"],
        published_at=row["published_at"],
        idempotent=idempotent,
    )


@router.get(
    "/drivers/{driver_id}/publications",
    response_model=list[PublicationSummary],
)
def list_publications(driver_id: str, session: Session = Depends(get_session)):
    return storage.list_publications(session, driver_id)


@router.get(
    "/drivers/{driver_id}/publications/{version}",
    response_model=PublicationResponse,
)
def get_publication(driver_id: str, version: int, session: Session = Depends(get_session)):
    row = storage.get_publication(session, driver_id, version)
    return PublicationResponse(
        id=row["id"],
        driver_id=row["driver_id"],
        version=row["version"],
        input_hash=row["input_hash"],
        segment_manifest=row["segment_manifest"],
        conclusion=row["conclusion"],
        compliant=row["compliant"],
        published_at=row["published_at"],
    )


@router.get(
    "/drivers/{driver_id}/compliance/diff",
    response_model=PublicationDiffResponse,
)
def diff_current(
    driver_id: str,
    base_version: int,
    session: Session = Depends(get_session),
):
    """当前实时结果 vs 指定历史发布版本（base=历史版本，target=当前）。"""
    pub = storage.get_publication(session, driver_id, base_version)
    current_conclusion = storage.compute_report(session, driver_id)
    current_manifest = [
        {"id": r["id"], "revision": r["revision"]}
        for r in storage.list_segments(session, driver_id)
    ]
    return diff_views(
        driver_id,
        base=ConclusionView(
            version=pub["version"],
            conclusion=pub["conclusion"],
            manifest=[
                {"id": e["id"], "revision": e["revision"]}
                for e in pub["segment_manifest"]
            ],
        ),
        target=ConclusionView(
            version=None,
            conclusion=current_conclusion,
            manifest=current_manifest,
        ),
    )


@router.get(
    "/drivers/{driver_id}/publications/{version}/diff",
    response_model=PublicationDiffResponse,
)
def diff_versions(
    driver_id: str,
    version: int,
    base_version: int | None = None,
    session: Session = Depends(get_session),
):
    """两个历史版本对照：target=version，base=base_version（缺省为 version-1）。"""
    if base_version is None:
        base_version = version - 1
    if base_version >= version:
        raise storage.ConflictError(
            "invalid_diff_order",
            f"base_version({base_version}) 必须早于 target version({version})",
        )
    target = storage.get_publication(session, driver_id, version)
    base = storage.get_publication(session, driver_id, base_version)
    return diff_views(
        driver_id,
        base=ConclusionView(
            version=base["version"],
            conclusion=base["conclusion"],
            manifest=[
                {"id": e["id"], "revision": e["revision"]}
                for e in base["segment_manifest"]
            ],
        ),
        target=ConclusionView(
            version=target["version"],
            conclusion=target["conclusion"],
            manifest=[
                {"id": e["id"], "revision": e["revision"]}
                for e in target["segment_manifest"]
            ],
        ),
    )
