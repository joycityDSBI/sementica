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

-- ── 2) API 토큰 시크릿 ────────────────────────────────────────────
-- ngrok URL 은 공개 인터넷에 열려 있으므로 REST API 에 토큰을 걸어야 합니다.
-- 서버의 .env 에 설정한 SNOWFLAKE_REST_TOKEN 과 **같은 값**을 넣으세요.
--   1) 서버:    .env 에 SNOWFLAKE_REST_TOKEN=<임의의 긴 문자열>
--   2) 여기:    아래 SECRET_STRING 에 동일 값
--   3) 재시작:  rest_api.py 재기동
-- 토큰을 쓰지 않으려면 이 블록과 02_python_udfs.sql 의 SECRETS 절을 함께
-- 지워야 합니다. 한쪽만 설정하면 모든 UDF 가 401 로 실패합니다.
CREATE OR REPLACE SECRET semantica_rest_token
  TYPE          = GENERIC_STRING
  SECRET_STRING = 'CHANGE_ME_같은_값을_서버_.env_에도';

-- ── 3) External Access Integration ───────────────────────────────
CREATE OR REPLACE EXTERNAL ACCESS INTEGRATION semantica_external_access
  ALLOWED_NETWORK_RULES = (semantica_network_rule)
  ALLOWED_AUTHENTICATION_SECRETS = (semantica_rest_token)
  ENABLED               = TRUE;

-- 생성 확인
DESC INTEGRATION semantica_external_access;
SHOW NETWORK RULES LIKE 'semantica_%';
SHOW SECRETS LIKE 'semantica_%';
