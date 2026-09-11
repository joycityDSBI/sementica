"""
골든셋 평가 스크립트
20개 질문으로 Semantica 검색 품질을 자동 평가합니다.

평가 대상 도구:
    - hybrid_search  : 벡터 + 그래프 결합 검색 (주 평가 도구)
    - path_search    : 두 엔티티 간 최단 경로 탐색 동작 확인
    - decision_trace : 의사결정 체인 탐색 동작 확인

실행:
    python src/eval/evaluate.py                    # legacy (joycity_pages)
    python src/eval/evaluate.py --dept strategic   # 본부별 컬렉션 사용
    python src/eval/evaluate.py --dept strategic --skip-tools  # 도구 동작 확인 생략

결과:
    data/eval/eval_result_YYYYMMDD_HHMMSS.json
    data/eval/eval_report_YYYYMMDD_HHMMSS.md
"""

import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

# ─── 경로 설정 ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

# .env 로드
_env = ROOT / ".env"
if _env.exists():
    for raw_line in _env.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

# ─── 설정 ─────────────────────────────────────────────────────────────────────
import argparse as _argparse

GCP_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
LOCATION = os.environ.get("VERTEX_AI_LOCATION", "us-east5")  # 임베딩 리전
# Claude 리전은 임베딩과 별개 — ingest.py와 동일한 환경변수를 사용.
# 임베딩 리전(us-central1 등)을 넘기면 "not servable in region" 400 오류가 발생합니다.
ANTHROPIC_REGION = os.environ.get("ANTHROPIC_VERTEX_REGION", "global")
EMBED_MODEL = "text-multilingual-embedding-002"

# ── 컨텍스트 예산 ────────────────────────────────────────────────────────────
# 실제 서비스(server.py)와 조건을 일치시킵니다. 이전에는 페이지당 2000자,
# 전체 5000자로 잘라 벡터를 8건 검색해도 LLM은 2건만 보았고, 검색이 성공한
# 문항도 전달 단계에서 실패했습니다(평가가 시스템을 과소평가).
# 검색 로직·파라미터는 utils.retrieval 이 정본입니다.
# 서비스(server.py)와 같은 모듈을 쓰므로, 한쪽만 튜닝되어 평가가 다른 검색을
# 측정하던 문제(coverage 부스트 0.15 vs 0.20, 복합 판정 임계값 등)가 사라집니다.
from utils.llm import create_message
from utils.retrieval import (
    DECOMPOSE_MODEL_VERTEX as _DECOMPOSE_MODEL_VERTEX,
    DEFAULT_PAGE_LIMIT as _DEFAULT_PAGE_LIMIT,
    find_entities_in_query as _find_entities,
    merge_semantic_results as _merge_semantic_results,
    search_queries as _search_queries,
    vector_search_pages as _vector_search_pages,
)

# 검색·컨텍스트 구성
# 문서 수로만 제한하면 짧은 문서가 상위를 차지할 때 컨텍스트가 텅 빕니다
# (실측: 51자 단편 6건이 상위를 독점해 컨텍스트가 958자, 예산 60000자 중 1.6%).
# 그 상태로 근거 문서가 7위로 밀려 답을 못 했습니다. 이제 예산이 찰 때까지 채웁니다.
RETRIEVE_LIMIT = _DEFAULT_PAGE_LIMIT  # 서브쿼리당 검색할 페이지 수 (서비스와 동일)
MAX_CONTEXT_DOCS = 25  # 컨텍스트에 넣을 문서 수 상한 (안전장치)
TOP_RELATIONS = 15  # 컨텍스트에 넣을 그래프 관계 수
# 전체 컨텍스트 상한 — Sonnet 200K 토큰(한국어 약 13만 자) 대비 여유 있는 값.
# 문서를 점수 순으로 채우다 이 한도에서 중단합니다.
CONTEXT_MAX_CHARS = int(os.environ.get("CONTEXT_MAX_CHARS", "60000"))
ANSWER_MAX_TOKENS = 800  # 나열형 답변이 중간에 끊기지 않도록
SCORE_RESPONSE_CHARS = 2500  # 채점 시 응답을 자르는 한도

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
FALKORDB_HOST = os.environ.get("FALKORDB_HOST", "localhost")
FALKORDB_PORT = int(os.environ.get("FALKORDB_PORT", "6379"))
CLAUDE_MODEL = "claude-sonnet-4-6@default"

# 채점·생성·분해의 온도. 평가는 재현 가능해야 합니다 — 기본값(1.0)으로
# 샘플링하면 코드가 그대로여도 문항 점수가 ±0.5 움직여, 파라미터 변경의
# 효과와 난수를 구분할 수 없습니다. 이것 때문에 검색 튜닝을 점수로 판단하지
# 못하고 tools/ab_retrieval.py 를 따로 만들어야 했습니다.
EVAL_TEMPERATURE = float(os.environ.get("EVAL_TEMPERATURE", "0"))


# 기본값 (--dept 없을 때)
COLLECTION_NAME = "joycity_pages"
GRAPH_NAME = "joycity_kg"
DEPT_LABEL = "legacy"

# --dept / --golden 인수 처리
_parser = _argparse.ArgumentParser(add_help=False)
_parser.add_argument("--dept", default="")
_parser.add_argument("--golden", default="", help="골든셋 JSON 파일 경로 (gen_golden_set.py 결과)")
_known, _ = _parser.parse_known_args()

if _known.dept:
    sys.path.insert(0, str(ROOT / "src" / "pipeline"))
    from dept_config import load_dept as _load_dept

    _cfg = _load_dept(_known.dept)
    COLLECTION_NAME = _cfg["qdrant_collection"]
    GRAPH_NAME = _cfg["falkordb_graph"]
    DEPT_LABEL = f"{_cfg['name']} ({_known.dept})"


# ─── 골든셋 로드 ──────────────────────────────────────────────────────────────
# --golden 파일이 지정되면 그 파일에서 로드, 없으면 내장 기본 골든셋 사용
def _load_golden(path: str) -> list:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    # gen_golden_set.py 출력 형식: {"meta": {...}, "questions": [...]}
    if isinstance(data, dict) and "questions" in data:
        return data["questions"]
    # 단순 리스트 형식도 허용
    if isinstance(data, list):
        return data
    raise ValueError(f"골든셋 형식 오류: {path}")


_GOLDEN_PATH = _known.golden
if _GOLDEN_PATH and not Path(_GOLDEN_PATH).exists():
    # 조용히 내장 골든셋으로 떨어지면 안 됩니다. 경로를 하루 틀리게 적었을 때
    # 다른 부서의 옛 문항으로 평가하고도 헤더에는 지정한 파일명이 찍혀,
    # 점수만 보고 회귀로 오해하게 됩니다.
    raise SystemExit(f"❌ 골든셋 파일을 찾을 수 없습니다: {_GOLDEN_PATH}")
if _GOLDEN_PATH:
    GOLDEN_SET = _load_golden(_GOLDEN_PATH)
    print(f"  📂 외부 골든셋 로드: {_GOLDEN_PATH} ({len(GOLDEN_SET)}문항)")
else:
    # 내장 기본 골든셋 (하위 호환)
    GOLDEN_SET = [
        # 카테고리 1: 담당자
        {
            "id": "Q01",
            "category": "담당자",
            "difficulty": "easy",
            "question": "점검 시작과 서버 오픈 단계는 어느 팀이 담당하나요?",
            "answer": "운영팀",
        },
        {
            "id": "Q02",
            "category": "담당자",
            "difficulty": "easy",
            "question": "에러코드 198이 발생했을 때 확인을 요청해야 하는 담당자는 누구인가요?",
            "answer": "정보시스템팀 안제민",
        },
        {
            "id": "Q03",
            "category": "담당자",
            "difficulty": "medium",
            "question": "RESU 라이브 + PM 이슈 전달의 담당자는 누구누구인가요?",
            "answer": "김도형, 허현철, 고명수 / 김정빈, 신동화",
        },
        {
            "id": "Q04",
            "category": "담당자",
            "difficulty": "medium",
            "question": "iOS 빌드 관련 채널이 없을 때 데브옵스팀에서 문의할 수 있는 담당자는 누구인가요?",
            "answer": "임재욱",
        },
        {
            "id": "Q05",
            "category": "담당자",
            "difficulty": "medium",
            "question": "애플 앱스토어 iOS 내부테스터를 등록할 때 애니플렉스 측에 요청을 전달하는 담당자는 누구인가요?",
            "answer": "김원태",
        },
        # 카테고리 2: 정책/규정
        {
            "id": "Q06",
            "category": "정책/규정",
            "difficulty": "easy",
            "question": "점검 소요 시간 확인은 점검 당일 기준 언제까지 완료해야 하나요?",
            "answer": "점검 전날 15시까지",
        },
        {
            "id": "Q07",
            "category": "정책/규정",
            "difficulty": "medium",
            "question": "iOS 버전 표기에서 괄호 안의 숫자(예: 1.9.1(10)에서 10)는 무엇을 의미하나요?",
            "answer": "번들버전(Bundle Version)",
        },
        {
            "id": "Q08",
            "category": "정책/규정",
            "difficulty": "easy",
            "question": "QA 빌드 후 접속까지 소요되는 시간은 얼마로 안내하나요?",
            "answer": "약 30분",
        },
        {
            "id": "Q09",
            "category": "정책/규정",
            "difficulty": "medium",
            "question": "QA 빌드 공유 시 공유해야 하는 빌드 항목은 어떻게 구성되나요?",
            "answer": "안드로이드 링크 2개(애니플렉스, 조이시티) + iOS 버전 2개(애니플렉스, 조이시티)를 QA방(+DQA방)에 공유",
        },
        # 카테고리 3: 관계
        {
            "id": "Q10",
            "category": "관계",
            "difficulty": "medium",
            "question": "FDE1팀과 FDE2팀의 기반 조직과 담당 리더는 각각 누구인가요?",
            "answer": "FDE1팀: 데이터사이언스실 기반, 리더 정민호 / FDE2팀: 플랫폼실 기반, 리더 김주철",
        },
        {
            "id": "Q11",
            "category": "관계",
            "difficulty": "medium",
            "question": "온톨로지, 디지털 트윈, End-to-End 도구는 각각 어떤 역할로 설명되나요?",
            "answer": "온톨로지(규칙) → 디지털 트윈(엔진) → End-to-End 도구(화면)",
        },
        {
            "id": "Q12",
            "category": "관계",
            "difficulty": "easy",
            "question": "FDE 활동 기여도는 무엇에 반영되나요?",
            "answer": "인사 평가 (GIVE + TAKE 두 기준으로 반영)",
        },
        {
            "id": "Q13",
            "category": "관계",
            "difficulty": "medium",
            "question": "FDE 파견이 종료되면 도구와 지식은 각각 어디에 남나요?",
            "answer": "도구는 해당 팀에, 지식(온톨로지)은 전사 온톨로지에 남음",
        },
        {
            "id": "Q14",
            "category": "관계",
            "difficulty": "medium",
            "question": "AppGuard Upload/Download Timeout 에러가 지속 발생할 경우 어떻게 해야 하나요?",
            "answer": "시간을 두고 재실행하고, 지속 발생 시 라이브팀에 공유",
        },
        # 카테고리 4: 문서위치
        {
            "id": "Q15",
            "category": "문서위치",
            "difficulty": "easy",
            "question": "iOS 버전 관리 시트는 어디서 확인할 수 있나요?",
            "answer": "https://www.notion.so/joycity/2e6ea67a5681804997f6e69195b4c008",
        },
        {
            "id": "Q16",
            "category": "문서위치",
            "difficulty": "medium",
            "question": "버전 표기 규칙 문서의 파일명은 무엇인가요?",
            "answer": "STRAT-버전 표기 규칙-101125-144421.pdf",
        },
        {
            "id": "Q17",
            "category": "문서위치",
            "difficulty": "hard",
            "question": "pLTV D3D5 관련 도커 이미지는 어느 GCP 레포지토리 경로에 업로드되나요?",
            "answer": "https://console.cloud.google.com/artifacts/docker/data-science-division-216308/us-west1/pltv-preprocessor-repo/pltv-uid-d3d5-model",
        },
        # 카테고리 5: 복합
        {
            "id": "Q18",
            "category": "복합",
            "difficulty": "hard",
            "question": "빌드 중 에러코드 138과 198이 발생했을 때 각각의 대응 방법은 무엇인가요?",
            "answer": "에러코드 138: 재빌드 / 에러코드 198: 정보시스템팀 안제민 확인 (급한 경우 빌드머신 재부팅 또는 sudo pkill -f Unity.Licensing.Client)",
        },
        {
            "id": "Q19",
            "category": "복합",
            "difficulty": "medium",
            "question": "점검 진행 중 서버 상태 확인에 사용하는 도구는 무엇이 있나요?",
            "answer": "Grafana, Kibana, OpenSearch 대시보드 (서버 상태 확인 단계에서 사용)",
        },
        {
            "id": "Q20",
            "category": "복합",
            "difficulty": "hard",
            "question": "IN-JOY가 '왜 매출이 떨어졌나'에 답하지 못하는 이유는 무엇이고, FDE는 이를 어떻게 해결하려 하나요?",
            "answer": "IN-JOY는 End-to-End 도구(화면)만 먼저 만들었으나 온톨로지(규칙)와 디지털 트윈(엔진)이 비어 있어 분석 불가. FDE의 TAKE 모델로 파견 중 수집한 업무 지식을 온톨로지에 축적하여 AI 분석 기반을 구축하는 것이 해결 방향.",
        },
    ]  # ← 내장 기본 골든셋 끝 (else 블록)


# ─── 클라이언트 초기화 ────────────────────────────────────────────────────────
def init_clients():
    from anthropic import AnthropicVertex
    from google import genai
    from qdrant_client import QdrantClient

    import falkordb as fdb

    embed_client = genai.Client(project=GCP_PROJECT, location=LOCATION, vertexai=True)
    qdrant = QdrantClient(url=QDRANT_URL)
    db = fdb.FalkorDB(host=FALKORDB_HOST, port=FALKORDB_PORT)
    graph = db.select_graph(GRAPH_NAME)
    claude = AnthropicVertex(project_id=GCP_PROJECT, region=ANTHROPIC_REGION)

    return embed_client, qdrant, graph, claude


def embed(client, text: str) -> list:
    result = client.models.embed_content(model=EMBED_MODEL, contents=[text[:2000]])
    return result.embeddings[0].values


# ─── 검색 함수 ────────────────────────────────────────────────────────────────
def timeline_lookup(graph, qdrant, query: str, limit: int = 20) -> dict:
    """이벤트 타임라인을 조회합니다 — 서비스와 동일한 판정·원문 첨부 경로.

    조건 추론(시계열 의도·날짜 범위·주체 결정)과 이벤트별 원문 첨부 모두
    semantica_helper.resolve_timeline_query() 가 담당합니다.
    server.py 의 hybrid_search 도 같은 함수를 같은 인자로 호출합니다.

    ※ qdrant 를 넘기지 않으면 이벤트 원문이 빠져, "몇 건인가" 처럼 원문에만
      있는 세부를 평가에서 볼 수 없게 됩니다.
    """
    try:
        sys.path.insert(0, str(ROOT / "src" / "pipeline"))
        from semantica_helper import resolve_timeline_query

        return resolve_timeline_query(
            graph, query, limit=limit, qc=qdrant, collection_name=COLLECTION_NAME
        )
    except Exception as e:
        # 조용히 {} 를 돌려주면 날짜 문항이 근거 없이 채점되고, 그 결과가
        # 시스템 품질 저하로 보고됩니다. 최소한 눈에는 띄게 남깁니다.
        print(f"    ⚠️  타임라인 조회 실패: {type(e).__name__}: {e}")
        return {}


def semantic_search(embed_client, qdrant, query: str, limit: int = RETRIEVE_LIMIT) -> list:
    """벡터 검색 — 서비스(server.py)와 동일한 utils.retrieval 경로를 사용합니다.

    limit 은 **페이지** 수입니다. 청크 오버샘플·앵커 윈도우는 공통 모듈이 처리합니다.

    반환 항목의 키는 서비스와 동일하게 `source_url` / `content` 입니다.
    """
    return _vector_search_pages(qdrant, COLLECTION_NAME, embed(embed_client, query), limit)


def graph_search(graph, entity: str) -> list:
    """엔티티(또는 질문 문장)와 연결된 관계 탐색.

    엔티티 탐색은 utils.retrieval.find_entities_in_query() 가 담당합니다
    (역방향 매칭 → 조사 제거 폴백). server.py 의 hybrid_search 도 같은 함수를
    쓰므로 평가와 서비스가 같은 엔티티를 찾습니다.

    다만 관계 조회 형태는 다릅니다 — 서비스는 MCP graph_search 도구의
    outgoing/incoming 구조를, 평가는 컨텍스트 합성용 트리플 목록을 씁니다.
    """
    relations: list = []
    seen: set = set()

    for name in _find_entities(graph, entity, limit=5):
        try:
            res = graph.query(
                "MATCH (n)-[r:REL]->(m) WHERE n.name = $n OR m.name = $n "
                "RETURN n.name, r.rel_name, m.name, r.condition LIMIT 15",
                {"n": name},
            )
        except Exception:
            continue
        for row in res.result_set:
            key = (row[0], row[1], row[2])
            if key in seen:
                continue
            seen.add(key)
            relations.append(
                {
                    "subject": row[0],
                    "predicate": row[1],
                    "object": row[2],
                    "condition": row[3] if len(row) > 3 else "",
                }
            )

    return relations


def hybrid_search(embed_client, qdrant, graph, query: str, claude=None) -> dict:
    """벡터 + 그래프 혼합 검색 — 서비스(server.py hybrid_search)와 동일 로직.

    쿼리 분해·벡터 검색·coverage 재랭킹 모두 utils.retrieval 을 사용하므로
    평가와 서비스가 같은 검색을 수행합니다.
    """
    # ── 1. 복합 쿼리 감지 및 분해 ──────────────────────────────────────────
    # 분해되면 원본 질문도 검색 대상에 포함됩니다 (utils.retrieval.search_queries).
    sub_queries = [query]
    decomposed = False
    if claude:

        def _complete(prompt: str) -> str:
            # 분해 모델은 서비스와 동일해야 합니다 (utils.retrieval 정본).
            msg = create_message(
                claude,
                model=_DECOMPOSE_MODEL_VERTEX,
                max_tokens=400,
                temperature=EVAL_TEMPERATURE,
                messages=[{"role": "user", "content": prompt}],
            )
            return msg.content[0].text

        sub_queries, decomposed = _search_queries(query, _complete)

    # ── 2. 이벤트 타임라인 (원본 질문 기준 1회) ────────────────────────────
    timeline = timeline_lookup(graph, qdrant, query)

    # ── 3. 서브쿼리별 검색 및 결과 수집 ────────────────────────────────────
    sem_per_query: list = []
    all_graph: list = []
    graph_seen: set = set()

    for sq in sub_queries:
        sem_per_query.append(semantic_search(embed_client, qdrant, sq, limit=RETRIEVE_LIMIT))
        for r in graph_search(graph, sq):
            key = (r["subject"], r["predicate"], r["object"])
            if key not in graph_seen:
                graph_seen.add(key)
                all_graph.append(r)

    # ── 4. coverage 가중 재랭킹 (서비스와 동일한 부스트 계수) ──────────────
    sem_final = _merge_semantic_results(sem_per_query)

    # ── 5. 컨텍스트 합성 ───────────────────────────────────────────────────
    graph_text = ""
    for r in all_graph[:TOP_RELATIONS]:
        cond = f" (조건: {r['condition']})" if r.get("condition") else ""
        graph_text += f"- {r['subject']} →[{r['predicate']}]→ {r['object']}{cond}\n"

    # 이벤트 타임라인 — 날짜·카테고리·담당자를 명시해 날짜 기반 질문에 답하게 함
    timeline_text = ""
    # game 필터 없이 조회된 경우 여러 주체가 섞이므로 주체명을 함께 표기
    show_scope = not timeline.get("game")
    for ev in (timeline.get("events") or [])[:20]:
        parts = [f"- {ev.get('date', '')}"]
        if show_scope and ev.get("game"):
            parts.append(f"({ev['game']})")
        if ev.get("category"):
            parts.append(f"[{ev['category']}]")
        elif ev.get("event_type"):
            parts.append(f"[{ev['event_type']}]")
        parts.append(str(ev.get("title", "")))
        if ev.get("manager"):
            parts.append(f"(담당: {ev['manager']})")
        timeline_text += " ".join(parts) + "\n"
        if ev.get("description"):
            timeline_text += f"    {ev['description'][:300]}\n"
        # 원문 — 이벤트 노드에 없는 세부(건수·수치 등)가 여기에만 있습니다
        if ev.get("page_content"):
            timeline_text += f"    {ev['page_content'][:800]}\n"

    # 상위 순위부터 **예산이 찰 때까지** 채웁니다.
    # 개수로 자르면 짧은 문서가 상위를 차지할 때 예산을 거의 쓰지 못한 채
    # 정작 근거가 담긴 문서가 잘려나갑니다. 긴 문서는 예산에서 자연히 몇 건으로
    # 제한되고, 짧은 문서는 훨씬 많이 들어갑니다.
    doc_budget = CONTEXT_MAX_CHARS - len(graph_text) - len(timeline_text) - 200
    vector_text = ""
    used_docs = 0
    for i, s in enumerate(sem_final[:MAX_CONTEXT_DOCS]):
        block = f"[{s['title']}]\n{s['content']}\n\n"
        if i and len(vector_text) + len(block) > doc_budget:
            break  # 최소 1건은 넣되, 이후로는 예산을 지킵니다
        vector_text += block
        used_docs += 1

    # 타임라인은 날짜 질문의 직접 근거이므로 문서보다 앞에 배치합니다.
    context_parts = ["=== 그래프 관계 ===\n" + graph_text]
    if timeline_text:
        flt = timeline.get("filter") or {}
        label = timeline.get("game") or ", ".join(flt.get("keywords") or []) or "전체"
        context_parts.append(f"=== 이벤트 이력 ({label}) ===\n" + timeline_text)
    context_parts.append("=== 관련 문서 ===\n" + vector_text)

    return {
        "semantic": sem_final,
        "graph": all_graph,
        "timeline": timeline.get("events") or [],
        "decomposed": decomposed,
        "sub_queries": sub_queries if decomposed else [],
        "used_docs": used_docs,  # 실제로 컨텍스트에 들어간 문서 수
        "combined_context": "\n".join(context_parts).strip(),
    }


# ─── Claude 채점 ──────────────────────────────────────────────────────────────
SCORE_PROMPT = """당신은 검색 시스템 평가자입니다.

질문: {question}
정답: {answer}
검색 결과에서 생성된 응답: {response}

위 응답이 정답을 얼마나 잘 포함하고 있는지 채점하세요.

채점 기준:
- 1.0: 정답의 핵심 정보를 완전히 포함
- 0.5: 정답의 핵심 정보를 부분적으로 포함 (일부 누락 또는 부정확)
- 0.0: 정답과 관련 없거나 완전히 틀림

반드시 아래 JSON 형식으로만 답변하세요 (다른 텍스트 없이):
{{"score": 0.0, "reason": "한 줄 이유"}}"""


def score_with_claude(claude, question: str, answer: str, response: str) -> dict:
    prompt = SCORE_PROMPT.format(
        question=question,
        answer=answer,
        response=response[:SCORE_RESPONSE_CHARS],
    )
    try:
        msg = create_message(
            claude,
            model=CLAUDE_MODEL,
            max_tokens=200,
            temperature=EVAL_TEMPERATURE,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        # JSON 파싱
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(text[start:end])
    except Exception as e:
        print(f"    ⚠️  채점 오류: {e}")
    # harness_error 로 표시해 "틀린 답"과 구분합니다. 429 한 번에 평균이
    # 떨어지면 시스템 회귀로 오해하게 됩니다.
    return {"score": 0.0, "reason": "채점 실패", "harness_error": "채점 실패"}


def generate_response(context: str, question: str, claude) -> tuple[str, bool]:
    """검색 결과를 바탕으로 답변 생성.

    Returns:
        (응답 텍스트, 성공 여부). 실패를 빈 답변으로 뭉개면 채점에서 0.0 이
        되어 시스템 품질 문제처럼 보입니다.
    """
    prompt = f"""아래 컨텍스트를 바탕으로 질문에 답하세요. 컨텍스트에 없는 내용은 답하지 마세요.

컨텍스트:
{context[:CONTEXT_MAX_CHARS]}

질문: {question}

답변 (간결하게):"""
    try:
        msg = create_message(
            claude,
            model=CLAUDE_MODEL,
            max_tokens=ANSWER_MAX_TOKENS,
            temperature=EVAL_TEMPERATURE,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip(), True
    except Exception as e:
        return f"응답 생성 실패: {e}", False


# ─── 평가 실행 ────────────────────────────────────────────────────────────────
def run_evaluation():
    print("=" * 60)
    print("  Semantica 골든셋 평가")
    print("=" * 60)
    print(f"  본부: {DEPT_LABEL}")
    print(f"  컬렉션: {COLLECTION_NAME}  그래프: {GRAPH_NAME}")
    print(f"  시작: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  총 질문: {len(GOLDEN_SET)}개")
    if _GOLDEN_PATH:
        print(f"  골든셋:  {_GOLDEN_PATH}")
    print()

    print("🔌 클라이언트 초기화 중...")
    embed_client, qdrant, graph, claude = init_clients()
    print("✅ 완료\n")

    results = []
    category_scores = {}

    for i, item in enumerate(GOLDEN_SET, 1):
        qid = item["id"]
        cat = item["category"]
        diff = item["difficulty"]
        question = item["question"]
        answer = item["answer"]

        print(f"[{i:02d}/{len(GOLDEN_SET)}] {qid} ({cat} / {diff})")
        print(f"  Q: {question}")

        # 1. 하이브리드 검색 (복합 쿼리 자동 분해)
        t0 = time.time()
        try:
            search_result = hybrid_search(embed_client, qdrant, graph, question, claude=claude)
            context = search_result["combined_context"]
            sem_count = len(search_result["semantic"])
            grp_count = len(search_result["graph"])
            tl_count = len(search_result.get("timeline") or [])
            decomposed = search_result.get("decomposed", False)
            sub_queries = search_result.get("sub_queries", [])
        except Exception as e:
            print(f"  ❌ 검색 오류: {e}")
            results.append(
                {
                    **item,
                    "score": 0.0,
                    "reason": f"검색 실패: {e}",
                    "harness_error": "검색 실패",
                    "response": "",
                    "search_time": 0,
                }
            )
            continue  # harness_error 문항은 평균에서 제외됩니다

        search_time = round(time.time() - t0, 2)

        # 2. 응답 생성
        response, gen_ok = generate_response(context, question, claude)
        print(f"  A: {response[:100]}{'...' if len(response) > 100 else ''}")

        # 3. Claude 채점
        scored = score_with_claude(claude, question, answer, response)
        score = scored.get("score", 0.0)
        reason = scored.get("reason", "")
        harness_error = scored.get("harness_error") or (None if gen_ok else "응답 생성 실패")

        score_icon = "✅" if score >= 0.8 else ("⚡" if score >= 0.4 else "❌")
        print(f"  {score_icon} 점수: {score:.1f} | {reason}")
        decomp_info = f" [분해: {len(sub_queries)}개]" if decomposed else ""
        tl_info = f" + 이벤트 {tl_count}건" if tl_count else ""
        print(
            f"     검색: 벡터 {sem_count}건 + 그래프 {grp_count}건{tl_info} "
            f"({search_time}s){decomp_info}"
        )
        print()

        row = {
            **item,
            "score": score,
            "reason": reason,
            "response": response,
            "search_time": search_time,
            "sem_count": sem_count,
            "grp_count": grp_count,
            "used_docs": search_result.get("used_docs"),
        }
        if harness_error:
            row["harness_error"] = harness_error
        results.append(row)

        # 평가 하네스 자체가 실패한 문항은 카테고리·전체 평균 어디에도 넣지
        # 않습니다. 예전에는 검색 실패만 카테고리에서 빠지고 전체 평균에는
        # 0.0 으로 들어가, 카테고리 표에는 1.00 인데 전체는 0.4 인 상태가
        # 나올 수 있었습니다.
        if not harness_error:
            category_scores.setdefault(cat, []).append(score)

        time.sleep(0.5)  # API 요청 간격

    # ─── 결과 집계 ────────────────────────────────────────────────────────────
    # 하네스 실패(API 오류 등)는 시스템 품질이 아니므로 분모에서 뺍니다.
    scored_rows = [r for r in results if not r.get("harness_error")]
    failed_rows = [r for r in results if r.get("harness_error")]
    total_score = sum(r["score"] for r in scored_rows) / len(scored_rows) if scored_rows else 0
    passed = sum(1 for r in scored_rows if r["score"] >= 0.7)

    print("=" * 60)
    print("  📊 평가 결과 요약")
    print("=" * 60)
    print(
        f"  전체 평균:  {total_score:.3f} ({'✅ 목표 달성' if total_score >= 0.7 else '❌ 목표 미달'}, 목표 0.70)"
    )
    print(f"  통과 (≥0.7): {passed}/{len(scored_rows)}문항")
    if failed_rows:
        kinds: dict[str, int] = {}
        for r in failed_rows:
            kinds[r["harness_error"]] = kinds.get(r["harness_error"], 0) + 1
        detail = ", ".join(f"{k} {v}건" for k, v in sorted(kinds.items()))
        print(f"  ⚠️  집계 제외 {len(failed_rows)}문항 (평가 하네스 실패: {detail})")
        print(f"      해당 문항: {', '.join(r['id'] for r in failed_rows)}")
    print()
    print("  카테고리별:")
    for cat, scores in category_scores.items():
        avg = sum(scores) / len(scores)
        bar = "█" * int(avg * 10) + "░" * (10 - int(avg * 10))
        print(f"    {cat:<10} {bar} {avg:.2f}  ({len(scores)}문항)")
    print()

    # 난이도별
    for diff in ["easy", "medium", "hard"]:
        d_scores = [r["score"] for r in scored_rows if r["difficulty"] == diff]
        if d_scores:
            avg = sum(d_scores) / len(d_scores)
            print(f"  {diff:<8}: {avg:.2f} ({len(d_scores)}문항)")

    # ─── 결과 저장 ────────────────────────────────────────────────────────────
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    out_dir = ROOT / "data" / "eval"
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / f"eval_result_{ts}.json"
    json_path.write_text(
        json.dumps(
            {
                "timestamp": ts,
                "golden_set": _GOLDEN_PATH or "(내장 기본 골든셋)",
                "collection": COLLECTION_NAME,
                "total_score": round(total_score, 4),
                "passed": passed,
                "scored": len(scored_rows),
                "harness_failed": len(failed_rows),
                "category_scores": {
                    c: round(sum(s) / len(s), 4) for c, s in category_scores.items()
                },
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # Markdown 리포트
    md_lines = [
        "# Semantica 골든셋 평가 결과\n",
        f"- **평가일시**: {ts}",
        f"- **전체 평균**: {total_score:.3f} ({'✅ 목표 달성' if total_score >= 0.7 else '❌ 목표 미달'})",
        f"- **골든셋**: {_GOLDEN_PATH or '(내장 기본 골든셋)'}",
        f"- **통과 문항**: {passed}/{len(scored_rows)}"
        + (f" (하네스 실패로 {len(failed_rows)}문항 제외)" if failed_rows else "")
        + "\n",
        "## 카테고리별 점수\n",
        "| 카테고리 | 평균 점수 | 문항 수 |",
        "|---------|---------|--------|",
    ]
    for cat, scores in category_scores.items():
        avg = sum(scores) / len(scores)
        md_lines.append(f"| {cat} | {avg:.2f} | {len(scores)} |")

    md_lines += [
        "\n## 문항별 결과\n",
        "| ID | 카테고리 | 난이도 | 점수 | 이유 |",
        "|----|---------|-------|------|-----|",
    ]
    for r in results:
        icon = "✅" if r["score"] >= 0.8 else ("⚡" if r["score"] >= 0.4 else "❌")
        md_lines.append(
            f"| {r['id']} | {r['category']} | {r['difficulty']} "
            f"| {icon} {r['score']:.1f} | {r.get('reason', '')[:40]} |"
        )

    md_path = out_dir / f"eval_report_{ts}.md"
    md_path.write_text("\n".join(md_lines), encoding="utf-8")

    print("\n  💾 결과 저장:")
    print(f"     JSON: {json_path}")
    print(f"     MD:   {md_path}")
    print()

    return total_score


if __name__ == "__main__":
    score = run_evaluation()
    sys.exit(0 if score >= 0.7 else 1)
