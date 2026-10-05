#!/usr/bin/env bash
# 铁路司机值勤合规核算服务 —— 端到端调用示例（curl）
# 用法：bash examples/curl-demo.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
API="$BASE/api/v1"
DRIVER="D-1001"

j() { python3 -m json.tool 2>/dev/null || cat; }

echo "== 1) 健康检查 =="
curl -sS "$BASE/health"; echo

echo "== 2) 录入驾驶片段 08:00-11:00（3h）=="
curl -sS -X POST "$API/segments" -H 'Content-Type: application/json' -d "{
  \"id\": \"seg-1001-1\", \"driver_id\": \"$DRIVER\",
  \"start\": \"2026-10-05T08:00:00Z\", \"end\": \"2026-10-05T11:00:00Z\",
  \"kind\": \"driving\"
}" | j; echo

echo "== 3) 同 ID 同内容重试（幂等，HTTP 200，不新建）=="
curl -sS -o /dev/null -w "HTTP %{http_code}\n" -X POST "$API/segments" \
  -H 'Content-Type: application/json' -d "{
  \"id\": \"seg-1001-1\", \"driver_id\": \"$DRIVER\",
  \"start\": \"2026-10-05T08:00:00Z\", \"end\": \"2026-10-05T11:00:00Z\",
  \"kind\": \"driving\"
}"

echo "== 4) 同 ID 异内容（冲突 409）=="
curl -sS -X POST "$API/segments" -H 'Content-Type: application/json' -d "{
  \"id\": \"seg-1001-1\", \"driver_id\": \"$DRIVER\",
  \"start\": \"2026-10-05T08:00:00Z\", \"end\": \"2026-10-05T12:00:00Z\",
  \"kind\": \"driving\"
}"; echo

echo "== 5) 录入相接的其他值勤 11:00-14:00（3h，相接合法）=="
curl -sS -o /dev/null -w "HTTP %{http_code}\n" -X POST "$API/segments" \
  -H 'Content-Type: application/json' -d "{
  \"id\": \"seg-1001-2\", \"driver_id\": \"$DRIVER\",
  \"start\": \"2026-10-05T11:00:00Z\", \"end\": \"2026-10-05T14:00:00Z\",
  \"kind\": \"other_duty\"
}"

echo "== 6) 录入重叠片段（重叠拒绝 409）=="
curl -sS -X POST "$API/segments" -H 'Content-Type: application/json' -d "{
  \"id\": \"seg-1001-overlap\", \"driver_id\": \"$DRIVER\",
  \"start\": \"2026-10-05T13:30:00Z\", \"end\": \"2026-10-05T15:00:00Z\",
  \"kind\": \"rest\"
}"; echo

echo "== 7) 非正时长拒绝（422）=="
curl -sS -o /dev/null -w "HTTP %{http_code}\n" -X POST "$API/segments" \
  -H 'Content-Type: application/json' -d "{
  \"id\": \"seg-bad\", \"driver_id\": \"$DRIVER\",
  \"start\": \"2026-10-05T15:00:00Z\", \"end\": \"2026-10-05T15:00:00Z\",
  \"kind\": \"rest\"
}"

echo "== 8) 再录驾驶 14:00-15:30（与首段驾驶累计 4.5h -> 触发 4h 规则；"
echo "      值勤合计 6.5h 未超 10h；首次越限时刻 15:00）=="
curl -sS -o /dev/null -w "HTTP %{http_code}\n" -X POST "$API/segments" \
  -H 'Content-Type: application/json' -d "{
  \"id\": \"seg-1001-3\", \"driver_id\": \"$DRIVER\",
  \"start\": \"2026-10-05T14:00:00Z\", \"end\": \"2026-10-05T15:30:00Z\",
  \"kind\": \"driving\"
}"

echo "== 9) 查询合规结果（违规类型 / 首次越限时刻 / 参与片段）=="
curl -sS "$API/drivers/$DRIVER/compliance" | j; echo

echo "== 10) 更正：错误修订号拒绝（409 revision_mismatch），正确修订号改为休息 =="
curl -sS -o /dev/null -w "expected_revision=99 -> HTTP %{http_code}\n" \
  -X PUT "$API/segments/seg-1001-3" -H 'Content-Type: application/json' -d "{
  \"driver_id\": \"$DRIVER\",
  \"start\": \"2026-10-05T14:00:00Z\", \"end\": \"2026-10-05T15:30:00Z\",
  \"kind\": \"rest\", \"expected_revision\": 99
}"
curl -sS -X PUT "$API/segments/seg-1001-3" -H 'Content-Type: application/json' -d "{
  \"driver_id\": \"$DRIVER\",
  \"start\": \"2026-10-05T14:00:00Z\", \"end\": \"2026-10-05T14:30:00Z\",
  \"kind\": \"rest\", \"expected_revision\": 1
}" | j; echo

echo "== 11) 更正后按现存记录重算：30 分钟休息合格，合规 =="
curl -sS "$API/drivers/$DRIVER/compliance" | j; echo

echo "== 12) 审核发布 v1（固化输入修订与结论），重复发布同修订幂等 =="
curl -sS -X POST "$API/publications" -H 'Content-Type: application/json' \
  -d "{\"driver_id\": \"$DRIVER\"}" | j; echo
curl -sS -o /dev/null -w "republish same input -> HTTP %{http_code}\n" \
  -X POST "$API/publications" -H 'Content-Type: application/json' \
  -d "{\"driver_id\": \"$DRIVER\"}"

echo "== 13) 更正片段后发布 v2；再对照当前结果与历史 v1 =="
curl -sS -o /dev/null -X PUT "$API/segments/seg-1001-2" \
  -H 'Content-Type: application/json' -d "{
  \"driver_id\": \"$DRIVER\",
  \"start\": \"2026-10-05T11:00:00Z\", \"end\": \"2026-10-05T13:00:00Z\",
  \"kind\": \"other_duty\", \"expected_revision\": 1
}"
curl -sS -o /dev/null -X POST "$API/publications" \
  -H 'Content-Type: application/json' -d "{\"driver_id\": \"$DRIVER\"}"
echo "-- 当前 vs v1 --"
curl -sS "$API/drivers/$DRIVER/compliance/diff?base_version=1" | j; echo
echo "-- v2 vs v1（历史互比） --"
curl -sS "$API/drivers/$DRIVER/publications/2/diff?base_version=1" | j; echo

echo "演示完成。交互式文档：$BASE/docs"
