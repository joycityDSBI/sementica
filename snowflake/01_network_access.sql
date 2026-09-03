-- ================================================================
-- Semantica × Snowflake — Step 3-A: External Network Access 설정
-- ================================================================
-- Snowflake External Function은 API Gateway 필수라 복잡합니다.
-- 대신 External Network Access + Python UDF 방식을 사용합니다.
-- → API Gateway 불필요, ngrok URL 직접 호출 가능
--
-- 실행 순서:
--   1. 이 파일 실행 (ACCOUNTADMIN)
--   2. 02_python_udfs.sql 실행
--
-- 전제:
--   - ngrok이 서버에서 실행 중
--   - ngrok URL: https://agility-unadvised-constrain.ngrok-free.dev
-- ================================================================

USE ROLE ACCOUNTADMIN;

-- ── 1) Network Rule ───────────────────────────────────────────────
-- Snowflake가 아래 호스트에 아웃바운드 HTTPS 요청을 허용
CREATE OR REPLACE NETWORK RULE semantica_network_rule
  TYPE       = HOST_PORT
  MODE       = EGRESS
  VALUE_LIST = ('agility-unadvised-constrain.ngrok-free.dev:443');

-- ── 2) External Access Integration ───────────────────────────────
CREATE OR REPLACE EXTERNAL ACCESS INTEGRATION semantica_external_access
  ALLOWED_NETWORK_RULES = (semantica_network_rule)
  ENABLED               = TRUE;

-- 생성 확인
DESC INTEGRATION semantica_external_access;
SHOW NETWORK RULES LIKE 'semantica_%';
