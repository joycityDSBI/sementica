-- ================================================================
-- Semantica × Snowflake — Step 3: External Functions 생성
-- ================================================================
-- 전제: 01_api_integration.sql 실행 완료 후 진행
--
-- 아래 두 곳을 수정하세요:
--   1. USE DATABASE / USE SCHEMA → 실제 DB.스키마 입력
--   2. NGROK_URL_HERE → ngrok 실제 주소 (슬래시 없이)
--      예) a1b2c3d4.ngrok-free.app
--
-- Snowflake External Function 입출력 규격:
--   요청(Snowflake → 서버):
--     {"data": [[row_index, param1, param2, ...]]}
--   응답(서버 → Snowflake):
--     {"data": [[row_index, {결과 VARIANT}]]}
-- ================================================================

USE ROLE ACCOUNTADMIN;
USE DATABASE SEMENTICA;       -- ← 사용할 데이터베이스로 변경
USE SCHEMA   PUBLIC;          -- ← 사용할 스키마로 변경

-- ── 1) 벡터 의미 검색 ────────────────────────────────────────────
-- 사용 예:
--   SELECT sementica_search('POTC 마케팅 이력', 5);
--   → VARIANT: {"count": 3, "results": [{...}, ...]}
CREATE OR REPLACE EXTERNAL FUNCTION sementica_search(
    query  VARCHAR,   -- 검색 쿼리
    lim    NUMBER     -- 결과 최대 개수 (기본 5)
)
  RETURNS VARIANT
  API_INTEGRATION = semantica_api_integration
  AS 'https://agility-unadvised-constrain.ngrok-free.dev/snowflake/search';


-- ── 2) 시계열 이벤트 이력 조회 ──────────────────────────────────
-- 사용 예:
--   SELECT sementica_events('POTC', 'ua_budget', '2026-08-01', '2026-08-31', 20);
--   → VARIANT: {"game": "POTC", "total": 5, "events": [{...}, ...]}
CREATE OR REPLACE EXTERNAL FUNCTION sementica_events(
    game        VARCHAR,   -- 게임 코드 (POTC, DS, FC 등)
    event_type  VARCHAR,   -- 이벤트 유형 (ua_budget, marketing 등, 빈 문자열 = 전체)
    from_date   VARCHAR,   -- 시작일 ISO (2026-08-01, 빈 문자열 = 제한 없음)
    to_date     VARCHAR,   -- 종료일 ISO (2026-08-31, 빈 문자열 = 제한 없음)
    lim         NUMBER     -- 결과 최대 개수 (기본 20)
)
  RETURNS VARIANT
  API_INTEGRATION = semantica_api_integration
  AS 'https://agility-unadvised-constrain.ngrok-free.dev/snowflake/events';


-- ── 3) 벡터 + 그래프 통합 검색 ──────────────────────────────────
-- 사용 예:
--   SELECT sementica_hybrid('DS 매출 감소 원인', 8);
--   → VARIANT: {"semantic_results": [...], "graph_results": [...], "sub_queries": [...]}
CREATE OR REPLACE EXTERNAL FUNCTION sementica_hybrid(
    query  VARCHAR,   -- 검색 쿼리
    lim    NUMBER     -- 결과 최대 개수 (기본 8)
)
  RETURNS VARIANT
  API_INTEGRATION = semantica_api_integration
  AS 'https://agility-unadvised-constrain.ngrok-free.dev/snowflake/hybrid';


-- ── 생성 확인 ──────────────────────────────────────────────────
SHOW EXTERNAL FUNCTIONS LIKE 'sementica_%';
