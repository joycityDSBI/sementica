-- ================================================================
-- Semantica × Snowflake — Step 6: 통합 테스트 쿼리
-- ================================================================
-- 02_external_functions.sql 실행 완료 후 테스트하세요.
-- ================================================================

USE ROLE SYSADMIN;        -- ACCOUNTADMIN 불필요
USE DATABASE SEMENTICA;   -- ← 실제 DB로 변경
USE SCHEMA   PUBLIC;      -- ← 실제 스키마로 변경


-- ── 1) 헬스 체크 ─────────────────────────────────────────────────
-- External Function을 통한 기본 연결 확인
SELECT sementica_search('테스트', 1) AS health_check;
-- 기대 결과: {"count": <숫자>, "results": [...]}


-- ── 2) 벡터 검색 테스트 ──────────────────────────────────────────
SELECT sementica_search('POTC 마케팅 이력', 5) AS search_result;

-- VARIANT 내부 필드 파싱 예시
SELECT
    value:title::VARCHAR  AS title,
    value:score::FLOAT    AS score,
    value:source::VARCHAR AS source
FROM TABLE(FLATTEN(
    input => sementica_search('POTC 마케팅 이력', 5):results
));


-- ── 3) 이벤트 이력 테스트 ────────────────────────────────────────
-- 특정 게임 전체 이벤트
SELECT sementica_events('POTC', '', '', '', 20) AS events_result;

-- 기간 + 유형 필터
SELECT sementica_events('POTC', 'ua_budget', '2026-08-01', '2026-08-31', 10) AS filtered_events;

-- VARIANT 내부 파싱
SELECT
    value:date::VARCHAR       AS event_date,
    value:event_type::VARCHAR AS event_type,
    value:title::VARCHAR      AS title
FROM TABLE(FLATTEN(
    input => sementica_events('POTC', '', '', '', 20):events
));


-- ── 4) 통합 검색 테스트 ──────────────────────────────────────────
SELECT sementica_hybrid('DS 매출 감소 원인 분석', 8) AS hybrid_result;

-- 의미 검색 결과만 추출
SELECT
    value:title::VARCHAR  AS title,
    value:score::FLOAT    AS score
FROM TABLE(FLATTEN(
    input => sementica_hybrid('DS 매출 감소 원인 분석', 8):semantic_results
));


-- ── 5) Cortex Agent 연동 예시 ────────────────────────────────────
-- Cortex Analyst가 sementica_hybrid를 호출해 컨텍스트를 보강하는 패턴
-- (Cortex Agent 설정 후 사용)
SELECT SNOWFLAKE.CORTEX.COMPLETE(
    'claude-3-5-haiku',
    CONCAT(
        '다음 온톨로지 검색 결과를 참고해 답변하세요:\n\n',
        sementica_hybrid('DS 매출 감소 원인', 5)::VARCHAR,
        '\n\n질문: DS 게임의 매출이 감소한 주요 원인은 무엇인가?'
    )
) AS cortex_answer;
