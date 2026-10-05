#!/usr/bin/env python3
"""铁路司机值勤合规核算服务 —— 端到端调用示例（仅标准库，无需 requests）。

用法：
    python3 examples/python-demo.py [base_url]
默认 http://localhost:8000
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:8000"
API = f"{BASE}/api/v1"
DRIVER = "D-2002"


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict | str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{API}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


def show(title: str, result: tuple[int, dict | str]) -> None:
    status, payload = result
    print(f"\n=== {title} -> HTTP {status} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> None:
    with urllib.request.urlopen(f"{BASE}/health") as resp:
        print("health:", resp.read().decode())

    seg = {
        "id": "seg-2002-1",
        "driver_id": DRIVER,
        "start": "2026-10-06T00:00:00Z",
        "end": "2026-10-06T05:00:00Z",
        "kind": "driving",
    }
    show("录入 5h 驾驶（单片段即超 4h，越限点 04:00）", call("POST", "/segments", seg))

    show("同 ID 同内容重试（幂等 200）", call("POST", "/segments", seg))

    conflict = {**seg, "end": "2026-10-06T06:00:00Z"}
    show("同 ID 异内容冲突（409 id_conflict）", call("POST", "/segments", conflict))

    show("非正时长拒绝（422）", call("POST", "/segments", {
        "id": "seg-bad", "driver_id": DRIVER,
        "start": "2026-10-06T10:00:00Z", "end": "2026-10-06T09:00:00Z",
        "kind": "rest",
    }))

    show("合规结果（含违规类型/首次越限时刻/参与片段）",
         call("GET", f"/drivers/{DRIVER}/compliance"))

    show("错误修订号更正（409 revision_mismatch）",
         call("PUT", "/segments/seg-2002-1", {
             "driver_id": DRIVER,
             "start": "2026-10-06T00:00:00Z", "end": "2026-10-06T04:00:00Z",
             "kind": "driving", "expected_revision": 7,
         }))

    show("正确修订号更正为恰好 4h（revision 1 -> 2，重算合规）",
         call("PUT", "/segments/seg-2002-1", {
             "driver_id": DRIVER,
             "start": "2026-10-06T00:00:00Z", "end": "2026-10-06T04:00:00Z",
             "kind": "driving", "expected_revision": 1,
         }))

    show("重算后的合规结果", call("GET", f"/drivers/{DRIVER}/compliance"))

    show("发布 v1", call("POST", "/publications", {"driver_id": DRIVER}))
    show("同修订重复发布（幂等，返回同一版本）",
         call("POST", "/publications", {"driver_id": DRIVER}))
    show("发布版本列表", call("GET", f"/drivers/{DRIVER}/publications"))

    print("\n全部示例执行完毕。")


if __name__ == "__main__":
    main()
