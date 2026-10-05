"""规则引擎单元测试：纯 Python，无数据库依赖。

运行：
    python -m pytest tests/ -q          # 有 pytest 时
    python tests/test_engine.py         # 无 pytest 时也可直接运行
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.compliance.engine import (
    QUALIFYING_REST,
    RULE_DRIVING,
    RULE_ROLLING_24H,
    ComplianceError,
    Segment,
    SegmentKind,
    evaluate_driver,
)

UTC = timezone.utc
T0 = datetime(2026, 10, 5, 0, 0, 0, tzinfo=UTC)


def seg(sid, start, end, kind, driver="D1"):
    return Segment(sid, driver, start, end, SegmentKind(kind))


def at(h, m=0):
    return T0 + timedelta(hours=h, minutes=m)


def first(rule, report):
    for v in report.violations:
        if v.rule == rule:
            return v
    return None


# --- 基础结构校验 -----------------------------------------------------------


def test_non_positive_duration_rejected():
    try:
        evaluate_driver("D1", [seg("a", at(2), at(2), "driving")])
        assert False, "应当拒绝零时长"
    except ComplianceError:
        pass
    try:
        evaluate_driver("D1", [seg("a", at(3), at(2), "driving")])
        assert False, "应当拒绝负时长"
    except ComplianceError:
        pass


def test_overlap_rejected_but_touching_ok():
    # 相接合法
    r = evaluate_driver(
        "D1",
        [seg("a", at(0), at(2), "driving"), seg("b", at(2), at(3), "other_duty")],
    )
    assert r.compliant
    try:
        evaluate_driver(
            "D1",
            [seg("a", at(0), at(2), "driving"), seg("b", at(1, 59), at(3), "rest")],
        )
        assert False, "重叠应拒绝"
    except ComplianceError:
        pass


def test_empty_driver_is_compliant():
    r = evaluate_driver("D1", [])
    assert r.compliant and r.violations == []


# --- 规则二：4 小时驾驶上限 --------------------------------------------------


def test_driving_exactly_4h_compliant():
    r = evaluate_driver("D1", [seg("a", at(0), at(4), "driving")])
    assert r.compliant


def test_driving_4h_plus_one_second_violates_at_4h():
    r = evaluate_driver("D1", [seg("a", at(0), at(4, 0) + timedelta(seconds=1), "driving")])
    v = first(RULE_DRIVING, r)
    assert v is not None
    assert v.first_exceed_at == at(4)
    assert v.participants[-1].segment_id == "a"
    assert v.participants[-1].intersects_violation_at
    assert v.cumulative_seconds_at_exceed == 4 * 3600


def test_driving_accumulates_across_other_duty_no_reset():
    r = evaluate_driver(
        "D1",
        [
            seg("a", at(0), at(3), "driving"),
            seg("b", at(3), at(5), "other_duty"),  # 不重置
            seg("c", at(5), at(6, 30), "driving"),  # 再开 1.5h -> 越限
        ],
    )
    v = first(RULE_DRIVING, r)
    assert v is not None
    assert v.first_exceed_at == at(6)  # 3h + 1h
    ids = [p.segment_id for p in v.participants]
    assert ids == ["a", "c"]


def test_29_minute_rest_does_not_reset():
    r = evaluate_driver(
        "D1",
        [
            seg("a", at(0), at(3), "driving"),
            seg("r", at(3), at(3, 29), "rest"),
            seg("c", at(3, 29), at(5), "driving"),  # +1.5h 后越限
        ],
    )
    v = first(RULE_DRIVING, r)
    assert v is not None
    assert v.first_exceed_at == at(4, 29)


def test_30_minute_rest_resets():
    r = evaluate_driver(
        "D1",
        [
            seg("a", at(0), at(3), "driving"),
            seg("r1", at(3), at(3, 15), "rest"),
            seg("r2", at(3, 15), at(3, 30), "rest"),  # 相接合并满 30 分钟
            seg("c", at(3, 30), at(7, 30), "driving"),  # 重置后开 4h 整
        ],
    )
    assert r.compliant, r.as_dict()
    # 重置后开 4 小时零 1 秒则违规
    r2 = evaluate_driver(
        "D1",
        [
            seg("a", at(0), at(3), "driving"),
            seg("r1", at(3), at(3, 15), "rest"),
            seg("r2", at(3, 15), at(3, 30), "rest"),
            seg("c", at(3, 30), at(7, 30) + timedelta(seconds=1), "driving"),
        ],
    )
    v = first(RULE_DRIVING, r2)
    assert v is not None
    assert v.first_exceed_at == at(7, 30)
    assert v.qualifying_rest_segment_ids == ["r1", "r2"]


def test_uncovered_gap_is_not_rest():
    # 30 分钟空档未被片段覆盖：不视为休息，不重置
    r = evaluate_driver(
        "D1",
        [
            seg("a", at(0), at(3), "driving"),
            seg("c", at(3, 30), at(5), "driving"),
        ],
    )
    v = first(RULE_DRIVING, r)
    assert v is not None
    assert v.first_exceed_at == at(4, 30)


def test_rest_before_first_driving_irrelevant():
    r = evaluate_driver(
        "D1",
        [
            seg("r", at(-2), at(-1), "rest"),
            seg("a", at(0), at(4), "driving"),
        ],
    )
    assert r.compliant


# --- 规则一：滚动 24 小时 10 小时上限 ----------------------------------------


def test_duty_exactly_10h_compliant():
    # 驾驶 4h + 其他值勤 6h，相接合计恰好 10h；驾驶分段恰好 4h
    r = evaluate_driver(
        "D1",
        [
            seg("a", at(0), at(4), "driving"),
            seg("b", at(4), at(10), "other_duty"),
        ],
    )
    assert r.compliant


def test_duty_10h_plus_epsilon_violates():
    r = evaluate_driver(
        "D1",
        [
            seg("a", at(0), at(6), "driving"),
            seg("b", at(6), at(10) + timedelta(seconds=1), "other_duty"),
        ],
    )
    v = first(RULE_ROLLING_24H, r)
    assert v is not None
    assert v.first_exceed_at == at(10)
    assert v.window_start_at == at(-14)
    assert v.cumulative_seconds_at_exceed == 10 * 3600


def test_rest_not_counted_in_24h():
    # 3h 驾驶 + 14h 休息 + 恰好 4h 驾驶：
    # - 若错误地把休息计入滚动窗口，t=21h 时窗内已有 3+14=17h，必越限；
    # - 正确语义：休息不计，窗内值勤最多 7h（合规）；
    # - 14h 休息是合格休息，4h 计数器重置后恰好 4h（合规）。
    r = evaluate_driver(
        "D1",
        [
            seg("a", at(0), at(3), "driving"),
            seg("r", at(3), at(17), "rest"),
            seg("b", at(17), at(21), "driving"),
        ],
    )
    assert r.compliant, r.as_dict()


def test_rest_not_counted_and_resets_both_rules():
    # 3h 驾驶 + 30 分钟合格休息 + 4h 驾驶：
    # 滚动窗口内值勤恰好 7h（休息不计），驾驶分段重置后 4h 整 -> 全合规
    r = evaluate_driver(
        "D1",
        [
            seg("a", at(0), at(3), "driving"),
            seg("r", at(3), at(3, 30), "rest"),
            seg("b", at(3, 30), at(7, 30), "driving"),
        ],
    )
    assert r.compliant, r.as_dict()


def test_rolling_window_drops_old_duty():
    # 9h 其他值勤（0~9）后相接 2h 驾驶（9~11）：
    # t=10:00 时窗内累计恰好 10h（合规）；t>10:00 严格超过 -> 越限点 10:00。
    # 同时验证 24h 滑窗语义：25 小时后旧值勤全部带出窗口。
    r = evaluate_driver(
        "D1",
        [
            seg("a", at(0), at(9), "other_duty"),
            seg("b", at(9), at(11), "driving"),
        ],
    )
    v = first(RULE_ROLLING_24H, r)
    assert v is not None
    assert v.first_exceed_at == at(10)
    ids = sorted(p.segment_id for p in v.participants)
    assert ids == ["a", "b"]

    # 把第二段推迟 25 小时：首段完全滑出滚动窗口后，仅剩 2h 值勤，滚动规则合规
    r2 = evaluate_driver(
        "D1",
        [
            seg("a", at(0), at(9), "other_duty"),
            seg("b", at(34), at(36), "driving"),
        ],
    )
    assert first(RULE_ROLLING_24H, r2) is None


def test_split_duty_spanning_window_boundary():
    # 值勤片段跨越 24h 窗口左界时，只有窗内部分计时；恰好卡 10h 合规
    r = evaluate_driver(
        "D1",
        [
            seg("a", at(0), at(2), "driving"),          # 2h，在 t=26 时滑出
            seg("b", at(20), at(28), "other_duty"),     # 8h
        ],
    )
    # t in [26,28): 窗口 (t-24,t]，a 已滑出 -> 8h；不越限
    # t in [24,26): a 部分在窗，合计恰好 2+8=10h 直至 26:00 -> 合规
    assert r.compliant, r.as_dict()


def test_single_11h_duty_violates_at_10h():
    r = evaluate_driver("D1", [seg("a", at(0), at(11), "driving")])
    v = first(RULE_ROLLING_24H, r)
    assert v is not None
    assert v.first_exceed_at == at(10)


# --- 两规则同时触发 / 重算语义 -----------------------------------------------


def test_both_rules_violated_reports_both():
    r = evaluate_driver(
        "D1",
        [
            seg("a", at(0), at(5), "driving"),       # 4h 规则先越限
            seg("b", at(5), at(11), "other_duty"),   # 凑满滚动 10h+
        ],
    )
    rules = {v.rule for v in r.violations}
    assert rules == {RULE_DRIVING, RULE_ROLLING_24H}


def test_recalculate_after_correction():
    segments = [
        seg("a", at(0), at(5), "driving"),
        seg("b", at(5), at(11), "other_duty"),
    ]
    assert not evaluate_driver("D1", segments).compliant
    # “更正”：缩短驾驶片段 -> 4h 规则解除；其余值勤合计 10h 整 -> 合规
    fixed = [
        Segment("a", "D1", at(0), at(4), SegmentKind.DRIVING, 2),
        seg("b", at(4), at(10), "other_duty"),
    ]
    assert evaluate_driver("D1", fixed).compliant


def test_timezone_offset_converted_to_utc():
    # 16:00+08:00 == 08:00Z；不同时区表达的同一时刻应一致计算
    tz8 = timezone(timedelta(hours=8))
    local_seg = Segment(
        "a",
        "D1",
        datetime(2026, 10, 5, 16, 0, tzinfo=tz8),
        datetime(2026, 10, 5, 20, 0, tzinfo=tz8),
        SegmentKind.DRIVING,
    )
    r = evaluate_driver("D1", [local_seg])
    assert r.compliant
    assert r.segment_count == 1
    # 内部已转 UTC：4h 整合规；再加 1 秒则越限点为 12:00Z
    local_over = Segment(
        "a",
        "D1",
        datetime(2026, 10, 5, 16, 0, tzinfo=tz8),
        datetime(2026, 10, 5, 20, 0, 1, tzinfo=tz8),
        SegmentKind.DRIVING,
    )
    r2 = evaluate_driver("D1", [local_over])
    v = first(RULE_DRIVING, r2)
    assert v is not None
    assert v.first_exceed_at == T0 + timedelta(hours=12)


def test_naive_datetime_rejected():
    try:
        evaluate_driver(
            "D1",
            [
                Segment(
                    "a",
                    "D1",
                    datetime(2026, 10, 5, 8, 0),
                    datetime(2026, 10, 5, 12, 0),
                    SegmentKind.DRIVING,
                )
            ],
        )
        assert False, "无时区的时间应拒绝"
    except ValueError:
        pass


if __name__ == "__main__":
    import traceback

    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in funcs:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(funcs) - failed}/{len(funcs)} passed")
    raise SystemExit(1 if failed else 0)
