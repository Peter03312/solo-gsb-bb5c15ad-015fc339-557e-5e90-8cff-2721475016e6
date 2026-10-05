"""历史版本与当前结果（或两个历史版本）的对照。

输出统一为“基准（较旧） -> 目标（较新）”方向：
- 当前结果 vs 历史发布：base=历史版本，target=当前实时结果（target_version=null）；
- 两个历史版本：base=base_version，target=target_version。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ConclusionView:
    version: int | None
    conclusion: dict
    manifest: list[dict[str, Any]]


def _violation_map(conclusion: dict) -> dict[str, dict]:
    return {v["rule"]: v for v in conclusion.get("violations", [])}


def diff_views(driver_id: str, base: ConclusionView, target: ConclusionView) -> dict:
    base_v = _violation_map(base.conclusion)
    target_v = _violation_map(target.conclusion)

    violation_diffs: list[dict] = []
    for rule in sorted(set(base_v) | set(target_v)):
        old = base_v.get(rule)
        new = target_v.get(rule)
        if old is None and new is not None:
            status = "new"
        elif old is not None and new is None:
            status = "resolved"
        elif old == new:
            status = "unchanged"
        else:
            status = "changed"
        violation_diffs.append(
            {"rule": rule, "status": status, "before": old, "after": new}
        )

    revisions_changed = sorted(base.manifest, key=lambda e: e["id"]) != sorted(
        target.manifest, key=lambda e: e["id"]
    )

    return {
        "driver_id": driver_id,
        "base_version": base.version,
        "target_version": target.version,
        "compliant_before": base.conclusion["compliant"],
        "compliant_after": target.conclusion["compliant"],
        "segment_revisions_changed": revisions_changed,
        "violation_diffs": violation_diffs,
    }
