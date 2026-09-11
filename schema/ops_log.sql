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
    error        TEXT,                              -- 오류 메시지 (정상이면 NULL)
    -- 어느 경로가 답했는가 — result_count 만으로는 구분되지 않습니다.
    -- (기존 DB 를 위해 아래쪽에 ALTER 도 함께 둡니다)
    vector_count   INT,
    graph_count    INT,
    timeline_count INT,
    sub_queries    INT,                             -- 분해된 서브쿼리 수 (1=분해 안 됨)
    truncated      BOOLEAN DEFAULT FALSE            -- 응답이 예산에 맞춰 잘렸는지
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
    is_db_item           BOOLEAN      DEFAULT FALSE,      -- Notion DB 항목 여부
    has_html_attachment  BOOLEAN      DEFAULT FALSE,      -- HTML 첨부 파일 포함 여부
    status               VARCHAR(20)  DEFAULT 'ok',       -- ok / skipped / error
    error_msg        TEXT,                            -- 오류 메시지 (정상이면 NULL)
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_notion_pages_page_dept UNIQUE (page_id, dept)
);

-- 페이지 조회 인덱스
-- 마이그레이션: 기존 DB에 컬럼 추가 (신규 설치 시 위 CREATE TABLE에 이미 포함됨)
-- psql $POSTGRES_URL -c "ALTER TABLE notion_pages ADD COLUMN IF NOT EXISTS has_html_attachment BOOLEAN DEFAULT FALSE;"

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


-- =============================================================
-- 인제스트 실행 로그 (sync_log 의 짝)
-- =============================================================
-- sync_log 는 있는데 ingest 는 없었습니다. 그래서 2026-09-11 에 LLM 호출이
-- 전부 실패해 그래프가 빈 채로 "인제스천 완료"가 찍힌 사고를 사후에 추적할
-- 수 없었습니다 — notion_pages 에 error 248건이 남았지만 **어느 실행에서**
-- 생긴 것인지 알 방법이 없었습니다.
--
-- files_found 와 pages_stored 를 함께 남기는 것이 핵심입니다. 로그의
-- "N개 저장"은 시도 횟수이고, 중복 파일이 쌓여 있으면 실제 저장된 페이지와
-- 4배까지 벌어집니다.
CREATE TABLE IF NOT EXISTS ingest_log (
    id               BIGSERIAL    PRIMARY KEY,
    ts               TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    dept             VARCHAR(50),
    mode             VARCHAR(20),        -- full / reset / dry_run
    files_found      INT,                -- 대상 .md 파일 수 (시도)
    pages_stored     INT,                -- 벡터 저장에 성공한 페이지 수
    pages_skipped    INT,
    pages_error      INT,
    chunks           INT,
    triplets         INT,
    nodes            INT,
    edges            INT,
    events           INT,
    workers          INT,
    duration_sec     INT,
    llm_temperature  REAL,               -- 실제 요청한 온도
    llm_temp_channel VARCHAR(16),        -- named / extra_body / omit — omit 이면 온도 미적용
    status           VARCHAR(20),        -- success / partial / failed / dry_run
    error_detail     TEXT
);

CREATE INDEX IF NOT EXISTS idx_ingest_log_ts   ON ingest_log (ts DESC);
CREATE INDEX IF NOT EXISTS idx_ingest_log_dept ON ingest_log (dept, ts DESC);


-- =============================================================
-- 저장소 실측 스냅샷
-- =============================================================
-- 실행 로그는 "무엇을 하려 했는가"이고, 이 표는 "지금 무엇이 들어 있는가"입니다.
-- 둘이 어긋나는 것을 사람이 눈으로 발견하는 데 하루가 걸렸습니다.
-- tools/stats.py --save 로 기록합니다 (cron 에 걸어두면 추세가 남습니다).
CREATE TABLE IF NOT EXISTS store_snapshot (
    id                 BIGSERIAL    PRIMARY KEY,
    ts                 TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    dept               VARCHAR(50),
    qdrant_chunks      INT,
    qdrant_pages       INT,             -- 서로 다른 page_id 수
    qdrant_no_page_id  INT,             -- page_id 없는 청크 (검색에 절대 안 나옴)
    graph_nodes        INT,
    graph_edges        INT,
    graph_events       INT,
    events_no_manager  INT,
    events_no_scope    INT,
    followed_by        INT,
    followed_by_skips  INT,             -- 사이에 다른 이벤트가 있는 연결
    registry_rows      INT,
    registry_error     INT,
    note               TEXT
);

CREATE INDEX IF NOT EXISTS idx_store_snap_ts ON store_snapshot (dept, ts DESC);


-- =============================================================
-- 골든셋 평가 실행 이력 (evaluate.py)
-- =============================================================
-- golden_run_log 는 대시보드 자체 골든셋용이라 별개입니다.
-- 그동안 evaluate.py 결과는 JSON 파일과 문서에만 남아, 회차 비교를 손으로
-- 해야 했습니다.
CREATE TABLE IF NOT EXISTS eval_run_log (
    id                BIGSERIAL    PRIMARY KEY,
    ts                TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    dept              VARCHAR(50),
    golden_set        TEXT,             -- 사용한 골든셋 파일 (회차 비교의 전제)
    collection        VARCHAR(100),
    total             INT,              -- 문항 수
    scored            INT,              -- 집계에 포함된 수 (하네스 실패 제외)
    harness_failed    INT,              -- API 오류 등 — 시스템 품질과 무관
    passed            INT,
    avg_score         NUMERIC(5,4),
    category_scores   JSONB,
    difficulty_scores JSONB,
    detail            JSONB             -- [{id, category, difficulty, score, reason}]
);

CREATE INDEX IF NOT EXISTS idx_eval_run_ts ON eval_run_log (dept, ts DESC);


-- =============================================================
-- mcp_request_log 보강 — 어느 경로가 답했는가
-- =============================================================
-- result_count 만으로는 벡터가 답했는지 그래프가 답했는지 타임라인이 답했는지
-- 알 수 없습니다. 실측으로 같은 종류의 UA 질문이 타임라인이 붙으면 1.0,
-- 안 붙으면 0.0 이었는데, 로그로는 구분되지 않았습니다.
ALTER TABLE mcp_request_log ADD COLUMN IF NOT EXISTS vector_count   INT;
ALTER TABLE mcp_request_log ADD COLUMN IF NOT EXISTS graph_count    INT;
ALTER TABLE mcp_request_log ADD COLUMN IF NOT EXISTS timeline_count INT;
ALTER TABLE mcp_request_log ADD COLUMN IF NOT EXISTS sub_queries    INT;   -- 분해된 서브쿼리 수 (1=분해 안 됨)
ALTER TABLE mcp_request_log ADD COLUMN IF NOT EXISTS truncated      BOOLEAN DEFAULT FALSE;


-- =============================================================
-- 유용한 뷰
-- =============================================================
-- CREATE OR REPLACE VIEW 는 컬럼 삭제·순서 변경을 허용하지 않아, 배포된
-- 정의가 파일과 다르면 "cannot drop columns from view" 로 실패합니다.
-- 뷰에는 데이터가 없으므로 DROP 후 재생성합니다.
-- 인제스트 실행별 "시도 vs 실제" 대조 — 4배 어긋난 것을 하루 만에 발견한 그 문제
DROP VIEW IF EXISTS v_ingest_vs_store;
CREATE VIEW v_ingest_vs_store AS
SELECT
    i.ts, i.dept, i.mode,
    i.files_found, i.pages_stored, i.edges AS edges_reported,
    s.qdrant_pages, s.graph_edges AS edges_actual,
    i.pages_error, i.llm_temp_channel, i.status
FROM ingest_log i
LEFT JOIN LATERAL (
    SELECT * FROM store_snapshot s
    WHERE s.dept = i.dept AND s.ts >= i.ts
    ORDER BY s.ts LIMIT 1
) s ON TRUE
ORDER BY i.ts DESC;

-- 유용한 뷰: 본부별 인제스천 현황
DROP VIEW IF EXISTS v_ingest_summary;
CREATE VIEW v_ingest_summary AS
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
