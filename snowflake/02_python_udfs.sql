-- ================================================================
-- Semantica × Snowflake — Step 3-B: Python UDF 생성
-- ================================================================
-- 전제: 01_network_access.sql 실행 완료
--
-- ← USE DATABASE / USE SCHEMA 를 실제 값으로 변경하세요.
-- ================================================================

USE ROLE ACCOUNTADMIN;
USE DATABASE SEMENTICA;       -- ← 실제 데이터베이스로 변경
USE SCHEMA   PUBLIC;          -- ← 실제 스키마로 변경

-- ── 공통 상수 ─────────────────────────────────────────────────────
-- ngrok URL이 바뀌면 아래 세 UDF만 재생성하면 됩니다.

-- ── 1) 벡터 의미 검색 ─────────────────────────────────────────────
-- 사용 예: SELECT sementica_search('POTC 마케팅 이력', 5);
CREATE OR REPLACE FUNCTION sementica_search(query VARCHAR, lim NUMBER)
  RETURNS VARIANT
  LANGUAGE PYTHON
  RUNTIME_VERSION = '3.11'
  HANDLER = 'run'
  EXTERNAL_ACCESS_INTEGRATIONS = (semantica_external_access)
  PACKAGES = ('requests')
AS $$
import requests, json

_BASE = 'https://agility-unadvised-constrain.ngrok-free.dev'

def run(query: str, lim: float) -> dict:
    resp = requests.post(
        f'{_BASE}/rest/search',
        json={'query': query, 'limit': int(lim)},
        headers={'ngrok-skip-browser-warning': '1'},
        timeout=30,
    )
    resp.raise_for_status()
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
  PACKAGES = ('requests')
AS $$
import requests

_BASE = 'https://agility-unadvised-constrain.ngrok-free.dev'

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
        headers={'ngrok-skip-browser-warning': '1'},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()
$$;


-- ── 3) 벡터 + 그래프 통합 검색 ────────────────────────────────────
-- 사용 예: SELECT sementica_hybrid('DS 매출 감소 원인', 8);
CREATE OR REPLACE FUNCTION sementica_hybrid(query VARCHAR, lim NUMBER)
  RETURNS VARIANT
  LANGUAGE PYTHON
  RUNTIME_VERSION = '3.11'
  HANDLER = 'run'
  EXTERNAL_ACCESS_INTEGRATIONS = (semantica_external_access)
  PACKAGES = ('requests')
AS $$
import requests

_BASE = 'https://agility-unadvised-constrain.ngrok-free.dev'

def run(query: str, lim: float) -> dict:
    resp = requests.post(
        f'{_BASE}/rest/hybrid',
        json={'query': query, 'limit': int(lim)},
        headers={'ngrok-skip-browser-warning': '1'},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()
$$;


-- ── 생성 확인 ──────────────────────────────────────────────────────
SHOW USER FUNCTIONS LIKE 'sementica_%';
