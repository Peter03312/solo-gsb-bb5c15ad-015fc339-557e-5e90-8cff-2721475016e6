# 铁路司机值勤合规核算服务

基于 **Python 3.11 / FastAPI / PostgreSQL** 的值勤片段合规核算 HTTP 服务。
录入司机值勤片段（驾驶 / 其他值勤 / 休息），按规则实时核算、支持修订更正、
审核发布固化与历史版本对照。

## 合规规则

| 规则 | 上限 | 计入类型 | 重置/窗口语义 |
|---|---|---|---|
| `rolling_24h_duty_10h` | 任意**滚动 24 小时**窗口内 ≤ **10 小时** | 驾驶 + 其他值勤 | 休息不计；窗口连续滑动 |
| `driving_4h` | 每个驾驶运行段内累计驾驶 ≤ **4 小时** | 仅驾驶 | 只有**连续休息满 30 分钟**才重置；其他值勤不重置，未被片段覆盖的空档**不**视为休息 |

- **恰好等于上限合规，严格大于才违规。**
- 违规结果包含：违规类型（`rule`/`title`）、**首次越限时刻** `first_exceed_at`
  （累计值第一次严格超过上限的瞬间）、当时窗口、累计秒数，以及全部**参与片段**
  （各片段对越限的贡献秒数、是否覆盖越限时刻；4h 规则另附合格休息片段 ID）。
- 补录或更正后，查询一律按该司机**现存全部片段重算**。

### 片段约束

- 同司机片段**不得重叠**（数据库 GiST 排除约束强保证）；端点**相接合法**
  （上一个 `end` == 下一个 `start`）。
- 时长必须为正（`end > start`），否则拒绝（422）。
- 片段 ID 全局唯一。

## 快速启动（Docker Compose）

```bash
docker compose up --build
```

启动后：

| 地址 | 说明 |
|---|---|
| http://localhost:8000 | 服务根路径 |
| http://localhost:8000/docs | Swagger UI（可直接在线调用） |
| http://localhost:8000/health | 健康检查 |
| http://localhost:5432 | PostgreSQL（库 `railway_duty`） |

PostgreSQL 首次启动自动执行 `db/init.sql` 完成建表与约束初始化；
应用启动时也会幂等执行同一脚本并等待数据库就绪。

停止与清理：

```bash
docker compose down          # 停止，保留数据
docker compose down -v       # 停止并删除数据库卷
```

### 本地不使用 Docker 运行

```bash
pip install -r requirements.txt
# 自行准备 PostgreSQL 14+，先执行 db/init.sql，并开启 btree_gist 扩展
export DATABASE_URL="postgresql+psycopg2://USER:PASSWORD@localhost:5432/DBNAME"
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg2://railway:railway@db:5432/railway_duty` | SQLAlchemy/psycopg2 连接串 |
| `HTTP_HOST` | `0.0.0.0` | 监听地址 |
| `HTTP_PORT` | `8000` | 监听端口 |
| `SCHEMA_PATH` | `/app/db/init.sql` | 幂等初始化 DDL 路径 |

数据库侧（`docker-compose.yml`）：`POSTGRES_USER/POSTGRES_PASSWORD/POSTGRES_DB`，
默认均为 `railway` / `railway` / `railway_duty`。

所有时间使用带时区的 ISO-8601，**建议统一传 UTC（以 `Z` 结尾）**；服务端统一转 UTC
存储与计算，响应也为 UTC。

## HTTP API

基础前缀：`/api/v1`。错误响应统一为 `{"error": {"code": ..., "detail": ...}}`。

### 片段

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/segments` | 录入片段 |
| PUT | `/segments/{id}` | 按预期修订号更正 |
| GET | `/segments/{id}` | 查询单个片段 |
| GET | `/drivers/{driver_id}/segments` | 列出司机全部片段（按开始时间） |

录入载荷：

```json
{
  "id": "seg-1001-1",
  "driver_id": "D-1001",
  "start": "2026-10-05T08:00:00Z",
  "end": "2026-10-05T11:00:00Z",
  "kind": "driving"
}
```

`kind` 取值：`driving`（驾驶）、`other_duty`（其他值勤）、`rest`（休息）。

- **幂等**：相同 `id` + 相同内容（司机、UTC 起止、类型）重复提交返回 **200** 与
   既有片段，不新增；相同 `id` + 不同内容返回 **409 `id_conflict`**。
- **重叠**：返回 **409 `segment_overlap`**；非正时长/无时区时间返回 **422**。

更正载荷（按片段 ID + **预期修订号**乐观并发控制）：

```json
{
  "driver_id": "D-1001",
  "start": "2026-10-05T14:00:00Z",
  "end": "2026-10-05T14:30:00Z",
  "kind": "rest",
  "expected_revision": 1
}
```

- 现存修订号与 `expected_revision` 不符 → **409 `revision_mismatch`**，片段不变。
- 更正成功修订号 +1；同内容 + 正确修订号重试幂等（200，无变更）。

### 合规核算

```
GET /api/v1/drivers/{driver_id}/compliance
```

按现存片段实时重算，返回 `compliant` 与 `violations[]`（首次越限时刻 + 参与片段）。

### 审核发布与历史版本

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/publications` | 固化当前输入修订清单与结论，生成新版本 |
| GET | `/drivers/{driver_id}/publications` | 发布版本列表（新→旧） |
| GET | `/drivers/{driver_id}/publications/{version}` | 取指定版本（含片段快照与结论） |
| GET | `/drivers/{driver_id}/compliance/diff?base_version=N` | **当前实时结果** vs 历史版本 N |
| GET | `/drivers/{driver_id}/publications/{version}/diff?base_version=M` | 两个历史版本互比（M 缺省为 version-1） |

发布语义：

- 发布记录包含输入修订清单（每个片段 ID 的修订号）、**完整片段快照**与合规结论，
  一经写入**不可变**；之后更正片段只影响当前结果与后续新版本，**不改旧版**。
- **相同输入修订**重复或并发发布：每司机串行（事务咨询锁）+
  `(driver_id, input_hash)` 唯一约束，保证**只留一版**；重复请求幂等返回同一版本
  （响应中 `idempotent: true`，HTTP 200）。
- 版本号每司机单调递增（1, 2, 3 …）。

对照结果字段：`compliant_before/after`、`segment_revisions_changed`、
`violation_diffs[]`（每条规则标 `new` / `resolved` / `unchanged` / `changed`，
并附前后内容；`target_version` 为 `null` 时表示“当前实时结果”）。

## 调用示例

```bash
# 完整端到端流程（录入/幂等/冲突/重叠/更正/重算/发布/对照）
bash examples/curl-demo.sh

# 仅用 Python 标准库的等价示例
python3 examples/python-demo.py
```

也可直接打开 http://localhost:8000/docs 使用 Swagger UI。

## 项目结构

```
app/
  main.py            FastAPI 入口、异常处理、启动等待与建表
  config.py          环境变量配置
  db.py              引擎/会话、等待数据库、幂等 DDL
  schemas.py         请求/响应模型（时区校验、非正时长拒绝）
  storage.py         录入/更正/发布/查询（幂等哈希、乐观修订、咨询锁、快照固化）
  routers.py         HTTP 路由
  compliance/
    engine.py        纯函数规则引擎（滚动24h扫描线 + 驾驶运行段累计）
    diff.py          版本对照
db/init.sql          建表、GiST 排除约束（禁重叠）、唯一索引
examples/            curl 与 Python 调用示例
tests/test_engine.py 规则引擎单元测试（19 个场景，无数据库依赖）
Dockerfile / docker-compose.yml
```

## 测试

规则引擎为无依赖纯函数，可直接运行：

```bash
PYTHONPATH=. python3 tests/test_engine.py     # 无 pytest 也可
# 或
PYTHONPATH=. python3 -m pytest tests/ -q
```

覆盖：非正时长、重叠拒绝/相接合法、恰好等于上限合规、4h 累计与 30 分钟连续休息
重置（相接休息合并、29 分钟不重置、空档不视为休息）、滚动 24h 窗口扫描
（休息不计、旧片段滑出窗）、两规则同时违规、更正后重算等。
