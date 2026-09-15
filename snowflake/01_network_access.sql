-- ================================================================
-- Semantica × Snowflake — Step 3-A: External Network Access 설정
-- ================================================================
-- Snowflake External Function은 API Gateway 필수라 복잡합니다.
-- 대신 External Network Access + Python UDF 방식을 사용합니다.
-- → API Gateway 불필요, HTTPS 엔드포인트 직접 호출
--
-- 실행 순서:
--   1. 이 파일 실행 (ACCOUNTADMIN)
--   2. 02_python_udfs.sql 실행
--
-- 전제:
--   - REST API 가 고정 도메인에 HTTPS 로 노출되어 있을 것
--   - 서버 .env 의 SNOWFLAKE_REST_TOKEN 값을 알고 있을 것
--
-- ⚠️ ngrok 은 제거했습니다 (2026-09-15). 재시작마다 URL 이 바뀌어 이 파일과
--    02·05 의 UDF 를 매번 다시 만들어야 했습니다. 고정 도메인으로 바뀌면서
--    호스트를 **두 곳에만** 둡니다 — 아래 네트워크 규칙과 base_url 시크릿.
--    UDF 본문에는 더 이상 URL 이 없습니다.
-- ================================================================

USE ROLE ACCOUNTADMIN;

-- 이 파일에서 `<SEMANTICA_HOST>` 두 곳을 실제 도메인으로 바꾸세요.
-- 예: semantica.joycityplay.com
-- (SQL 변수로 묶고 싶지만 NETWORK RULE 의 VALUE_LIST 는 변수를 받지 않습니다.)

-- ── 1) Network Rule ───────────────────────────────────────────────
-- Snowflake가 아래 호스트에 아웃바운드 HTTPS 요청을 허용
CREATE OR REPLACE NETWORK RULE semantica_network_rule
  TYPE       = HOST_PORT
  MODE       = EGRESS
  VALUE_LIST = ('<SEMANTICA_HOST>:443');

-- ── 2) API 토큰 시크릿 ────────────────────────────────────────────
-- 엔드포인트가 공개 인터넷에 열려 있으므로 REST API 에 토큰을 걸어야 합니다.
-- 서버의 .env 에 설정한 SNOWFLAKE_REST_TOKEN 과 **같은 값**을 넣으세요.
--   1) 서버:    .env 에 SNOWFLAKE_REST_TOKEN=<임의의 긴 문자열>
--   2) 여기:    아래 SECRET_STRING 에 동일 값
--   3) 재시작:  sudo systemctl restart sementica-rest
-- 토큰을 쓰지 않으려면 이 블록과 02_python_udfs.sql 의 SECRETS 절을 함께
-- 지워야 합니다. 한쪽만 설정하면 모든 UDF 가 401 로 실패합니다.
CREATE OR REPLACE SECRET semantica_rest_token
  TYPE          = GENERIC_STRING
  SECRET_STRING = 'CHANGE_ME_같은_값을_서버_.env_에도';

-- ── 2-b) 베이스 URL 시크릿 ────────────────────────────────────────
-- UDF 본문에 URL 을 박지 않기 위한 것입니다. 시크릿이 아니어도 되는 값이지만,
-- Python UDF 가 외부 설정을 읽을 통로가 SECRETS 뿐입니다.
--
-- 이렇게 두면 **도메인이 바뀔 때 UDF 를 다시 만들 필요가 없습니다** —
-- 이 시크릿과 위 네트워크 규칙만 바꾸면 됩니다:
--   ALTER SECRET semantica_base_url SET SECRET_STRING = 'https://새도메인';
CREATE OR REPLACE SECRET semantica_base_url
  TYPE          = GENERIC_STRING
  SECRET_STRING = 'https://<SEMANTICA_HOST>';

-- ── 3) External Access Integration ───────────────────────────────
CREATE OR REPLACE EXTERNAL ACCESS INTEGRATION semantica_external_access
  ALLOWED_NETWORK_RULES = (semantica_network_rule)
  ALLOWED_AUTHENTICATION_SECRETS = (semantica_rest_token, semantica_base_url)
  ENABLED               = TRUE;

-- 생성 확인
DESC INTEGRATION semantica_external_access;
SHOW NETWORK RULES LIKE 'semantica_%';
SHOW SECRETS LIKE 'semantica_%';
