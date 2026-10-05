"""铁路司机值勤合规核算引擎（纯函数实现，无数据库依赖）。

规则：
1. 24 小时滚动上限：驾驶 (driving) 与其他值勤 (other_duty) 之和在任意滚动
   24 小时窗口内不得超过 10 小时；休息 (rest) 不计入。
2. 驾驶分段上限：自首个驾驶片段起、以及每次合格休息（连续休息满 30 分钟）后，
   累计驾驶不得超过 4 小时；其他值勤不重置，未被任何片段覆盖的空档不视为休息。

边界语义：恰好等于上限合规，严格大于才违规。报告给出“首次越限时刻”——即累计
值第一次严格超过上限的时刻（等于上限的瞬间仍合规）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Iterable, Mapping, Sequence

UTC = timezone.utc

DRIVING_LIMIT = timedelta(hours=4)
DUTY_LIMIT_24H = timedelta(hours=10)
QUALIFYING_REST = timedelta(minutes=30)
ROLLING_WINDOW = timedelta(hours=24)

RULE_DRIVING = "driving_4h"
RULE_ROLLING_24H = "rolling_24h_duty_10h"

# 浮点容差：累计值恰好等于上限时视为合规（误差 1 微秒以内）
EPSILON_SECONDS = 1e-6


class SegmentKind(str, Enum):
    DRIVING = "driving"
    OTHER_DUTY = "other_duty"
    REST = "rest"


DUTY_KINDS = frozenset({SegmentKind.DRIVING.value, SegmentKind.OTHER_DUTY.value})


class ComplianceError(ValueError):
    """输入片段本身不合法（重叠、非正时长、归属不一致等）。"""


@dataclass(frozen=True)
class Segment:
    id: str
    driver_id: str
    start: datetime
    end: datetime
    kind: SegmentKind
    revision: int = 1

    @property
    def duration(self) -> timedelta:
        return self.end - self.start


@dataclass
class Participant:
    segment_id: str
    started_at: datetime
    ended_at: datetime
    kind: str
    contribution_seconds: float
    intersects_violation_at: bool

    def as_dict(self) -> dict:
        return {
            "segment_id": self.segment_id,
            "started_at": _iso(self.started_at),
            "ended_at": _iso(self.ended_at),
            "kind": self.kind,
            "contribution_seconds": self.contribution_seconds,
            "intersects_violation_at": self.intersects_violation_at,
        }


@dataclass
class Violation:
    rule: str
    title: str
    first_exceed_at: datetime
    limit_seconds: float
    window_start_at: datetime
    window_end_at: datetime
    cumulative_seconds_at_exceed: float
    participants: list[Participant] = field(default_factory=list)
    qualifying_rest_segment_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "rule": self.rule,
            "title": self.title,
            "first_exceed_at": _iso(self.first_exceed_at),
            "limit_seconds": self.limit_seconds,
            "window_start_at": _iso(self.window_start_at),
            "window_end_at": _iso(self.window_end_at),
            "cumulative_seconds_at_exceed": self.cumulative_seconds_at_exceed,
            "participants": [p.as_dict() for p in self.participants],
            "qualifying_rest_segment_ids": list(self.qualifying_rest_segment_ids),
        }


@dataclass
class ComplianceReport:
    driver_id: str
    computed_at: datetime
    segment_count: int
    compliant: bool
    violations: list[Violation]

    def as_dict(self) -> dict:
        return {
            "driver_id": self.driver_id,
            "computed_at": _iso(self.computed_at),
            "segment_count": self.segment_count,
            "compliant": self.compliant,
            "violations": [v.as_dict() for v in self.violations],
        }


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("datetime 必须携带时区信息（UTC）")
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("datetime 必须携带时区信息（UTC）")
    return dt.astimezone(UTC)


def _coerce_segment(raw) -> Segment:
    if isinstance(raw, Segment):
        seg = raw
    elif isinstance(raw, Mapping):
        seg = Segment(
            id=raw["id"],
            driver_id=raw["driver_id"],
            start=raw["start"],
            end=raw["end"],
            kind=SegmentKind(raw["kind"]),
            revision=int(raw.get("revision", 1)),
        )
    else:
        raise ComplianceError(f"无法识别的片段类型: {type(raw)!r}")
    if seg.start >= seg.end:
        raise ComplianceError(f"片段 {seg.id} 时长必须为正（start < end）")
    if not isinstance(seg.kind, SegmentKind):
        seg = Segment(
            seg.id, seg.driver_id, seg.start, seg.end, SegmentKind(seg.kind), seg.revision
        )
    return Segment(
        seg.id, seg.driver_id, _to_utc(seg.start), _to_utc(seg.end), seg.kind, seg.revision
    )


def _validate_no_overlap(segments: Sequence[Segment]) -> None:
    """同司机片段不得重叠；端点相接（前一片段 end == 后一片段 start）合法。"""
    ordered = sorted(segments, key=lambda s: (s.start, s.end))
    for prev, cur in zip(ordered, ordered[1:]):
        if cur.start < prev.end:
            raise ComplianceError(
                f"片段重叠：{prev.id} ({_iso(prev.start)}~{_iso(prev.end)}) 与 "
                f"{cur.id} ({_iso(cur.start)}~{_iso(cur.end)})"
            )


def evaluate_driver(
    driver_id: str, raw_segments: Iterable, *, computed_at: datetime | None = None
) -> ComplianceReport:
    """对单个司机的现存全部片段重算合规结论（补录或更正后同样调用本函数）。"""
    segments = sorted(
        (_coerce_segment(s) for s in raw_segments), key=lambda s: (s.start, s.end)
    )
    for seg in segments:
        if seg.driver_id != driver_id:
            raise ComplianceError(f"片段 {seg.id} 不属于司机 {driver_id}")
    _validate_no_overlap(segments)

    violations: list[Violation] = []
    v1 = _check_rolling_24h(driver_id, segments)
    if v1:
        violations.append(v1)
    v2 = _check_driving_run(driver_id, segments)
    if v2:
        violations.append(v2)

    return ComplianceReport(
        driver_id=driver_id,
        computed_at=_to_utc(computed_at or datetime.now(UTC)),
        segment_count=len(segments),
        compliant=not violations,
        violations=violations,
    )


# ---------------------------------------------------------------------------
# 规则一：任意滚动 24 小时窗口内驾驶 + 其他值勤 ≤ 10 小时
# ---------------------------------------------------------------------------

def _check_rolling_24h(driver_id: str, segments: Sequence[Segment]) -> Violation | None:
    duty = [s for s in segments if s.kind.value in DUTY_KINDS]
    if not duty:
        return None

    # f(t) = 窗口 (t-24h, t] 内所有值勤片段与窗口交集的时长之和（片段互不重叠，
    # 交集之和即覆盖时长）。f 连续、分段线性，斜率在以下时刻变化：
    #   t = s.start         +1（片段进入窗口右端）
    #   t = s.end           -1（片段完全越过右端）
    #   t = s.start + 24h   -1（片段开始滑出窗口左端）
    #   t = s.end + 24h     +1（片段整体滑出窗口左端）
    events: list[tuple[datetime, int]] = []
    for s in duty:
        events.append((s.start, +1))
        events.append((s.end, -1))
        events.append((s.start + ROLLING_WINDOW, -1))
        events.append((s.end + ROLLING_WINDOW, +1))
    events.sort(key=lambda e: (e[0], e[1]))

    limit_s = DUTY_LIMIT_24H.total_seconds()

    def duty_seconds_in_window(right: datetime) -> float:
        left = right - ROLLING_WINDOW
        total = 0.0
        for s in duty:
            lo = s.start if s.start > left else left
            hi = s.end if s.end < right else right
            if hi > lo:
                total += (hi - lo).total_seconds()
        return total

    t = duty[0].start
    accumulated = 0.0  # duty_seconds_in_window(t)，首个值勤开始时为 0
    slope = 0
    for tm, dslope in events:
        if tm <= t:
            # t 之前（含同时刻）的事件直接并入；首个事件即 duty[0].start(+1)
            slope += dslope
            continue
        if accumulated < limit_s - EPSILON_SECONDS and slope > 0:
            hit_at = t + timedelta(seconds=(limit_s - accumulated) / slope)
            # 严格早于下一断点才算在区间内部越限；hit_at == tm 时在断点处
            # 用精确值重判（恰好等于上限合规）
            if hit_at < tm:
                return _build_rolling_violation(hit_at, limit_s, duty)
        # 到达断点：不用斜率外推（浮点漂移会双向出现），直接精确重算累计值
        t = tm
        slope += dslope
        accumulated = duty_seconds_in_window(t)
        if accumulated > limit_s + EPSILON_SECONDS:
            return _build_rolling_violation(t, limit_s, duty)
    return None


def _build_rolling_violation(hit_at: datetime, limit_s: float, duty) -> Violation:
    window_start = hit_at - ROLLING_WINDOW
    participants: list[Participant] = []
    for s in duty:
        lo = s.start if s.start > window_start else window_start
        hi = s.end if s.end < hit_at else hit_at
        contrib = (hi - lo).total_seconds() if hi > lo else 0.0
        if contrib > 0 or (s.start <= hit_at <= s.end):
            participants.append(
                Participant(
                    segment_id=s.id,
                    started_at=s.start,
                    ended_at=s.end,
                    kind=s.kind.value,
                    contribution_seconds=round(contrib, 6),
                    intersects_violation_at=s.start <= hit_at <= s.end,
                )
            )
    cumulative = sum(p.contribution_seconds for p in participants)
    return Violation(
        rule=RULE_ROLLING_24H,
        title="任意滚动24小时内驾驶与其他值勤合计超过10小时",
        first_exceed_at=hit_at,
        limit_seconds=limit_s,
        window_start_at=window_start,
        window_end_at=hit_at,
        cumulative_seconds_at_exceed=round(cumulative, 6),
        participants=participants,
    )


# ---------------------------------------------------------------------------
# 规则二：每个驾驶运行段内累计驾驶 ≤ 4 小时；满 30 分钟连续休息才重置
# ---------------------------------------------------------------------------

def _check_driving_run(driver_id: str, segments: Sequence[Segment]) -> Violation | None:
    if not any(s.kind == SegmentKind.DRIVING for s in segments):
        return None

    limit_s = DRIVING_LIMIT.total_seconds()
    accumulated = 0.0
    run_start: datetime | None = None
    run_segments: list[Segment] = []
    last_qualifying_rest: list[str] = []

    for idx, seg in enumerate(segments):
        if seg.kind == SegmentKind.REST:
            merged_ids, merged_len = _merge_rest_from(segments, idx)
            if merged_len >= QUALIFYING_REST:
                # 合格休息：重置驾驶运行段
                accumulated = 0.0
                run_start = None
                run_segments = []
                last_qualifying_rest = merged_ids
            continue

        if seg.kind == SegmentKind.OTHER_DUTY:
            # 其他值勤不累计驾驶也不重置；未覆盖空档同样不视为休息，状态保持
            continue

        # driving
        if run_start is None:
            run_start = seg.start
        seg_len = seg.duration.total_seconds()
        if accumulated < limit_s - EPSILON_SECONDS and accumulated + seg_len > limit_s + EPSILON_SECONDS:
            hit_at = seg.start + timedelta(seconds=(limit_s - accumulated))
            return _build_driving_violation(
                run_start, run_segments, seg, hit_at, limit_s, last_qualifying_rest
            )
        accumulated += seg_len
        run_segments.append(seg)

    return None


def _merge_rest_from(
    segments: Sequence[Segment], start_index: int
) -> tuple[list[str], timedelta]:
    """从 start_index 起合并所有首尾相接的连续休息片段。

    相接合法：rest.end == 下一个 rest.start 视为连续。中间隔着任何非休息片段
    （或未覆盖的空档）都中断连续性。返回 (片段ID列表, 合并总时长)。
    """
    first = segments[start_index]
    ids = [first.id]
    total = first.duration
    cur_end = first.end
    for nxt in segments[start_index + 1:]:
        if nxt.kind != SegmentKind.REST or nxt.start != cur_end:
            break
        ids.append(nxt.id)
        total += nxt.duration
        cur_end = nxt.end
    return ids, total


def _build_driving_violation(
    run_start, run_segments, current, hit_at, limit_s, rest_ids
) -> Violation:
    participants: list[Participant] = []
    cumulative = 0.0
    for s in run_segments:
        cumulative += s.duration.total_seconds()
        participants.append(
            Participant(
                segment_id=s.id,
                started_at=s.start,
                ended_at=s.end,
                kind=s.kind.value,
                contribution_seconds=s.duration.total_seconds(),
                intersects_violation_at=False,
            )
        )
    contrib = (hit_at - current.start).total_seconds()
    cumulative += contrib
    participants.append(
        Participant(
            segment_id=current.id,
            started_at=current.start,
            ended_at=current.end,
            kind=current.kind.value,
            contribution_seconds=round(contrib, 6),
            intersects_violation_at=True,
        )
    )
    return Violation(
        rule=RULE_DRIVING,
        title="驾驶运行段内累计驾驶超过4小时（需连续休息满30分钟重置）",
        first_exceed_at=hit_at,
        limit_seconds=limit_s,
        window_start_at=run_start,
        window_end_at=current.end,
        cumulative_seconds_at_exceed=round(cumulative, 6),
        participants=participants,
        qualifying_rest_segment_ids=list(rest_ids),
    )
