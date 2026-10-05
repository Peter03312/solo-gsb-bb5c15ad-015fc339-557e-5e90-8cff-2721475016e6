"""持久化操作：片段录入/更正（乐观修订）、按司机现存记录重算、发布固化。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from psycopg2.errors import ExclusionViolation, UniqueViolation
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .compliance.engine import evaluate_driver

UTC = timezone.utc

# ---------------------------------------------------------------------------
# 领域错误
# ---------------------------------------------------------------------------


class ServiceError(Exception):
    def __init__(self, status_code: int, code: str, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail


class NotFoundError(ServiceError):
    def __init__(self, detail: str):
        super().__init__(404, "not_found", detail)


class ConflictError(ServiceError):
    def __init__(self, code: str, detail: str):
        super().__init__(409, code, detail)


class RevisionMismatchError(ServiceError):
    def __init__(self, detail: str, current_revision: int):
        super().__init__(409, "revision_mismatch", detail)
        self.current_revision = current_revision


# ---------------------------------------------------------------------------
# 内容指纹：同 ID 同内容重试幂等，异内容冲突
# ---------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def content_hash(
    driver_id: str, start: datetime, end: datetime, kind: str
) -> str:
    payload = json.dumps(
        {
            "driver_id": driver_id,
            "start": _iso(start),
            "end": _iso(end),
            "kind": kind,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def list_segments(session: Session, driver_id: str) -> list:
    stmt = text(
        """
        SELECT id, driver_id, started_at, ended_at, kind, revision,
               content_hash, created_at, updated_at
        FROM segments
        WHERE driver_id = :driver_id
        ORDER BY started_at, id
        """
    ).bindparams(driver_id=driver_id)
    return list(session.execute(stmt).mappings().all())


def get_segment(session: Session, segment_id: str):
    stmt = text(
        """
        SELECT id, driver_id, started_at, ended_at, kind, revision,
               content_hash, created_at, updated_at
        FROM segments WHERE id = :id
        """
    ).bindparams(id=segment_id)
    return session.execute(stmt).mappings().first()


def _engine_inputs(rows) -> list[dict]:
    return [
        {
            "id": r["id"],
            "driver_id": r["driver_id"],
            "start": r["started_at"],
            "end": r["ended_at"],
            "kind": r["kind"],
            "revision": r["revision"],
        }
        for r in rows
    ]


def compute_report(session: Session, driver_id: str) -> dict:
    """按现存全部片段重算合规结论。"""
    rows = list_segments(session, driver_id)
    report = evaluate_driver(driver_id, _engine_inputs(rows))
    return report.as_dict()


# ---------------------------------------------------------------------------
# 录入：同 ID 同内容幂等，异内容 409；重叠由 GiST 排除约束兜底
# ---------------------------------------------------------------------------


def create_segment(session: Session, data) -> tuple[dict, bool]:
    """返回 (片段行, 是否幂等命中)。"""
    existing = get_segment(session, data.id)
    h = content_hash(data.driver_id, data.start, data.end, data.kind)

    if existing is not None:
        if existing["content_hash"] == h:
            return dict(existing), True
        raise ConflictError(
            "id_conflict",
            f"片段 ID {data.id} 已存在且内容不同（当前修订号 "
            f"{existing['revision']}）；如需修改请使用更正接口",
        )

    stmt = text(
        """
        INSERT INTO segments (id, driver_id, started_at, ended_at, kind, revision, content_hash)
        VALUES (:id, :driver_id, :start, :end, :kind, 1, :hash)
        RETURNING id, driver_id, started_at, ended_at, kind, revision,
                  content_hash, created_at, updated_at
        """
    ).bindparams(
        id=data.id,
        driver_id=data.driver_id,
        start=data.start,
        end=data.end,
        kind=data.kind,
        hash=h,
    )
    try:
        # 保存点：命中约束时只回滚本条语句，外层事务仍可返回错误响应
        savepoint = session.begin_nested()
        row = session.execute(stmt).mappings().first()
        savepoint.commit()
    except IntegrityError as exc:
        return _translate_integrity(session, exc, data.id, h, action="create")
    return dict(row), False


def _translate_integrity(session, exc, segment_id, new_hash, *, action):
    orig = exc.orig
    if isinstance(orig, ExclusionViolation):
        raise ConflictError(
            "segment_overlap",
            "该司机在同一时间段已有值勤片段（相接合法，重叠拒绝）",
        ) from exc
    if isinstance(orig, UniqueViolation):
        # 并发下另一事务刚插入/更新同 ID（PRIMARY KEY）：保存点已自动回滚，
        # 外层事务仍可用，按幂等/冲突再判一次
        fresh = get_segment(session, segment_id)
        if fresh is not None and fresh["content_hash"] == new_hash:
            return dict(fresh), True
        if action == "update":
            raise RevisionMismatchError(
                f"片段 {segment_id} 修订号已变化（并发更正）",
                current_revision=fresh["revision"] if fresh else 0,
            ) from exc
        raise ConflictError(
            "id_conflict",
            f"片段 ID {segment_id} 已存在且内容不同",
        ) from exc
    raise


# ---------------------------------------------------------------------------
# 更正：按片段 ID + 预期修订号；版本不符拒绝；同内容重试幂等
# ---------------------------------------------------------------------------


def correct_segment(session: Session, segment_id: str, data) -> tuple[dict, bool]:
    existing = get_segment(session, segment_id)
    if existing is None:
        raise NotFoundError(f"片段 {segment_id} 不存在")
    if existing["revision"] != data.expected_revision:
        raise RevisionMismatchError(
            f"片段 {segment_id} 修订号不符：期望 {data.expected_revision}，"
            f"现存 {existing['revision']}",
            current_revision=existing["revision"],
        )

    new_hash = content_hash(data.driver_id, data.start, data.end, data.kind)
    if new_hash == existing["content_hash"]:
        # 同内容 + 正确预期修订：幂等无变更
        return dict(existing), True

    stmt = text(
        """
        UPDATE segments
        SET driver_id = :driver_id,
            started_at = :start,
            ended_at = :end,
            kind = :kind,
            revision = revision + 1,
            content_hash = :hash,
            updated_at = now()
        WHERE id = :id AND revision = :expected
        RETURNING id, driver_id, started_at, ended_at, kind, revision,
                  content_hash, created_at, updated_at
        """
    ).bindparams(
        driver_id=data.driver_id,
        start=data.start,
        end=data.end,
        kind=data.kind,
        hash=new_hash,
        id=segment_id,
        expected=data.expected_revision,
    )
    try:
        savepoint = session.begin_nested()
        row = session.execute(stmt).mappings().first()
        savepoint.commit()
    except IntegrityError as exc:
        _translate_integrity(session, exc, segment_id, new_hash, action="update")
        raise  # _translate_integrity 必抛异常
    if row is None:
        # WHERE 未命中：并发更正抢先，修订号已变化
        fresh = get_segment(session, segment_id)
        raise RevisionMismatchError(
            f"片段 {segment_id} 修订号已变化（并发更正）",
            current_revision=fresh["revision"] if fresh else data.expected_revision,
        )
    return dict(row), False


# ---------------------------------------------------------------------------
# 审核发布：固化输入修订与结论；同修订并发发布只留一版；历史不可变
# ---------------------------------------------------------------------------


def _input_hash(rows) -> str:
    manifest = sorted(
        ({"id": r["id"], "revision": r["revision"]} for r in rows),
        key=lambda e: e["id"],
    )
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def publish_driver(session: Session, driver_id: str) -> tuple[dict, bool]:
    """返回 (发布记录, 是否幂等命中既有版本)。"""
    # 事务级咨询锁：串行化同司机的发布，保证 version 分配与去重确定
    key_hash = hashlib.blake2b(driver_id.encode("utf-8"), digest_size=8).digest()
    key_int = int.from_bytes(key_hash, "big", signed=True)
    session.execute(text("SELECT pg_advisory_xact_lock(:k)").bindparams(k=key_int))

    rows = list_segments(session, driver_id)
    if not rows:
        raise NotFoundError(f"司机 {driver_id} 暂无任何片段，无法发布")

    input_hash = _input_hash(rows)

    existing = session.execute(
        text(
            "SELECT * FROM publications WHERE driver_id = :d AND input_hash = :h"
        ).bindparams(d=driver_id, h=input_hash)
    ).mappings().first()
    if existing is not None:
        return dict(existing), True

    conclusion = evaluate_driver(driver_id, _engine_inputs(rows)).as_dict()
    next_version = session.execute(
        text(
            "SELECT COALESCE(MAX(version), 0) + 1 AS v "
            "FROM publications WHERE driver_id = :d"
        ).bindparams(d=driver_id)
    ).scalar_one()

    manifest = [{"id": r["id"], "revision": r["revision"]} for r in rows]
    snapshot = [
        {
            "id": r["id"],
            "driver_id": r["driver_id"],
            "start": _iso(r["started_at"]),
            "end": _iso(r["ended_at"]),
            "kind": r["kind"],
            "revision": r["revision"],
        }
        for r in rows
    ]

    insert_stmt = text(
        """
        INSERT INTO publications
            (driver_id, version, input_hash, segment_manifest, segment_snapshot,
             conclusion, compliant)
        VALUES (:d, :v, :h, CAST(:manifest AS JSONB), CAST(:snapshot AS JSONB),
                CAST(:conclusion AS JSONB), :compliant)
        RETURNING id, driver_id, version, input_hash, segment_manifest,
                  conclusion, compliant, published_at
        """
    ).bindparams(
        d=driver_id,
        v=next_version,
        h=input_hash,
        manifest=json.dumps(manifest, ensure_ascii=False),
        snapshot=json.dumps(snapshot, ensure_ascii=False),
        conclusion=json.dumps(conclusion, ensure_ascii=False),
        compliant=conclusion["compliant"],
    )
    try:
        savepoint = session.begin_nested()
        row = session.execute(insert_stmt).mappings().first()
        savepoint.commit()
    except IntegrityError as exc:
        # 并发发布相同输入：唯一约束保证只留一版（保存点已自动回滚）
        if isinstance(exc.orig, UniqueViolation):
            existing = session.execute(
                text(
                    "SELECT * FROM publications WHERE driver_id = :d AND input_hash = :h"
                ).bindparams(d=driver_id, h=input_hash)
            ).mappings().first()
            if existing is not None:
                return dict(existing), True
        raise
    return dict(row), False


def list_publications(session: Session, driver_id: str) -> list[dict]:
    rows = session.execute(
        text(
            """
            SELECT id, driver_id, version, input_hash, compliant, published_at
            FROM publications WHERE driver_id = :d ORDER BY version DESC
            """
        ).bindparams(d=driver_id)
    ).mappings().all()
    return [dict(r) for r in rows]


def get_publication(session: Session, driver_id: str, version: int | None = None) -> dict:
    if version is None:
        row = session.execute(
            text(
                "SELECT * FROM publications WHERE driver_id = :d "
                "ORDER BY version DESC LIMIT 1"
            ).bindparams(d=driver_id)
        ).mappings().first()
    else:
        row = session.execute(
            text(
                "SELECT * FROM publications WHERE driver_id = :d AND version = :v"
            ).bindparams(d=driver_id, v=version)
        ).mappings().first()
    if row is None:
        what = "版本 " + str(version) if version is not None else "任何版本"
        raise NotFoundError(f"司机 {driver_id} 不存在{what}的发布记录")
    return dict(row)
