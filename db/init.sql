-- 铁路司机值勤合规核算服务 —— PostgreSQL 初始化脚本
-- 由 postgres 官方镜像在首次启动时自动执行（docker-entrypoint-initdb.d），
-- 应用启动时也会幂等执行一次，保证两条路径都能完成初始化。
-- 全部语句均为幂等语法，可重复执行。

CREATE EXTENSION IF NOT EXISTS btree_gist;

-- 值勤片段
CREATE TABLE IF NOT EXISTS segments (
    id           TEXT PRIMARY KEY,
    driver_id    TEXT NOT NULL,
    started_at   TIMESTAMPTZ NOT NULL,
    ended_at     TIMESTAMPTZ NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('driving', 'other_duty', 'rest')),
    revision     INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    content_hash TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 非正时长拒绝
    CONSTRAINT segments_positive_duration CHECK (ended_at > started_at)
);

-- 同司机片段不得重叠；相接（a.ended_at = b.started_at）合法。
-- PG 的 ADD CONSTRAINT 不支持 IF NOT EXISTS，用异常捕获做幂等。
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'segments_driver_no_overlap'
    ) THEN
        ALTER TABLE segments
            ADD CONSTRAINT segments_driver_no_overlap
            EXCLUDE USING gist (
                driver_id WITH =,
                tstzrange(started_at, ended_at, '[)') WITH &&
            );
    END IF;
END$$;

CREATE INDEX IF NOT EXISTS idx_segments_driver_time
    ON segments (driver_id, started_at);

-- 审核发布快照。每次发布固化当时的输入修订与结论；同修订并发发布只留一版。
CREATE TABLE IF NOT EXISTS publications (
    id              BIGSERIAL PRIMARY KEY,
    driver_id       TEXT NOT NULL,
    version         INTEGER NOT NULL,
    input_hash      TEXT NOT NULL,
    segment_manifest JSONB NOT NULL,
    segment_snapshot JSONB NOT NULL,
    conclusion      JSONB NOT NULL,
    compliant       BOOLEAN NOT NULL,
    published_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 同司机、同一输入版本（修订清单）只允许一版：并发发布第二个事务命中唯一约束
CREATE UNIQUE INDEX IF NOT EXISTS uq_publications_driver_input
    ON publications (driver_id, input_hash);
CREATE UNIQUE INDEX IF NOT EXISTS uq_publications_driver_version
    ON publications (driver_id, version);
