-- ================================================================
-- Semantica × Snowflake — Step 3-B: Python UDF 생성
-- ================================================================
-- 전제: 01_network_access.sql 실행 완료
--
-- ================================================================

USE ROLE ACCOUNTADMIN;
USE DATABASE DATAHUB;         -- 실제 배포 위치 (DATAHUB.DATAHUB)
USE SCHEMA   DATAHUB;

-- ── 공통 상수 ─────────────────────────────────────────────────────
-- ngrok URL이 바뀌면 아래 세 UDF만 재생성하면 됩니다.

-- ── 1) 벡터 의미 검색 ─────────────────────────────────────────────
-- 사용 예: SELECT sementica_search('POTC 마케팅 이력', 3);
-- Cortex Agent 권장 limit: 3 (속도 최적화)
CREATE OR REPLACE FUNCTION sementica_search(query VARCHAR, lim NUMBER)
  RETURNS VARIANT
  LANGUAGE PYTHON
  RUNTIME_VERSION = '3.11'
  HANDLER = 'run'
  EXTERNAL_ACCESS_INTEGRATIONS = (semantica_external_access)
  SECRETS = ('rest_token' = semantica_rest_token)
  PACKAGES = ('requests')
AS $$
import requests, json

_BASE = 'https://agility-unadvised-constrain.ngrok-free.dev'

def _headers():
    # SNOWFLAKE_REST_TOKEN 을 Authorization 으로 보냅니다. 이 헤더가 없으면
    # 서버에서 토큰을 켜는 순간 모든 UDF 가 401 이 되어, 결국 아무도 토큰을
    # 켜지 못하고 ngrok URL 이 무인증으로 남습니다.
    h = {'ngrok-skip-browser-warning': '1'}
    try:
        import _snowflake
        tok = (_snowflake.get_generic_secret_string('rest_token') or '').strip()
        if tok and not tok.startswith('CHANGE_ME'):
            h['Authorization'] = 'Bearer ' + tok
    except Exception:
        pass
    return h

def run(query: str, lim: float) -> dict:
    resp = requests.post(
        f'{_BASE}/rest/search',
        json={'query': query, 'limit': int(lim)},
        headers=_headers(),
        timeout=30,
    )
    if resp.status_code >= 400:
        # REST API 는 실패 시 {"error": "..."} 를 돌려줍니다. raise_for_status 로
        # 던지면 그 메시지가 사라지고 Agent 는 원인을 알 수 없는 Snowflake 오류만
        # 받습니다. 본문을 그대로 올려 보냅니다.
        try:
            return {'error': resp.json().get('error', resp.text[:300]), 'status': resp.status_code}
        except Exception:
            return {'error': resp.text[:300], 'status': resp.status_code}
    return resp.json()
$$;


-- ── 2) 시계열 이벤트 이력 ─────────────────────────────────────────
-- 사용 예:
--   SELECT sementica_events('POTC', 'ua_budget', '2026-08-01', '2026-08-31', 20);
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
    # SNOWFLAKE_REST_TOKEN 을 Authorization 으로 보냅니다. 이 헤더가 없으면
    # 서버에서 토큰을 켜는 순간 모든 UDF 가 401 이 되어, 결국 아무도 토큰을
    # 켜지 못하고 ngrok URL 이 무인증으로 남습니다.
    h = {'ngrok-skip-browser-warning': '1'}
    try:
        import _snowflake
        tok = (_snowflake.get_generic_secret_string('rest_token') or '').strip()
        if tok and not tok.startswith('CHANGE_ME'):
            h['Authorization'] = 'Bearer ' + tok
    except Exception:
        pass
    return h

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
    if resp.status_code >= 400:
        # REST API 는 실패 시 {"error": "..."} 를 돌려줍니다. raise_for_status 로
        # 던지면 그 메시지가 사라지고 Agent 는 원인을 알 수 없는 Snowflake 오류만
        # 받습니다. 본문을 그대로 올려 보냅니다.
        try:
            return {'error': resp.json().get('error', resp.text[:300]), 'status': resp.status_code}
        except Exception:
            return {'error': resp.text[:300], 'status': resp.status_code}
    return resp.json()
$$;


-- ── 3) 벡터 + 그래프 통합 검색 ────────────────────────────────────
-- 사용 예: SELECT sementica_hybrid('DS 매출 감소 원인', 3);
-- Cortex Agent에서는 엔티티 관계가 반드시 필요한 경우에만 사용 (sementica_search보다 느림)
-- Cortex Agent 권장 limit: 3 (속도 최적화)
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
    # SNOWFLAKE_REST_TOKEN 을 Authorization 으로 보냅니다. 이 헤더가 없으면
    # 서버에서 토큰을 켜는 순간 모든 UDF 가 401 이 되어, 결국 아무도 토큰을
    # 켜지 못하고 ngrok URL 이 무인증으로 남습니다.
    h = {'ngrok-skip-browser-warning': '1'}
    try:
        import _snowflake
        tok = (_snowflake.get_generic_secret_string('rest_token') or '').strip()
        if tok and not tok.startswith('CHANGE_ME'):
            h['Authorization'] = 'Bearer ' + tok
    except Exception:
        pass
    return h

def run(query: str, lim: float) -> dict:
    resp = requests.post(
        f'{_BASE}/rest/hybrid',
        json={'query': query, 'limit': int(lim)},
        headers=_headers(),
        timeout=30,
    )
    if resp.status_code >= 400:
        # REST API 는 실패 시 {"error": "..."} 를 돌려줍니다. raise_for_status 로
        # 던지면 그 메시지가 사라지고 Agent 는 원인을 알 수 없는 Snowflake 오류만
        # 받습니다. 본문을 그대로 올려 보냅니다.
        try:
            return {'error': resp.json().get('error', resp.text[:300]), 'status': resp.status_code}
        except Exception:
            return {'error': resp.text[:300], 'status': resp.status_code}
    return resp.json()
$$;


-- ── 생성 확인 ──────────────────────────────────────────────────────
SHOW USER FUNCTIONS LIKE 'sementica_%';
