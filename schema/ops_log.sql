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

-- Notion 페이지 레지스트리 — 인제스천된 페이지 목록 및 상태
CREATE TABLE IF NOT EXISTS notion_pages (
    id               BIGSERIAL    PRIMARY KEY,
    page_id          VARCHAR(32)  NOT NULL,           -- Notion UUID (32자, 대시 제거)
    dept             VARCHAR(50)  NOT NULL,            -- 본부 (strategic / dev 등)
    notion_url       TEXT         NOT NULL DEFAULT '', -- Notion 원본 URL
    title            TEXT,                            -- 페이지 제목
    last_edited_time TIMESTAMPTZ,                     -- Notion 마지막 수정 시각
    last_ingested_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),  -- 마지막 인제스천 시각
    word_count       INT          DEFAULT 0,          -- 본문 단어 수
    chunk_count      INT          DEFAULT 0,          -- Qdrant 저장 청크 수
    triplet_count    INT          DEFAULT 0,          -- FalkorDB 트리플 수
    event_count      INT          DEFAULT 0,          -- FalkorDB :Event 노드 수
    is_db_item       BOOLEAN      DEFAULT FALSE,      -- Notion DB 항목 여부
    status           VARCHAR(20)  DEFAULT 'ok',       -- ok / skipped / error
    error_msg        TEXT,                            -- 오류 메시지 (정상이면 NULL)
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_notion_pages_page_dept UNIQUE (page_id, dept)
);

-- 페이지 조회 인덱스
CREATE INDEX IF NOT EXISTS idx_np_dept          ON notion_pages (dept, last_ingested_at DESC);
CREATE INDEX IF NOT EXISTS idx_np_last_edited   ON notion_pages (last_edited_time DESC);
CREATE INDEX IF NOT EXISTS idx_np_status        ON notion_pages (dept, status);

-- updated_at 자동 갱신 트리거
CREATE OR REPLACE FUNCTION _set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END; $$;

DROP TRIGGER IF EXISTS trg_notion_pages_updated_at ON notion_pages;
CREATE TRIGGER trg_notion_pages_updated_at
    BEFORE UPDATE ON notion_pages
    FOR EACH ROW EXECUTE FUNCTION _set_updated_at();

-- 검색 품질 골든셋 — 테스트 케이스 저장
CREATE TABLE IF NOT EXISTS search_golden_set (
    id           BIGSERIAL    PRIMARY KEY,
    dept         VARCHAR(50)  NOT NULL,
    query        TEXT         NOT NULL,          -- 테스트 쿼리
    expected     TEXT[]       NOT NULL,          -- 기대 문서 제목 목록 (하나라도 top-k 내 있으면 Pass)
    top_k        INT          NOT NULL DEFAULT 5,
    notes        TEXT,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_golden_dept ON search_golden_set (dept, id DESC);

-- 골든셋 실행 이력
CREATE TABLE IF NOT EXISTS golden_run_log (
    id           BIGSERIAL    PRIMARY KEY,
    dept         VARCHAR(50)  NOT NULL,
    total        INT,
    passed       INT,
    failed       INT,
    avg_score    NUMERIC(5,4),
    detail       JSONB,                          -- [{golden_id, query, passed, score, matched, results}]
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_golden_run_dept ON golden_run_log (dept, created_at DESC);

-- 유용한 뷰: 본부별 인제스천 현황
CREATE OR REPLACE VIEW v_ingest_summary AS
SELECT
    dept,
    COUNT(*)                                    AS total_pages,
    COUNT(*) FILTER (WHERE status = 'ok')       AS ok_pages,
    COUNT(*) FILTER (WHERE status = 'skipped')  AS skipped_pages,
    COUNT(*) FILTER (WHERE status = 'error')    AS error_pages,
    SUM(chunk_count)                            AS total_chunks,
    SUM(triplet_count)                          AS total_triplets,
    SUM(event_count)                            AS total_events,
    MAX(last_ingested_at)                       AS last_run
FROM notion_pages
GROUP BY dept
ORDER BY dept;
