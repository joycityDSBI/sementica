-- ================================================================
-- Semantica × Snowflake — Python UDF 생성 (전체)
-- ================================================================
-- 이 파일 하나로 UDF 3개와 인증 시크릿까지 만듭니다.
--
-- 전제:
--   01_network_access.sql 의 NETWORK RULE + EXTERNAL ACCESS INTEGRATION
--   (semantica_network_rule / semantica_external_access) 이 이미 있어야 합니다.
--
-- 배포 위치: DATAHUB.DATAHUB
-- 함수:
--   sementica_search(query, lim)                        벡터 의미 검색
--   sementica_events(game, etype, from, to, lim)        시계열 이벤트
--   sementica_hybrid(query, lim)                        벡터 + 그래프
--
-- ── 서버 쪽 동작 (2026-09-11 기준) ────────────────────────────────
--   · limit 상한 50, depth 상한 3 — 초과분은 서버에서 잘립니다.
--     (상한이 없던 시절 {"limit":100000} 한 번에 프로세스가 죽었습니다)
--   · hybrid 응답은 60,000자 예산으로 제한되고, 잘리면 truncated 필드가 붙습니다.
--   · 부분 실패 시 errors 필드가 붙습니다. 예전에는 Qdrant 가 죽어도 빈 결과를
--     정상 응답으로 돌려줘서 "자료 없음"과 구분되지 않았습니다.
--     → Agent 프롬프트에 "errors 가 있으면 자료 없음이 아니라 조회 실패"를
--       명시해 두는 것을 권합니다.
-- ================================================================

USE ROLE ACCOUNTADMIN;
USE DATABASE DATAHUB;
USE SCHEMA   DATAHUB;


-- ================================================================
-- 1) 인증 시크릿
-- ================================================================
-- ngrok URL 은 공개 인터넷에 열려 있습니다. 서버(.env)의
-- SNOWFLAKE_REST_TOKEN 과 **같은 값**을 넣으세요.
--
-- 순서가 중요합니다. UDF 는 토큰이 CHANGE_ME 로 시작하면 Authorization 을
-- 보내지 않고, 서버도 토큰이 비어 있으면 인증을 요구하지 않습니다. 따라서
--   ① 이 파일을 그대로 배포 (placeholder 상태 — 기존처럼 무인증으로 동작)
--   ② 실제 토큰으로 SECRET 교체
--   ③ 서버 .env 에 같은 값 설정 후 rest_api.py 재시작
-- 순서로 무중단 전환이 됩니다. ②③ 을 거꾸로 하면 그 사이 모든 UDF 가 401 입니다.
CREATE OR REPLACE SECRET semantica_rest_token
  TYPE          = GENERIC_STRING
  SECRET_STRING = 'CHANGE_ME_서버_env_의_SNOWFLAKE_REST_TOKEN_과_동일하게';

-- UDF 가 시크릿을 읽을 수 있도록 integration 에 등록
ALTER EXTERNAL ACCESS INTEGRATION semantica_external_access
  SET ALLOWED_AUTHENTICATION_SECRETS = (semantica_rest_token);


-- ================================================================
-- 2) 벡터 의미 검색
-- ================================================================
-- SELECT sementica_search('POTC 마케팅 이력', 5);
-- Cortex Agent 권장 limit: 3 (속도 우선)
CREATE OR REPLACE FUNCTION sementica_search(query VARCHAR, lim NUMBER)
  RETURNS VARIANT
  LANGUAGE PYTHON
  RUNTIME_VERSION = '3.11'
  HANDLER = 'run'
  EXTERNAL_ACCESS_INTEGRATIONS = (semantica_external_access)
  SECRETS = ('rest_token' = semantica_rest_token)
  PACKAGES = ('requests')
AS $$
import requests

_BASE = 'https://agility-unadvised-constrain.ngrok-free.dev'


def _headers():
    """Authorization 헤더를 붙입니다 (토큰이 placeholder 면 생략).

    이 헤더가 없으면 서버에서 토큰을 켜는 순간 모든 UDF 가 401 이 됩니다.
    그래서 아무도 토큰을 켜지 못하고 공개 URL 이 무인증으로 남아 있었습니다.
    """
    h = {'ngrok-skip-browser-warning': '1'}
    try:
        import _snowflake
        tok = (_snowflake.get_generic_secret_string('rest_token') or '').strip()
        if tok and not tok.startswith('CHANGE_ME'):
            h['Authorization'] = 'Bearer ' + tok
    except Exception:
        pass
    return h


def _result(resp):
    """실패해도 본문을 그대로 돌려줍니다.

    REST API 는 실패 시 {"error": "..."} 를 함께 보냅니다. raise_for_status 로
    던지면 그 메시지가 사라지고 Agent 는 원인을 알 수 없는 오류만 받습니다.
    """
    if resp.status_code >= 400:
        try:
            return {'error': resp.json().get('error', resp.text[:300]),
                    'status': resp.status_code}
        except Exception:
            return {'error': resp.text[:300], 'status': resp.status_code}
    return resp.json()


def run(query: str, lim: float) -> dict:
    resp = requests.post(
        f'{_BASE}/rest/search',
        json={'query': query, 'limit': int(lim)},
        headers=_headers(),
        timeout=30,
    )
    return _result(resp)
$$;


-- ================================================================
-- 3) 시계열 이벤트 이력
-- ================================================================
-- SELECT sementica_events('POTC', 'ua_budget', '2026-08-01', '2026-08-31', 20);
-- event_type / from_date / to_date 는 빈 문자열이면 필터 없음.
CREATE OR REPLACE FUNCTION sementica_events(
    game       VARCHAR,
    event_type VARCHAR,
    from_date  VARCHAR,
    to_date    VARCHAR,
    lim        NUMBER
)
  RETURNS VARIANT
  LANGUAGE PYTHON
  RUNTIME_VERSION = '3.11'
  HANDLER = 'run'
  EXTERNAL_ACCESS_INTEGRATIONS = (semantica_external_access)
  SECRETS = ('rest_token' = semantica_rest_token)
  PACKAGES = ('requests')
AS $$
import requests

_BASE = 'https://agility-unadvised-constrain.ngrok-free.dev'


def _headers():
    h = {'ngrok-skip-browser-warning': '1'}
    try:
        import _snowflake
        tok = (_snowflake.get_generic_secret_string('rest_token') or '').strip()
        if tok and not tok.startswith('CHANGE_ME'):
            h['Authorization'] = 'Bearer ' + tok
    except Exception:
        pass
    return h


def _result(resp):
    if resp.status_code >= 400:
        try:
            return {'error': resp.json().get('error', resp.text[:300]),
                    'status': resp.status_code}
        except Exception:
            return {'error': resp.text[:300], 'status': resp.status_code}
    return resp.json()


def run(game: str, event_type: str, from_date: str, to_date: str, lim: float) -> dict:
    resp = requests.post(
        f'{_BASE}/rest/events',
        json={
            'game':       game,
            'event_type': event_type or '',
            'from_date':  from_date  or '',
            'to_date':    to_date    or '',
            'limit':      int(lim),
        },
        headers=_headers(),
        timeout=30,
    )
    return _result(resp)
$$;


-- ================================================================
-- 4) 벡터 + 그래프 통합 검색
-- ================================================================
-- SELECT sementica_hybrid('DS 매출 감소 원인', 8);
-- sementica_search 보다 느립니다 (질문 분해 + 그래프 탐색).
-- 엔티티 관계가 꼭 필요한 질문에만 쓰세요.
CREATE OR REPLACE FUNCTION sementica_hybrid(query VARCHAR, lim NUMBER)
  RETURNS VARIANT
  LANGUAGE PYTHON
  RUNTIME_VERSION = '3.11'
  HANDLER = 'run'
  EXTERNAL_ACCESS_INTEGRATIONS = (semantica_external_access)
  SECRETS = ('rest_token' = semantica_rest_token)
  PACKAGES = ('requests')
AS $$
import requests

_BASE = 'https://agility-unadvised-constrain.ngrok-free.dev'


def _headers():
    h = {'ngrok-skip-browser-warning': '1'}
    try:
        import _snowflake
        tok = (_snowflake.get_generic_secret_string('rest_token') or '').strip()
        if tok and not tok.startswith('CHANGE_ME'):
            h['Authorization'] = 'Bearer ' + tok
    except Exception:
        pass
    return h


def _result(resp):
    if resp.status_code >= 400:
        try:
            return {'error': resp.json().get('error', resp.text[:300]),
                    'status': resp.status_code}
        except Exception:
            return {'error': resp.text[:300], 'status': resp.status_code}
    return resp.json()


def run(query: str, lim: float) -> dict:
    resp = requests.post(
        f'{_BASE}/rest/hybrid',
        json={'query': query, 'limit': int(lim)},
        headers=_headers(),
        timeout=30,
    )
    return _result(resp)
$$;


-- ================================================================
-- 5) 권한 (Agent 가 ACCOUNTADMIN 이 아닌 롤로 돈다면)
-- ================================================================
-- GRANT USAGE ON FUNCTION sementica_search(VARCHAR, NUMBER) TO ROLE <AGENT_ROLE>;
-- GRANT USAGE ON FUNCTION sementica_events(VARCHAR, VARCHAR, VARCHAR, VARCHAR, NUMBER)
--   TO ROLE <AGENT_ROLE>;
-- GRANT USAGE ON FUNCTION sementica_hybrid(VARCHAR, NUMBER) TO ROLE <AGENT_ROLE>;


-- ================================================================
-- 6) 생성 확인
-- ================================================================
SHOW USER FUNCTIONS LIKE 'sementica_%';
SHOW SECRETS LIKE 'semantica_%';

-- 동작 확인 — 결과에 error 키가 없으면 정상입니다.
SELECT sementica_search('점검 진행 프로세스', 3)        AS search_test;
SELECT sementica_events('POTC', '', '', '', 5)         AS events_test;
SELECT sementica_hybrid('서버 오픈을 담당하는 팀', 3)   AS hybrid_test;
