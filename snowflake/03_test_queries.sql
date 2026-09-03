-- ================================================================
-- Semantica × Snowflake — Step 6: 통합 테스트 쿼리
-- ================================================================
-- 02_python_udfs.sql 실행 완료 후 테스트하세요.
-- ================================================================

USE ROLE SYSADMIN;
USE DATABASE SEMENTICA;   -- ← 실제 DB로 변경
USE SCHEMA   PUBLIC;      -- ← 실제 스키마로 변경


-- ── 1) 연결 확인 ─────────────────────────────────────────────────
SELECT sementica_search('테스트', 1) AS health_check;
-- 기대 결과: {"count": <숫자>, "results": [...]}


-- ── 2) 벡터 검색 ─────────────────────────────────────────────────
SELECT sementica_search('POTC 마케팅 이력', 5) AS result;

-- VARIANT 내부 파싱 (Parent Document Retrieval 적용 후 content 필드 사용)
SELECT
    value:title::VARCHAR       AS title,
    value:score::FLOAT         AS score,
    value:source_url::VARCHAR  AS source_url,
    value:content::VARCHAR     AS full_content,   -- 전체 페이지 본문 (최대 4000자)
    value:chunk_count::INTEGER AS chunk_count
FROM TABLE(FLATTEN(
    input => sementica_search('POTC 마케팅 이력', 5):results
));


-- ── 3) 이벤트 이력 ───────────────────────────────────────────────
SELECT sementica_events('POTC', '', '', '', 20) AS result;

SELECT
    value:date::VARCHAR       AS event_date,
    value:event_type::VARCHAR AS event_type,
    value:title::VARCHAR      AS title
FROM TABLE(FLATTEN(
    input => sementica_events('POTC', '', '', '', 20):events
));


-- ── 4) 통합 검색 ─────────────────────────────────────────────────
SELECT sementica_hybrid('DS 매출 감소 원인 분석', 8) AS result;

SELECT
    value:title::VARCHAR  AS title,
    value:score::FLOAT    AS score
FROM TABLE(FLATTEN(
    input => sementica_hybrid('DS 매출 감소 원인 분석', 8):semantic_results
));


-- ── 5) Cortex COMPLETE 연동 예시 ─────────────────────────────────
-- Cortex LLM이 sementica_hybrid 결과를 컨텍스트로 사용
SELECT SNOWFLAKE.CORTEX.COMPLETE(
    'claude-3-5-haiku',
    CONCAT(
        '아래 온톨로지 검색 결과를 참고해 질문에 답하세요.\n\n',
        '검색 결과:\n',
        sementica_hybrid('DS 매출 감소 원인', 5)::VARCHAR,
        '\n\n질문: DS 게임의 매출이 감소한 주요 원인은 무엇인가?'
    )
) AS cortex_answer;
