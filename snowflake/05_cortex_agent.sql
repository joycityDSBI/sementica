-- ================================================================
-- Semantica × Snowflake — Step 5: Cortex Agent 오케스트레이터
-- ================================================================
-- 기존 Cortex Analyst + Semantica 온톨로지를 통합하는 오케스트레이터.
-- 질문 내용에 따라 자동으로 KPI 데이터 / 온톨로지 지식 / 둘 다 조회하고
-- 최종 LLM이 두 결과를 종합해 분석 답변을 생성합니다.
--
-- 전제:
--   - 01_network_access.sql 실행 완료 (semantica_external_access)
--   - 02_python_udfs.sql 실행 완료 (sementica_* UDF)
--   - 기존 Cortex Analyst YAML이 Stage에 업로드되어 있을 것
--
-- ← SEMANTIC_MODEL_FILE 상수를 실제 Stage 경로로 변경하세요.
-- ← USE DATABASE / USE SCHEMA 를 실제 값으로 변경하세요.
-- ================================================================

USE ROLE ACCOUNTADMIN;
USE DATABASE SEMENTICA;   -- ← 실제 DB로 변경
USE SCHEMA   PUBLIC;      -- ← 실제 스키마로 변경


-- ================================================================
-- Stored Procedure: sementica_agent
-- ================================================================
-- 사용 예:
--   CALL sementica_agent('지난달 POTC 매출이 감소한 이유가 뭐야?');
--   CALL sementica_agent('DS 팀의 Q3 마케팅 전략은 무엇이었나?');
--   CALL sementica_agent('POTC DAU와 UA 예산 집행 이력을 같이 보여줘');
-- ================================================================
CREATE OR REPLACE PROCEDURE sementica_agent(question VARCHAR)
  RETURNS VARIANT
  LANGUAGE PYTHON
  RUNTIME_VERSION = '3.11'
  HANDLER = 'run'
  EXTERNAL_ACCESS_INTEGRATIONS = (semantica_external_access)
  PACKAGES = ('snowflake-snowpark-python', 'requests')
AS $$
import _snowflake
import json
import requests

# ── 상수 ────────────────────────────────────────────────────────
SEMANTICA_BASE      = 'https://agility-unadvised-constrain.ngrok-free.dev'
SEMANTIC_MODEL_FILE = '@DATAHUB.DATAHUB.semantica_stage/YOUR_MODEL.yaml'  # ← 실제 경로로 변경
CLASSIFY_MODEL      = 'claude-3-5-haiku'
SYNTHESIS_MODEL     = 'claude-3-5-sonnet'
HEADERS             = {'ngrok-skip-browser-warning': '1'}


# ── 1단계: 질문 분류 ─────────────────────────────────────────────
def classify_question(session, question: str) -> str:
    """
    질문을 3가지 유형으로 분류:
      KPI       — 수치, 매출, DAU, 설치 등 정형 데이터 조회
      ONTOLOGY  — 운영 이력, 마케팅 전략, 팀 맥락, 의사결정 배경 등 비정형 지식
      BOTH      — 정형 데이터 + 비정형 지식 모두 필요
    """
    prompt = f"""다음 질문을 분류하세요.

분류 기준:
- KPI: 수치, 매출, DAU, MAU, ARPU, 설치 수, PU, 구매 전환율 등 게임 지표 데이터 조회
- ONTOLOGY: 운영 이력, 마케팅 전략, 팀 구조, 의사결정 맥락, 과거 이벤트 배경, Notion 문서 내용 등 비정형 지식
- BOTH: 수치 데이터와 운영 컨텍스트를 함께 분석해야 하는 경우

KPI, ONTOLOGY, BOTH 중 하나만 대답하세요. 이유나 설명 없이 단어만.

질문: {question}"""

    result = session.sql(
        "SELECT SNOWFLAKE.CORTEX.COMPLETE(?, ?) AS r",
        params=[CLASSIFY_MODEL, prompt]
    ).collect()[0][0].strip().upper()

    if 'BOTH' in result:
        return 'BOTH'
    elif 'ONTOLOGY' in result:
        return 'ONTOLOGY'
    else:
        return 'KPI'


# ── 2단계-A: Cortex Analyst NL-SQL ──────────────────────────────
def call_cortex_analyst(question: str) -> dict:
    """기존 Cortex Analyst를 통해 KPI 데이터 조회."""
    try:
        response = _snowflake.send_snow_api_request(
            'POST',
            '/api/v2/cortex/analyst/message',
            {},   # headers
            {},   # query params
            {
                'messages': [{
                    'role': 'user',
                    'content': [{'type': 'text', 'text': question}]
                }],
                'semantic_model_file': SEMANTIC_MODEL_FILE,
            },
            None,
            30000,
        )
        body = json.loads(response['content'])
        # Cortex Analyst 응답에서 SQL 결과 추출
        msg = body.get('message', {})
        content = msg.get('content', [])
        for item in content:
            if item.get('type') == 'sql':
                return {'sql': item.get('statement', ''), 'type': 'sql'}
            if item.get('type') == 'text':
                return {'text': item.get('text', ''), 'type': 'text'}
        return {'raw': body, 'type': 'raw'}
    except Exception as e:
        return {'error': str(e), 'type': 'error'}


# ── 2단계-B: Semantica 온톨로지 검색 ────────────────────────────
def call_semantica(question: str) -> dict:
    """Semantica REST API를 통해 온톨로지 지식 검색."""
    try:
        resp = requests.post(
            f'{SEMANTICA_BASE}/rest/hybrid',
            json={'query': question, 'limit': 5},
            headers=HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return {'error': str(e)}


# ── 3단계: Cortex Analyst SQL 실행 ──────────────────────────────
def execute_analyst_sql(session, sql: str) -> str:
    """Cortex Analyst가 생성한 SQL을 실행하고 결과를 문자열로 반환."""
    if not sql or len(sql.strip()) < 5:
        return ''
    try:
        rows = session.sql(sql).collect()
        if not rows:
            return '데이터 없음'
        # 처음 20행, 컬럼명 포함
        cols = rows[0].as_dict().keys()
        lines = [' | '.join(str(cols))]
        for r in rows[:20]:
            lines.append(' | '.join(str(v) for v in r.as_dict().values()))
        return '\n'.join(lines)
    except Exception as e:
        return f'SQL 실행 오류: {e}'


# ── 4단계: 최종 LLM 종합 ────────────────────────────────────────
def synthesize(session, question: str, kpi_data: str, ontology_data: str) -> str:
    """KPI 데이터와 온톨로지 지식을 합쳐 최종 분석 답변 생성."""
    parts = []
    if kpi_data:
        parts.append(f"[KPI 데이터]\n{kpi_data[:3000]}")
    if ontology_data:
        parts.append(f"[운영 컨텍스트 (Notion 온톨로지)]\n{ontology_data[:3000]}")

    context = '\n\n'.join(parts) if parts else '참고 자료 없음'

    prompt = f"""당신은 JoyCity 전략사업본부 AI 분석 어시스턴트입니다.
아래 참고 자료를 바탕으로 질문에 대해 명확하고 구체적인 분석 답변을 작성하세요.
수치 데이터와 운영 맥락을 연결하여 인사이트를 제공하세요.

{context}

질문: {question}

답변:"""

    result = session.sql(
        "SELECT SNOWFLAKE.CORTEX.COMPLETE(?, ?) AS r",
        params=[SYNTHESIS_MODEL, prompt]
    ).collect()[0][0]

    return result


# ── 메인 오케스트레이터 ──────────────────────────────────────────
def run(session, question: str) -> dict:
    output = {
        'question':       question,
        'classification': '',
        'kpi_sql':        '',
        'kpi_data':       '',
        'ontology_used':  False,
        'answer':         '',
    }

    try:
        # 1) 분류
        category = classify_question(session, question)
        output['classification'] = category

        kpi_text      = ''
        ontology_text = ''

        # 2a) KPI 데이터 (Cortex Analyst)
        if category in ('KPI', 'BOTH'):
            analyst_result = call_cortex_analyst(question)
            if analyst_result.get('type') == 'sql':
                sql = analyst_result['sql']
                output['kpi_sql'] = sql
                kpi_text = execute_analyst_sql(session, sql)
                output['kpi_data'] = kpi_text
            elif analyst_result.get('type') == 'text':
                kpi_text = analyst_result.get('text', '')
                output['kpi_data'] = kpi_text

        # 2b) 온톨로지 지식 (Semantica)
        if category in ('ONTOLOGY', 'BOTH'):
            sem_result = call_semantica(question)
            output['ontology_used'] = True

            # 의미 검색 결과 요약
            sem_parts = []
            for r in sem_result.get('semantic_results', [])[:5]:
                title   = r.get('title', '')
                content = r.get('content', r.get('text', ''))[:300]
                sem_parts.append(f"• {title}: {content}")

            # 그래프 결과 요약
            for r in sem_result.get('graph_results', [])[:3]:
                entity   = r.get('entity', '')
                rels     = r.get('outgoing', [])[:3]
                rel_text = ', '.join(f"{x.get('relation','')} → {x.get('target','')}" for x in rels)
                if rel_text:
                    sem_parts.append(f"• [{entity}] {rel_text}")

            ontology_text = '\n'.join(sem_parts)

        # 3) 최종 종합
        output['answer'] = synthesize(session, question, kpi_text, ontology_text)

    except Exception as e:
        output['answer'] = f'오류 발생: {str(e)}'
        output['error']  = str(e)

    return output
$$;


-- ================================================================
-- 사용 예시
-- ================================================================

-- KPI 질문 (Cortex Analyst만 호출)
CALL sementica_agent('지난달 게임별 매출 합계를 보여줘');

-- 온톨로지 질문 (Semantica만 호출)
CALL sementica_agent('POTC Q2 마케팅 전략과 UA 예산 집행 이력을 알려줘');

-- 복합 질문 (둘 다 호출 → 종합 분석)
CALL sementica_agent('POTC 8월 DAU가 감소했는데 동 기간 운영 이슈나 마케팅 변화가 있었나?');

-- 결과에서 최종 답변만 추출
SELECT r:answer::VARCHAR AS answer
FROM (SELECT sementica_agent('POTC 8월 매출 감소 원인 분석') AS r);

-- 분류 결과 확인 (디버깅용)
SELECT
    r:classification::VARCHAR AS category,
    r:kpi_sql::VARCHAR        AS generated_sql,
    r:ontology_used::BOOLEAN  AS used_ontology,
    r:answer::VARCHAR         AS answer
FROM (SELECT sementica_agent('POTC 8월 DAU 감소와 UA 이력 연결 분석') AS r);
