-- =============================================================
-- Sementica 운영 로그 스키마
-- 적용: psql $POSTGRES_URL -f schema/ops_log.sql
-- =============================================================

-- MCP 도구 호출 로그
CREATE TABLE IF NOT EXISTS mcp_request_log (
    id           BIGSERIAL    PRIMARY KEY,
    ts           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    dept         VARCHAR(50),                        -- 본부 (strategic 등)
    tool         VARCHAR(50),                        -- semantic_search / graph_search / hybrid_search
    query        TEXT,                               -- 사용자 입력 쿼리
    result_count INT,                               -- 반환 결과 수
    duration_ms  INT,                               -- 응답 시간 (밀리초)
    error        TEXT                               -- 오류 메시지 (정상이면 NULL)
);

CREATE INDEX IF NOT EXISTS idx_mcp_log_ts   ON mcp_request_log (ts DESC);
CREATE INDEX IF NOT EXISTS idx_mcp_log_dept ON mcp_request_log (dept, ts DESC);
CREATE INDEX IF NOT EXISTS idx_mcp_log_tool ON mcp_request_log (tool, ts DESC);

-- 동기화(cron) 작업 로그
CREATE TABLE IF NOT EXISTS sync_log (
    id             BIGSERIAL    PRIMARY KEY,
    ts             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    dept           VARCHAR(50),                     -- 본부
    search_keyword VARCHAR(200),                    -- --search 옵션 값 (없으면 NULL)
    since_time     TIMESTAMPTZ,                     -- 기준 시각 (last_sync_time)
    modified_found INT,                             -- Notion에서 발견된 수정 페이지 수
    processed      INT,                             -- 실제 처리된 페이지 수
    skipped        INT,                             -- 텍스트 부족 등으로 건너뜀
    errors         INT,                             -- 오류 발생 수
    new_chunks     INT,                             -- 신규 생성 벡터 청크 수
    new_triplets   INT,                             -- 신규 생성 그래프 트리플 수
    duration_sec   INT,                             -- 총 소요 시간 (초)
    status         VARCHAR(20),                     -- success / partial / failed / dry_run
    error_detail   TEXT                             -- 대표 오류 메시지
);

CREATE INDEX IF NOT EXISTS idx_sync_log_ts   ON sync_log (ts DESC);
CREATE INDEX IF NOT EXISTS idx_sync_log_dept ON sync_log (dept, ts DESC);
