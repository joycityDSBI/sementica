-- =============================================================
-- Sementica v2 마이그레이션: route 분류 + content_hash 중복 방지
-- 적용: psql $POSTGRES_URL -f schema/ops_log_migration_v2.sql
--
-- 변경 내용:
--   notion_pages.route        VARCHAR(20) DEFAULT 'core'
--     core     → LLM 전체 파이프라인 (기본값, 기존 데이터 호환)
--     defer    → 벡터 임베딩만, LLM 추출 생략 (정보 밀도 낮음)
--     excluded → 인제스천 완전 제외 (너무 짧거나 임시 문서)
--
--   notion_pages.content_hash VARCHAR(16)
--     본문 텍스트 SHA-256 앞 16자.
--     sync.py가 hash 비교로 내용 무변경 페이지의 LLM 재처리를 건너뜀.
-- =============================================================

ALTER TABLE notion_pages
    ADD COLUMN IF NOT EXISTS route        VARCHAR(20) DEFAULT 'core',
    ADD COLUMN IF NOT EXISTS content_hash VARCHAR(16) DEFAULT NULL;

-- route 인덱스 (경로별 통계 조회용)
CREATE INDEX IF NOT EXISTS idx_np_route ON notion_pages (dept, route);

-- v_ingest_summary 뷰 갱신 — route별 집계 추가
DROP VIEW IF EXISTS v_ingest_summary;
CREATE VIEW v_ingest_summary AS
SELECT
    dept,
    COUNT(*)                                       AS total_pages,
    COUNT(*) FILTER (WHERE status = 'ok')          AS ok_pages,
    COUNT(*) FILTER (WHERE status = 'skipped')     AS skipped_pages,
    COUNT(*) FILTER (WHERE status = 'error')       AS error_pages,
    COUNT(*) FILTER (WHERE route = 'core')         AS core_pages,
    COUNT(*) FILTER (WHERE route = 'defer')        AS defer_pages,
    COUNT(*) FILTER (WHERE route = 'excluded')     AS excluded_pages,
    SUM(chunk_count)                               AS total_chunks,
    SUM(triplet_count)                             AS total_triplets,
    SUM(event_count)                               AS total_events,
    MAX(last_ingested_at)                          AS last_run
FROM notion_pages
GROUP BY dept
ORDER BY dept;
