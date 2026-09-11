"""
JoyCity Ontology MCP 서버
Claude Code에서 사용할 수 있는 3가지 검색 도구를 제공합니다:

  semantic_search  : Qdrant 벡터 검색 — 의미 기반 유사 페이지 탐색
  graph_search     : FalkorDB 그래프 탐색 — 엔티티 관계 조회
  hybrid_search    : 벡터 + 그래프 결합 — 가장 풍부한 답변

실행:
  # stdio (Claude Code 로컬 연결)
  python src/mcp/server.py

  # Streamable HTTP (원격 연결, 포트 8765)
  python src/mcp/server.py --transport streamable-http --port 8765

Claude Code 등록:
  claude mcp add --transport http joycity-ontology http://<서버IP>:8765/mcp
"""

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

# src/ — utils 패키지(synonym_resolver·korean·datespan) import 에 필요.
# 아래 try/except 보다 먼저 실행되어야 하며, 누락 시 동의어 확장과
# 그래프 노드 매칭(hybrid_search 의 _do_graph)이 조용히 비활성화됩니다.
sys.path.insert(0, str(Path(__file__).parent.parent))

# ─── 동의어 해결기 (비즈니스 용어집 API) ────────────────────────────────────────
try:
    from utils.synonym_resolver import (
        expand as _syn_expand,
        preload as _syn_preload,
        resolve as _syn_resolve,
    )
except ImportError:

    def _syn_expand(name: str) -> list[str]:  # type: ignore[misc]
        return [name]

    def _syn_resolve(name: str) -> str:  # type: ignore[misc]
        return name

    def _syn_preload() -> None:  # type: ignore[misc]
        pass


# ─── .env 로드 ────────────────────────────────────────────────────────────────
_env_path = Path(__file__).parent.parent.parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# ─── 설정 ─────────────────────────────────────────────────────────────────────
GCP_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
LOCATION = os.environ.get("VERTEX_AI_LOCATION", "us-east5")
EMBED_MODEL = "text-multilingual-embedding-002"
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
FALKORDB_HOST = os.environ.get("FALKORDB_HOST", "localhost")
FALKORDB_PORT = int(os.environ.get("FALKORDB_PORT", "6379"))

# 검색 파라미터·공통 로직은 utils.retrieval 이 정본입니다.
# 평가 파이프라인(evaluate.py)도 같은 모듈을 쓰므로 한쪽만 튜닝되어
# 서로 다른 검색을 하던 문제가 재발하지 않습니다.
from utils.llm import create_message
from utils.retrieval import (
    DEFAULT_PAGE_LIMIT as _DEFAULT_PAGE_LIMIT,
    fetch_pages_by_source_urls as _fetch_pages_by_source_urls,
    find_entities_in_query as _find_entities,
    merge_semantic_results as _merge_semantic_results,
    search_queries as _search_queries,
    vector_search_pages as _vector_search_pages,
)

# 도구 응답 문자 예산. 분해된 질문은 (서브쿼리 수 * limit 페이지 * PAGE_MAX_CHARS)
# 라 손쉽게 수십만 자가 됩니다. 평가는 60000자로 자르는데 서비스는 자르지 않아,
# 골든셋이 재는 컨텍스트와 실제로 클라이언트에 넘기는 양이 달랐습니다.
RESPONSE_MAX_CHARS = int(os.environ.get("RESPONSE_MAX_CHARS", "60000"))
# 그래프 엣지에서 끌어오는 보조 문서 수 상한. 허브 노드(예: "운영팀")를 물면
# 엣지 source_url 이 수백 개가 되고, 그 전부의 청크를 스크롤하게 됩니다.
MAX_LINKED_PAGES = int(os.environ.get("MAX_LINKED_PAGES", "10"))


def _fit_response_budget(semantic: list, linked: list) -> tuple[list, list, bool]:
    """문자 예산에 맞게 앞에서부터 담습니다 (evaluate.py 의 예산 채우기와 동일 규칙).

    첫 문서는 예산을 넘더라도 넣습니다 — 빈 응답보다 낫습니다.
    """
    out_sem: list = []
    out_link: list = []
    total = 0
    for d in semantic:
        block = len(d.get("content", "")) + len(d.get("title", "")) + 20
        if out_sem and total + block > RESPONSE_MAX_CHARS:
            break
        total += block
        out_sem.append(d)
    for d in linked[:MAX_LINKED_PAGES]:
        block = len(d.get("content", "")) + len(d.get("title", "")) + 20
        if total + block > RESPONSE_MAX_CHARS:
            break
        total += block
        out_link.append(d)
    truncated = len(out_sem) < len(semantic) or len(out_link) < len(linked)
    return out_sem, out_link, truncated


def tool_fn(tool):
    """@mcp.tool() 데코레이트된 객체에서 실제 함수를 꺼냅니다.

    FastMCP 버전에 따라 @mcp.tool() 이 원본 함수를 그대로 돌려주기도 하고
    호출 불가능한 FunctionTool 을 돌려주기도 합니다. 후자에서 도구를 내부
    호출하면 TypeError 가 나는데, hybrid_search 의 _do_graph 는 예외를
    삼키므로 그래프 결과만 조용히 비게 됩니다. requirements 가
    fastmcp>=2.0.0 로 열려 있어 재설치 시점에 따라 갈립니다.
    """
    return getattr(tool, "fn", tool)


def _complete_for_decompose(prompt: str) -> str:
    """쿼리 분해용 LLM 호출 — 분해 로직 자체는 utils.retrieval 이 담당합니다.

    MCP 서버는 API 키 방식(anthropic.Anthropic)을 쓰고 평가는 Vertex 를 쓰므로
    클라이언트 생성만 각자 담당합니다.
    """
    import anthropic

    from utils.retrieval import DECOMPOSE_MODEL_API

    msg = create_message(
        anthropic.Anthropic(),
        model=DECOMPOSE_MODEL_API,
        max_tokens=400,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


# 기본값 (--dept 없을 때 / legacy)
COLLECTION_NAME = "joycity_pages"
GRAPH_NAME = "joycity_kg"
DEPT_NAME = "JoyCity"


def _load_dept_config(dept: str):
    """본부 설정 로드 후 전역 변수 덮어쓰기"""
    global COLLECTION_NAME, GRAPH_NAME, DEPT_NAME
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent / "pipeline"))
        from dept_config import load_dept

        cfg = load_dept(dept)
        COLLECTION_NAME = cfg["qdrant_collection"]
        GRAPH_NAME = cfg["falkordb_graph"]
        DEPT_NAME = cfg["name"]
        print(f"  본부: {DEPT_NAME} ({dept})")
        print(f"  컬렉션: {COLLECTION_NAME}  그래프: {GRAPH_NAME}")
    except Exception as e:
        print(f"  ⚠️  본부 설정 로드 실패: {e} → legacy 모드 사용")


# ─── 클라이언트 (지연 초기화) ─────────────────────────────────────────────────
_embed_client = None
_qdrant = None
_falkordb = None


def _get_embed():
    global _embed_client
    if _embed_client is None:
        from google import genai

        _embed_client = genai.Client(project=GCP_PROJECT, location=LOCATION, vertexai=True)
    return _embed_client


def _get_qdrant():
    global _qdrant
    if _qdrant is None:
        from qdrant_client import QdrantClient

        _qdrant = QdrantClient(url=QDRANT_URL)
    return _qdrant


def _get_falkordb():
    global _falkordb
    if _falkordb is None:
        import falkordb

        db = falkordb.FalkorDB(host=FALKORDB_HOST, port=FALKORDB_PORT)
        _falkordb = db.select_graph(GRAPH_NAME)
    return _falkordb


def _embed(text: str) -> list[float]:
    """텍스트 → 768차원 벡터"""
    client = _get_embed()
    result = client.models.embed_content(model=EMBED_MODEL, contents=[text[:2000]])
    return result.embeddings[0].values


def _search_event_timeline(query: str, limit: int = 20) -> dict:
    """질문에 시계열 의도가 있으면 :Event 이력을 조회하고 원문을 첨부합니다.

    조건 판정·조회·원문 첨부 모두 semantica_helper.resolve_timeline_query()
    가 담당합니다 (평가 파이프라인과 동일 경로).

    Returns:
        get_event_chain() 결과 dict + events[].page_content.
        조건 미충족·조회 실패 시 {}.
    """
    if _resolve_timeline is None:
        return {}
    try:
        return _resolve_timeline(
            _get_falkordb(),
            query,
            limit=limit,
            qc=_get_qdrant(),
            collection_name=COLLECTION_NAME,
        )
    except Exception:
        return {}


def _run_sub_search(sub_query: str, limit: int) -> tuple[list, list, list]:
    """서브쿼리 단위 벡터+그래프 검색 — ThreadPoolExecutor로 두 검색을 병렬 실행.

    Returns:
        (벡터 결과, 그래프 결과, 오류 메시지 목록)

    순차 실행 대비 레이턴시: (vec_ms + gph_ms) → max(vec_ms, gph_ms)
    일반적으로 300~500ms → 200~300ms 수준으로 단축.

    Parent Document Retrieval 적용:
    벡터 유사도로 top-k 청크를 찾은 뒤, 매칭된 page_id의 전체 청크를
    조합해 완전한 페이지 본문을 반환합니다.

    ※ limit 은 반환할 **페이지** 수입니다. 청크를 page_id 로 묶으면 결과가
      줄어들므로(같은 페이지의 청크 여러 개 → 1건) 청크는 CHUNK_OVERSAMPLE
      배로 조회합니다. 이것이 없으면 긴 문서가 상위를 독점할 때 페이지가
      1~2건만 남아 다른 문서를 아예 보지 못합니다.
    """

    errors: list[str] = []

    def _do_vector() -> list:
        try:
            return _vector_search_pages(_get_qdrant(), COLLECTION_NAME, _embed(sub_query), limit)
        except Exception as e:
            # 삼키기만 하면 Qdrant 가 죽어도 "문서 없음"과 똑같이 보입니다.
            # 빈 결과는 유지하되(부분 응답이 낫습니다) 사유는 위로 올립니다.
            errors.append(f"벡터 검색 실패: {type(e).__name__}: {e}")
            return []

    def _do_graph() -> list:
        gph: list = []
        try:
            # 엔티티 탐색은 utils.retrieval 이 담당 (역방향 매칭 → 조사 제거 폴백).
            # 평가 파이프라인도 같은 함수를 씁니다.
            seen: set = set()
            _graph_search = tool_fn(graph_search)
            for entity_name in _find_entities(_get_falkordb(), sub_query, limit=5):
                if entity_name in seen:
                    continue
                seen.add(entity_name)
                g = _graph_search(entity_name, depth=1)
                if g.get("found"):
                    gph.append(g)
                if len(gph) >= 3:
                    break
        except Exception as e:
            errors.append(f"그래프 검색 실패: {type(e).__name__}: {e}")
        return gph

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_vec = pool.submit(_do_vector)
        f_gph = pool.submit(_do_graph)
        sem = f_vec.result()
        gph = f_gph.result()

    return sem, gph, errors


# ─── DB 로거 ──────────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
try:
    from db_logger import log_mcp_request
except Exception:

    def log_mcp_request(*a, **kw):
        pass  # DB 없을 때 no-op


# ─── Semantica 헬퍼 (경로 탐색) ───────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent / "pipeline"))
try:
    from semantica_helper import (
        find_shortest_path as _find_path,
        get_event_chain as _get_event_chain,
        resolve_timeline_query as _resolve_timeline,
        trace_decision_chain as _trace_decision,
        upsert_event_node as _upsert_event_node,
    )
except Exception:
    _find_path = None
    _trace_decision = None
    _get_event_chain = None
    _upsert_event_node = None
    _resolve_timeline = None

# ─── FastMCP 서버 ─────────────────────────────────────────────────────────────
from fastmcp import FastMCP

mcp = FastMCP(
    name="JoyCity Ontology",
    instructions=(
        "JoyCity 전략사업본부 Notion 기반 지식 그래프 검색 서버입니다.\n"
        "\n"
        "【도구 선택 — 3단계 판단】\n"
        "\n"
        "STEP 1. 아래 특수 목적 도구에 해당하면 즉시 사용:\n"
        "  - 게임/서비스 이름 + 날짜·기간·이벤트 종류 → timeline_search\n"
        "    예: 'POTC 2분기 업데이트', '지난달 점검 일정', '복귀유저 이벤트 이력'\n"
        "  - 두 엔티티가 명확히 주어지고 연결 경로가 궁금할 때 → path_search\n"
        "    예: '운영팀과 배포 프로세스의 관계', '김도형에서 POTC 점검까지 연결'\n"
        "  - 승인·결정·경위·인과관계가 궁금할 때 → decision_trace\n"
        "    예: '이 정책이 결정된 경위', '운영팀 승인 과정'\n"
        "\n"
        "STEP 2. STEP 1에 해당하지 않으면 → hybrid_search 사용 (기본값)\n"
        "  - 담당자·팀·정책·절차 등 일반 업무 질문은 대부분 여기에 해당\n"
        "  - 문서 본문과 관계 그래프 중 어디에 답이 있는지 알 수 없으므로\n"
        "    hybrid_search가 두 곳을 동시에 탐색하여 가장 안전한 선택\n"
        "  - 예: '점검 담당팀은?', 'POTC 운영 정책', '신규유저 이벤트 기획 절차'\n"
        "\n"
        "STEP 3. hybrid_search 결과가 부족하면 보완적으로 단독 사용:\n"
        "  - 특정 이름(사람·팀)의 관계망만 빠르게 확인 → graph_search\n"
        "  - 특정 주제의 문서 본문만 더 많이 확인 → semantic_search\n"
        "\n"
        "【공통 원칙】\n"
        "- 한 번의 도구 호출로 답이 불충분하면 다른 도구를 추가 호출하세요.\n"
        "- 어떤 도구를 써야 할지 1초라도 고민된다면 hybrid_search를 쓰세요.\n"
        "- semantic_search와 graph_search는 독립적으로 쓰기보다\n"
        "  hybrid_search 결과를 보완하는 용도로 활용하는 것이 좋습니다."
    ),
)


@mcp.tool()
def semantic_search(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """
    Notion 문서 본문을 의미 기반(벡터 유사도)으로 검색합니다.
    키워드가 정확히 일치하지 않아도 의미가 유사한 문서를 찾아줍니다.

    ⚠️ 일반적인 업무 질문에는 hybrid_search를 먼저 사용하세요.
    semantic_search는 "Notion 문서 본문에 답이 있다고 확신할 때"
    또는 hybrid_search 결과가 부족할 때 보완적으로 사용합니다.

    【이 도구가 적합한 경우 (hybrid_search 대신 단독 사용)】
    - hybrid_search를 이미 호출했지만 문서 내용이 부족하여 더 많은 문서를 봐야 할 때
    - 답이 Notion 문서 텍스트 본문에 있다고 명확히 알고 있을 때
      예: "운영 가이드 문서 전문을 찾아줘", "배포 체크리스트 문서"
    - 특정 주제의 문서를 폭넓게 수집해야 할 때 (limit를 높게 설정)

    【hybrid_search를 대신 써야 하는 경우】
    - "~담당팀은?", "~절차는?", "~정책은?" 등 답이 문서에 있을 수도,
      그래프 관계에 있을 수도 있는 모든 일반 질문 → hybrid_search 사용

    【timeline_search·path_search·decision_trace를 써야 하는 경우】
    - 게임 이벤트 이력 → timeline_search
    - 두 엔티티 연결 경로 → path_search
    - 의사결정 경위 → decision_trace

    Args:
        query: 검색할 자연어 질문 또는 키워드 (한국어 가능).
               구체적일수록 정확도가 높아집니다.
               예: "POTC 점검 공지 절차" (좋음) vs "점검" (너무 짧음)
        limit: 반환할 최대 결과 수. 기본값 5.
               보완 탐색 시 10~15로 늘려 더 많은 후보를 확인하세요.

    Returns:
        list of {
            title:       Notion 페이지 제목,
            source_url:  Notion 원본 URL (반드시 인용),
            content:     페이지 전체 본문 (chunk_index 순 조합, PAGE_MAX_CHARS 한도.
                         초과하면 매칭 청크 중심으로 잘라내고 생략 표시를 넣습니다),
            chunk_count: 이 페이지의 총 청크 수,
            score:       유사도 점수 (0~1, 높을수록 관련성 높음)
        }

    Note:
        Parent Document Retrieval 적용:
        벡터 유사도로 top-k 청크를 찾은 뒤, 해당 청크와 같은 page_id를 가진
        모든 청크를 chunk_index 순서대로 이어붙여 완전한 문맥을 반환합니다.
        단순 청크 미리보기(text_preview) 대신 전체 본문을 제공하므로
        문맥 손실 없이 정확한 해석이 가능합니다.
    """
    _t0 = time.time()
    _err = None
    results = []  # finally 블록에서 참조 가능하도록 try 외부에서 초기화
    try:
        # 청크 오버샘플 + 앵커 윈도우를 포함한 공통 경로 사용
        # (hybrid_search 와 동일한 로직 — 이전에는 이 도구만 오버샘플이 빠져 있었음)
        results = _vector_search_pages(_get_qdrant(), COLLECTION_NAME, _embed(query), limit)
        return results
    except Exception as e:
        _err = str(e)
        raise
    finally:
        log_mcp_request(
            dept=DEPT_NAME,
            tool="semantic_search",
            query=query,
            result_count=len(results) if _err is None else 0,
            duration_ms=int((time.time() - _t0) * 1000),
            error=_err,
        )


@mcp.tool()
def graph_search(entity: str, depth: int = 1) -> dict[str, Any]:
    """
    특정 엔티티(사람·팀·프로세스·시스템 등)를 중심으로 연결된 관계를 그래프에서 탐색합니다.
    문서 내용이 아닌 '구조적 관계'(누가 무엇을 담당하는지, 어떤 팀에 속하는지 등)를 파악할 때 사용합니다.

    ⚠️ 일반적인 업무 질문에는 hybrid_search를 먼저 사용하세요.
    graph_search는 "특정 엔티티의 관계망이 그래프에 있다고 확신할 때"
    또는 hybrid_search 결과가 부족하여 관계 구조를 더 정밀하게 볼 때 보완적으로 사용합니다.

    【이 도구가 적합한 경우 (hybrid_search 대신 단독 사용)】
    - hybrid_search를 이미 호출했지만 관계 정보가 부족하여 더 상세히 펼쳐봐야 할 때
    - 특정 이름(사람·팀·프로세스)이 명확히 주어지고, 그 엔티티의 관계망 전체를 구조적으로 봐야 할 때
      예: "운영팀의 모든 담당 업무와 소속 인원 구조를 보여줘"
    - depth=2로 2단계 관계까지 펼쳐야 할 때

    【hybrid_search를 대신 써야 하는 경우】
    - "~담당팀은?", "~의 담당자는?", "~팀이 하는 일은?" 같이
      답이 그래프에 있을 수도, 문서 본문에 있을 수도 있는 모든 일반 질문 → hybrid_search 사용

    【예시 (보완 탐색 시)】
    - hybrid_search("점검 담당팀") 결과 확인 후 관계 상세가 필요하면
      → graph_search(entity="운영팀") 으로 보완
    - entity="운영팀"  → 운영팀의 outgoing/incoming 관계 전체 조회
    - entity="김도형"  → 김도형의 소속·담당 관계 조회
    - entity="POTC"    → POTC와 연결된 팀·프로세스 조회

    【depth 선택 기준】
    - depth=1 (기본): 직접 연결된 관계만. "운영팀이 직접 담당하는 것"
    - depth=2: 2단계 연결. "운영팀 담당자가 참여하는 다른 프로세스까지"
      (depth=2는 결과가 많아질 수 있으므로 필요할 때만 사용)

    Args:
        entity: 탐색할 엔티티 이름. 부분 일치 가능.
                정확한 이름을 모르면 짧게 입력하세요. 예: "운영" → "운영팀" 매칭
        depth:  탐색 깊이. 1=직접 관계(기본값), 2=2홉 관계

    Returns:
        {
            entity:   실제 매칭된 엔티티 이름,
            type:     엔티티 유형 (Person/Team/Process/System/Policy 등),
            found:    검색 성공 여부 (false면 해당 엔티티가 그래프에 없음),
            outgoing: [{relation, target_name, target_type, condition, order, source_url,
                        evidence_quote, realization_status, evidence_chunk_id, source_text}]
                      이 엔티티에서 나가는 관계 목록
                      (evidence_quote: 원문 인용, realization_status: planned/applied/unconfirmed,
                       source_text: 해당 Qdrant 청크 전문 — evidence_chunk_id가 있을 때만 포함),
            incoming: [{relation, source_name, source_type, source_url}]
                      이 엔티티로 들어오는 관계 목록 (누가 이 엔티티와 관계를 맺는지)
        }
    """
    _t0 = time.time()
    _err = None
    _result = None
    try:
        graph = _get_falkordb()

        # 동의어 확장: "드래곤슈퍼" → ["DS", "드래곤슈퍼", "Dragon Super"]
        # canonical로 저장된 신규 데이터와 비정규 이름의 구버전 데이터를 모두 검색합니다.
        entity_forms = _syn_expand(entity)
        node_query = (
            "MATCH (n) WHERE ANY(form IN $forms WHERE n.name CONTAINS form) "
            "RETURN n.name AS name, labels(n)[0] AS type LIMIT 5"
        )
        node_result = graph.query(node_query, {"forms": entity_forms})

        if not node_result.result_set:
            _result = {"entity": entity, "found": False, "relations": []}
            return _result

        # 첫 번째 매칭 노드 기준으로 관계 탐색
        matched_name = node_result.result_set[0][0]
        matched_type = node_result.result_set[0][1]

        if depth == 1:
            # v2: evidence_quote·realization_status·evidence_chunk_id 포함
            rel_query = (
                "MATCH (n {name: $name})-[r:REL]->(m) "
                "RETURN r.rel_name, m.name, labels(m)[0], "
                "r.condition, r.order, r.source_url, "
                "r.evidence_quote, r.realization_status, r.evidence_chunk_id "
                "LIMIT 20"
            )
            rel_result = graph.query(rel_query, {"name": matched_name})
        else:
            # path 기반 추출 (depth=2) — source_url 제외, FalkorDB r[-1] 미지원
            rel_query = (
                "MATCH p=(n {name: $name})-[:REL*1..2]->(m) "
                "RETURN [r IN relationships(p) | r.rel_name] AS relation, "
                "m.name AS target, labels(m)[0] AS target_type "
                "LIMIT 30"
            )
            rel_result = graph.query(rel_query, {"name": matched_name})

        relations = []
        for row in rel_result.result_set:
            rel = {
                "relation": row[0],
                "target_name": row[1],
                "target_type": row[2],
            }
            if depth == 1:
                # row: [rel_name, target, target_type, condition, order, source_url,
                #        evidence_quote, realization_status, evidence_chunk_id]
                if len(row) > 3 and row[3]:
                    rel["condition"] = row[3]
                if len(row) > 4 and row[4]:
                    rel["order"] = row[4]
                if len(row) > 5 and row[5]:
                    rel["source_url"] = row[5]
                if len(row) > 6 and row[6]:
                    rel["evidence_quote"] = row[6]
                if len(row) > 7 and row[7]:
                    rel["realization_status"] = row[7]
                if len(row) > 8 and row[8]:
                    rel["evidence_chunk_id"] = row[8]
            else:
                pass  # depth=2: rel_name만 (path 기반)
            relations.append(rel)

        # ── evidence_chunk_id → Qdrant 직접 조회로 원문 청크 텍스트 첨부 ──────
        # 벡터 유사도 검색보다 훨씬 빠름 (O(1) ID 조회)
        chunk_ids = [r["evidence_chunk_id"] for r in relations if r.get("evidence_chunk_id")]
        if chunk_ids:
            try:
                qc = _get_qdrant()
                pts = qc.retrieve(
                    collection_name=COLLECTION_NAME,
                    ids=chunk_ids,
                    with_payload=["text"],
                )
                id_to_text = {str(pt.id): (pt.payload or {}).get("text", "") for pt in pts}
                for r in relations:
                    cid = r.get("evidence_chunk_id")
                    if cid and cid in id_to_text:
                        r["source_text"] = id_to_text[cid]
            except Exception:
                pass  # chunk 조회 실패해도 그래프 결과는 정상 반환

        # 역방향 관계 탐색 (누가 이 엔티티와 관계를 맺는지)
        # depth=1: 직접 연결된 1홉 incoming
        # depth=2: 1홉 + 2홉 indirect incoming (중복 제거)
        incoming: list = []
        seen_incoming: set = set()

        # ── 1홉 incoming (depth=1·2 공통) ────────────────────────────────────
        rev1_result = graph.query(
            "MATCH (m)-[r:REL]->(n {name: $name}) "
            "RETURN r.rel_name AS relation, m.name AS source, labels(m)[0] AS source_type, "
            "r.source_url AS source_url "
            "LIMIT 10",
            {"name": matched_name},
        )
        for row in rev1_result.result_set:
            src = row[1]
            if src not in seen_incoming:
                seen_incoming.add(src)
                incoming.append(
                    {
                        "relation": row[0],
                        "source_name": src,
                        "source_type": row[2],
                        "source_url": row[3] if len(row) > 3 else "",
                    }
                )

        # ── 2홉 incoming (depth=2 전용 추가) ──────────────────────────────────
        if depth == 2:
            rev2_result = graph.query(
                "MATCH (m)-[:REL]->(x)-[r:REL]->(n {name: $name}) "
                "RETURN r.rel_name AS relation, m.name AS source, labels(m)[0] AS source_type "
                "LIMIT 10",
                {"name": matched_name},
            )
            for row in rev2_result.result_set:
                src = row[1]
                if src not in seen_incoming:
                    seen_incoming.add(src)
                    incoming.append(
                        {
                            "relation": f"{row[0]} (2홉)",
                            "source_name": src,
                            "source_type": row[2],
                            "source_url": "",
                        }
                    )

        _result = {
            "entity": matched_name,
            "type": matched_type,
            "found": True,
            "outgoing": relations,
            "incoming": incoming,
        }
        return _result
    except Exception as e:
        _err = str(e)
        raise
    finally:
        log_mcp_request(
            dept=DEPT_NAME,
            tool="graph_search",
            query=entity,
            result_count=len(_result.get("outgoing", [])) if _result else 0,
            duration_ms=int((time.time() - _t0) * 1000),
            error=_err,
        )


@mcp.tool()
def hybrid_search(query: str, limit: int = _DEFAULT_PAGE_LIMIT) -> dict[str, Any]:
    """
    벡터 검색(문서 내용) + 그래프 탐색(관계 구조)을 동시에 수행하는 통합 검색입니다.
    복합 질문을 자동으로 서브쿼리로 분해하여 각각 검색한 뒤 결과를 병합합니다.

    ✅ timeline_search·path_search·decision_trace에 해당하지 않는 모든 일반 질문의 기본 도구입니다.
    "어떤 도구를 써야 할지 1초라도 고민된다면 hybrid_search를 사용하세요."

    【이 도구를 사용해야 하는 경우 — 사실상 대부분의 업무 질문】
    - "~담당팀은?", "~의 담당자는?", "~절차는?", "~정책은?"
      (답이 문서에 있을 수도, 그래프에 있을 수도 있는 모든 질문)
    - "~팀에서 ~업무를 담당하는 사람이 작성한 문서는?" 같은 복합 질문
    - 처음 탐색을 시작할 때 — 어디서 답을 찾아야 할지 모를 때
    - semantic_search나 graph_search 중 무엇을 써야 할지 불분명할 때

    【다른 도구를 대신 써야 하는 경우】
    - 게임 이름 + 날짜·기간·이벤트 종류 조합 → timeline_search
    - 두 엔티티가 명확하고 연결 경로가 궁금 → path_search
    - 승인·결정·경위 추적 → decision_trace
    - hybrid_search 결과 확인 후 문서 본문이 더 필요 → semantic_search 보완
    - hybrid_search 결과 확인 후 관계 구조가 더 필요 → graph_search 보완

    【복합 쿼리 자동 분해 동작】
    질문이 길거나 복합 조건이 있으면 Claude Haiku가 자동으로 2~3개 서브쿼리로 분해합니다.
    예: "운영팀에서 POTC 점검을 담당하는 사람이 작성한 배포 가이드는?"
    → ["운영팀 POTC 점검 담당자", "POTC 점검 배포 가이드", "운영팀 작성 문서"]
    각 서브쿼리에서 공통으로 등장하는 문서에 가중치를 부여해 재랭킹합니다.

    【예시 질문 → 이 도구로 해결되는 것들】
    - "점검 담당팀은?"
    - "POTC 운영 정책 문서와 담당자"
    - "신규유저 이벤트 기획 절차"
    - "전략사업본부 게임 배포 승인 절차와 관련 팀"
    - "글로벌 서비스 운영 가이드"

    Args:
        query: 검색할 자연어 질문 (한국어 가능). 복합 질문도 그대로 입력하세요.
               AI가 자동으로 서브쿼리로 분해합니다.
        limit: 서브쿼리당 반환할 **페이지** 수 (기본값: 12).
               짧은 문서(50~300자 단편)가 상위를 차지하면 8건으로는 정보량이
               부족해 정작 근거가 담긴 문서가 밀려납니다. 더 넓은 탐색이
               필요하면 15~20까지 올리세요.

    Returns:
        {
            semantic_results: 벡터 검색 결과 목록 [{title, source_url, content, chunk_count, score, coverage}],
            graph_results:    그래프 탐색 결과 목록 [{entity, type, outgoing, incoming}],
            entity_summary:   관계 요약 문자열 목록 ["엔티티A → 관계 → 엔티티B", ...],
            linked_pages:     그래프 엣지 source_url로 연결된 추가 문서 [{title, source_url, content, chunk_count}]
                              (semantic_results에 없는 페이지만 포함 — 그래프-벡터 명시적 교차 연결),
            decomposed:       복합 쿼리 분해 여부 (true/false),
            sub_queries:      분해된 서브쿼리 목록 (decomposed=true일 때만),

            timeline_results: 날짜 기반 질문일 때만 존재.
                              {game, total, events: [{date, category, event_type, title,
                               description, target, manager, source_url, page_content, ...}]}
                              질문에 게임/서비스 이름과 날짜(또는 이력·변경 등 시계열
                              키워드)가 함께 있을 때 :Event 노드에서 자동 조회됩니다.
                              category는 Notion "변경카테고리" 원문입니다.
            timeline_summary: ["2026-06-19: [소재변경] 소재 3건 OFF", ...] 형태 요약.
                              위 두 키는 조건 미충족 시 아예 포함되지 않습니다.
        }

    【날짜 기반 질문도 이 도구로 처리됩니다】
    "2026년 6월 19일 RESU에서 OFF된 소재는?" 처럼 게임명 + 날짜가 있는 질문은
    timeline_results 가 자동으로 채워지므로 timeline_search 를 따로 호출할
    필요가 없습니다. 특정 게임의 전체 이력을 기간·유형으로 정밀하게 필터링해야
    할 때만 timeline_search 를 사용하세요.
    """
    _t0 = time.time()
    _err = None
    _result = None
    try:
        # ── 1. 복합 쿼리 감지 및 서브쿼리 분해 ─────────────────────────────
        # 분해되면 원본 질문도 검색 대상에 포함됩니다 — 분해 과정에서 긴 엔티티
        # 이름이 축약되면 그래프 노드 매칭이 깨지기 때문입니다.
        sub_queries, decomposed = _search_queries(query, _complete_for_decompose)

        # ── 2. 서브쿼리별 벡터+그래프 병렬 검색 ───────────────────────────
        # 각 서브쿼리는 독립적 → 서브쿼리 간에도 병렬 실행
        # 내부에서 _run_sub_search가 vector+graph를 이미 병렬 실행
        # 총 스레드: len(sub_queries) x 2 (최대 8개)
        # 레이턴시: O(max(sub_queries)) 아닌 O(max(single_sub_query))
        sem_per_q: list[list] = []
        all_graph_hits: list = []
        search_errors: list[str] = []

        # +1 워커: 이벤트 타임라인 조회를 서브쿼리 검색과 병렬로 실행하므로
        # 레이턴시가 추가되지 않습니다.
        n_workers = min(len(sub_queries), 4) + 1
        with ThreadPoolExecutor(max_workers=n_workers) as sq_pool:
            # 타임라인은 원본 질문 기준으로 1회만 조회합니다.
            # (날짜·게임명은 질문 전체의 속성이므로 서브쿼리로 쪼갤 필요가 없음)
            tl_future = sq_pool.submit(_search_event_timeline, query)
            sq_futures = [sq_pool.submit(_run_sub_search, sq, limit) for sq in sub_queries]
            for fut in as_completed(sq_futures):
                sem, gph, errs = fut.result()
                sem_per_q.append(sem)
                all_graph_hits.extend(gph)
                search_errors.extend(errs)
            timeline = tl_future.result()

        # ── 3. 벡터 결과 병합 (coverage 재랭킹) ────────────────────────────
        semantic = _merge_semantic_results(sem_per_q)

        # ── 4. 그래프 결과 중복 제거 ────────────────────────────────────────
        seen_entities: set = set()
        graph_hits: list = []
        for g in all_graph_hits:
            if g["entity"] not in seen_entities:
                seen_entities.add(g["entity"])
                graph_hits.append(g)

        # ── 5. 관계 요약 ────────────────────────────────────────────────────
        entity_summary: list[str] = [
            f"{g['entity']} → {rel['relation']} → {rel['target_name']}"
            for g in graph_hits
            for rel in (g.get("outgoing") or [])[:3]
        ]

        # ── 6. 그래프 엣지 source_url → 벡터 DB 원문 연결 ──────────────────
        # semantic_results에 없는 페이지만 linked_pages로 추가합니다.
        # (그래프가 참조하는 문서 중 벡터 유사도 상위에 없었던 것을 보완)
        existing_urls: set = {r.get("source_url", "") for r in semantic}
        edge_urls: list = list(
            {
                rel.get("source_url", "")
                for g in graph_hits
                for rel in (g.get("outgoing", []) + g.get("incoming", []))
                if rel.get("source_url") and rel["source_url"] not in existing_urls
            }
            - {""}
        )

        linked_pages: list = []
        if edge_urls:
            try:
                qc = _get_qdrant()
                # 페이지마다 청크를 스크롤하므로 가져올 개수를 먼저 자릅니다.
                lp_map = _fetch_pages_by_source_urls(
                    qc, COLLECTION_NAME, edge_urls[:MAX_LINKED_PAGES], max_chars=1500
                )
                linked_pages = list(lp_map.values())
            except Exception as e:
                search_errors.append(f"연결 문서 조회 실패: {type(e).__name__}: {e}")

        # ── 6-b. 응답 크기 제한 ─────────────────────────────────────────────
        semantic, linked_pages, _truncated = _fit_response_budget(semantic, linked_pages)

        _result = {
            "semantic_results": semantic,
            "graph_results": graph_hits,
            "entity_summary": entity_summary,
            "linked_pages": linked_pages,
            "decomposed": decomposed,
            "sub_queries": sub_queries if decomposed else [],
        }
        if _truncated:
            _result["truncated"] = f"응답이 {RESPONSE_MAX_CHARS}자 예산에 맞춰 잘렸습니다."
        if search_errors:
            # 결과가 비었을 때 "자료가 없다"와 "검색이 실패했다"를 호출한
            # 모델이 구분할 수 있어야 합니다.
            _result["errors"] = search_errors

        # ── 7. 이벤트 타임라인 (날짜 기반 질문일 때만 존재) ─────────────────
        # 조건 미충족 시 키를 넣지 않습니다 — 호출자가 무관한 빈 필드를
        # 해석하려 시도하지 않도록.
        if timeline and timeline.get("events"):
            _result["timeline_results"] = {
                "game": timeline.get("game", ""),
                "total": timeline.get("total", 0),
                # 어떤 조건으로 조회된 이벤트인지 (게임 / 키워드 / 기간)
                "filter": timeline.get("filter", {}),
                "events": timeline["events"],
            }
            _result["timeline_summary"] = timeline.get("timeline_summary", [])
        return _result
    except Exception as e:
        _err = str(e)
        raise
    finally:
        # 부분 실패도 로그에 남깁니다. 예전에는 Qdrant 가 죽어 빈 결과가
        # 나가도 error=None 으로 기록돼, 대시보드에서 100% 성공으로 보였습니다.
        _partial = "; ".join(_result.get("errors", [])) if _result else ""
        _tl = (_result.get("timeline_results") or {}).get("events", []) if _result else []
        log_mcp_request(
            dept=DEPT_NAME,
            tool="hybrid_search",
            query=query,
            result_count=len(_result.get("semantic_results", [])) if _result else 0,
            duration_ms=int((time.time() - _t0) * 1000),
            error=_err or _partial or None,
            # 어느 경로가 답했는지 — result_count 만으로는 구분되지 않습니다.
            # 같은 종류의 질문이 타임라인이 붙으면 1.0, 안 붙으면 0.0 인 사례가
            # 있었는데 로그로는 보이지 않았습니다.
            vector_count=len(_result.get("semantic_results", [])) if _result else 0,
            graph_count=len(_result.get("graph_results", [])) if _result else 0,
            timeline_count=len(_tl),
            sub_queries=len(_result.get("sub_queries") or []) or 1 if _result else None,
            truncated=bool(_result.get("truncated")) if _result else False,
        )


@mcp.tool()
def path_search(start_entity: str, end_entity: str, max_hops: int = 6) -> dict[str, Any]:
    """
    두 엔티티 사이의 최단 연결 경로(관계 체인)를 그래프에서 탐색합니다.
    "A와 B는 어떻게 연결되어 있는가?"를 알고 싶을 때 사용합니다.

    【이 도구를 사용해야 하는 경우】
    - 두 엔티티가 명확히 주어지고, 둘 사이의 연결 관계를 알고 싶을 때
    - "~팀과 ~프로세스는 어떤 관계인가?"
    - "~사람이 ~업무에 어떻게 관여하는가?"
    - "~시스템에서 ~담당자까지 어떻게 연결되는가?"
    - graph_search로 단일 엔티티를 봤지만 다른 엔티티와의 연결이 궁금할 때

    【이 도구를 사용하면 안 되는 경우】
    - 한 엔티티의 전체 관계를 보고 싶을 때 → graph_search 사용
    - 두 엔티티가 명확히 특정되지 않은 경우 → graph_search 또는 hybrid_search 사용

    【예시 질문】
    - "운영팀과 배포 프로세스는 어떻게 연결되는가?" → start="운영팀", end="배포 프로세스"
    - "김도형과 POTC 점검의 관계는?" → start="김도형", end="POTC 점검"
    - "전략사업본부에서 글로벌 출시까지 경로" → start="전략사업본부", end="글로벌 출시"

    Args:
        start_entity: 경로 탐색 시작 엔티티 이름. 부분 일치 가능.
                      예: "운영팀", "김도형", "POTC"
        end_entity:   경로 탐색 도착 엔티티 이름. 부분 일치 가능.
                      예: "점검 완료", "배포", "글로벌 출시"
        max_hops:     탐색할 최대 관계 단계 수 (기본값: 6).
                      직접 연결이면 1, 중간 단계가 있으면 그 수만큼 증가.
                      너무 크면 탐색 시간이 길어질 수 있으므로 기본값 유지 권장.

    Returns:
        {
            found:          경로 발견 여부,
            start:          실제 매칭된 시작 엔티티 이름,
            end:            실제 매칭된 도착 엔티티 이름,
            path_nodes:     경로 상의 엔티티 목록 ["엔티티A", "엔티티B", "엔티티C"],
            path_relations: 각 단계의 관계명 목록 ["담당", "포함"],
            hops:           경로 단계 수
        }
        found=false이면 두 엔티티 사이에 경로가 없거나 엔티티 자체가 없음을 의미.
    """
    _t0 = time.time()
    _err = None
    _result = None
    try:
        if _find_path is None:
            return {"found": False, "error": "경로 탐색 모듈을 로드할 수 없습니다"}
        graph = _get_falkordb()
        _result = _find_path(graph, start_entity, end_entity, max_hops)
        return _result
    except Exception as e:
        _err = str(e)
        raise
    finally:
        log_mcp_request(
            dept=DEPT_NAME,
            tool="path_search",
            query=f"{start_entity} → {end_entity}",
            result_count=1 if (_result and _result.get("found")) else 0,
            duration_ms=int((time.time() - _t0) * 1000),
            error=_err,
        )


@mcp.tool()
def decision_trace(entity: str, max_depth: int = 4) -> dict[str, Any]:
    """
    특정 엔티티와 관련된 의사결정 이력과 인과 체인을 추적합니다.
    "왜 이런 결정이 내려졌는가?", "어떤 승인 과정을 거쳤는가?"를 알고 싶을 때 사용합니다.

    【이 도구를 사용해야 하는 경우】
    - "~의 승인/결정 과정은 어떻게 되는가?"
    - "~프로세스가 변경된 경위는?"
    - "~팀이 내린 주요 결정들은?"
    - "~의 의사결정 흐름(결정 → 결과 → 다음 결정)을 알고 싶다"
    - Notion 문서에 '승인', '결정', '채택', '합의', '확정' 같은 키워드가 있는 맥락

    【이 도구를 사용하면 안 되는 경우】
    - 단순 관계 탐색 → graph_search 사용
    - 문서 내용 검색 → semantic_search 사용
    - 게임 이벤트 이력 → timeline_search 사용

    【의사결정 데이터 수집 원리】
    Notion 문서 인제스천 시 '승인', '결정', '채택' 등 결정 키워드가 포함된 트리플은
    :Decision 노드로 자동 분류됩니다.
    이전 결정의 결과(outcome)가 다음 결정의 주체(subject)와 연결되면
    LED_TO 엣지로 인과 체인이 자동 구성됩니다.

    【예시 질문】
    - "POTC 운영 정책 변경 결정 과정" → entity="POTC 운영 정책"
    - "운영팀의 주요 결정 이력" → entity="운영팀"
    - "점검 프로세스 개편 경위" → entity="점검 프로세스"
    - "김도형이 관여한 결정들" → entity="김도형"

    Args:
        entity:    의사결정을 추적할 엔티티 이름 (사람·팀·프로세스·정책 등).
                   부분 일치 가능. 예: "운영팀", "점검 프로세스", "POTC"
        max_depth: 인과 체인(LED_TO) 탐색 최대 단계 수 (기본값: 4).
                   결정이 연쇄적으로 이어지는 체인을 몇 단계까지 추적할지 결정.

    Returns:
        {
            entity:        실제 매칭된 엔티티 이름,
            found:         관련 의사결정 존재 여부,
            decisions: [{
                decision_id: 결정 고유 ID,
                subject:     결정 주체 (예: "운영팀"),
                action:      행위/결정 내용 (예: "점검 일정 승인"),
                outcome:     결과 (예: "배포 일정 확정"),
                source_url:  출처 Notion URL,
                ts:          기록 시각,
                leads_to:    이 결정이 이어진 다음 결정 목록,
                led_by:      이 결정을 유발한 이전 결정 목록
            }],
            chain_summary: ["주체 → 행위 → 결과", ...] 형태의 인과 체인 요약
        }
        found=false이면 해당 엔티티 관련 의사결정 기록이 없음을 의미.
    """
    _t0 = time.time()
    _err = None
    _result = None
    try:
        if _trace_decision is None:
            return {
                "entity": entity,
                "found": False,
                "error": "decision_trace 모듈을 로드할 수 없습니다",
            }
        graph = _get_falkordb()
        _result = _trace_decision(graph, entity, max_depth)
        return _result
    except Exception as e:
        _err = str(e)
        raise
    finally:
        log_mcp_request(
            dept=DEPT_NAME,
            tool="decision_trace",
            query=entity,
            result_count=len(_result.get("decisions", [])) if _result else 0,
            duration_ms=int((time.time() - _t0) * 1000),
            error=_err,
        )


@mcp.tool()
def timeline_search(
    game: str = "",
    event_type: str = "",
    from_date: str = "",
    to_date: str = "",
    limit: int = 20,
    keyword: str = "",
) -> dict[str, Any]:
    """
    시계열 이벤트 이력을 날짜 오름차순으로 조회합니다.
    게임/서비스뿐 아니라 부서·조직의 업무 일정도 조회할 수 있습니다.
    업데이트·이벤트·점검·장애·시즌 등 날짜 기반 운영 이력이 필요할 때 사용합니다.

    【이 도구를 사용해야 하는 경우】
    - "~게임의 이벤트 이력을 알려줘"
    - "~게임 마지막 클라이언트 업데이트는 언제인가?"
    - "올해 2분기(Q2)에 진행된 유저 이벤트 목록은?"
    - "~게임 이번 달 점검 일정은?"
    - "~게임의 신규/복귀유저 이벤트 내역"
    - "~서비스에 장애가 발생했던 시점은?"
    - 질문에 게임 이름 + 날짜/기간/분기/이벤트 유형이 포함된 경우

    【부서·조직의 업무 일정 조회】
    - "재무실 업무 일정 알려줘"
      → timeline_search(keyword="재무실")
    - "재무실 6월 일정"
      → timeline_search(keyword="재무실", from_date="2026-06-01", to_date="2026-06-30")
    게임명이 없는 이벤트는 game="기타" 로 저장되므로 부서명을 game 에 넣으면
    조회되지 않습니다. 반드시 keyword 로 전달하세요.

    【이 도구를 사용하면 안 되는 경우】
    - 날짜와 무관한 팀·사람·정책 관계 구조 → graph_search 사용
    - 이벤트 관련 Notion 문서 본문이 필요할 때 → semantic_search 사용
    - 주체·날짜 모두 없는 일반 업무 문서 검색 → hybrid_search 사용
      (hybrid_search 는 날짜 기반 질문이면 이벤트를 자동으로 함께 조회합니다)

    【이벤트 유형(event_type) 선택 기준】
    - client_update   : 클라이언트 패치, 앱 버전 업데이트
    - server_update   : 서버 배포, 백엔드 업데이트
    - user_event      : 신규·복귀·VIP 유저 대상 기간한정 이벤트
    - season          : 시즌 개막·종료
    - content_release : 신규 콘텐츠(던전·캐릭터·맵 등) 오픈
    - maintenance     : 정기 점검, 임시 점검
    - incident        : 장애 발생·복구
    - kpi_milestone   : DAU·매출·가입자 등 KPI 마일스톤 달성
    - (빈 문자열)     : 유형 무관 전체 조회

    【예시 호출】
    - timeline_search(game="POTC")
      → POTC 전체 이벤트 이력
    - timeline_search(game="POTC", event_type="user_event", from_date="2026-01-01")
      → 2026년 이후 POTC 유저 이벤트만
    - timeline_search(game="POTC", from_date="2026-04-01", to_date="2026-06-30")
      → POTC 2분기(Q2) 전체 이벤트
    - timeline_search(game="POTC", event_type="maintenance")
      → POTC 점검 전체 이력

    Args:
        game:       게임/서비스 이름. 부분 일치 가능.
                    예: "POTC", "파이럿" → 모두 POTC 매칭
                    게임이 아닌 주체(부서·조직)라면 비우고 keyword 를 쓰세요.
        event_type: 이벤트 유형 필터. 위 목록 중 하나 또는 빈 문자열(전체).
                    유형을 모르거나 전체가 필요하면 빈 문자열로 두세요.
        from_date:  조회 시작 날짜. YYYY-MM-DD 형식.
                    예: "2026-01-01" / 제한 없으면 빈 문자열.
        to_date:    조회 종료 날짜. YYYY-MM-DD 형식.
                    예: "2026-06-30" / 제한 없으면 빈 문자열.
        limit:      최대 반환 이벤트 수 (기본값: 20).
                    전체 이력을 보고 싶으면 50~100으로 늘리세요.
        keyword:    게임이 아닌 주체를 찾을 때 사용. 이벤트의 제목·설명·
                    카테고리·담당자·주체명에서 부분 일치로 검색합니다.
                    예: keyword="재무실" → 재무실 업무 일정
                    쉼표로 여러 개 지정 가능: "재무실,결산"
                    ※ 게임명이 없는 이벤트는 내부적으로 game="기타" 로
                      저장되므로, 부서명은 game 이 아니라 keyword 로
                      전달해야 조회됩니다.

    ※ game·keyword·날짜가 모두 비어 있으면 전체 이벤트 스캔이 되므로
      조회하지 않고 빈 결과를 반환합니다. 최소 하나는 지정하세요.

    Returns:
        {
            game:   실제 매칭된 게임명,
            found:  이벤트 존재 여부,
            total:  조건에 맞는 전체 이벤트 수,
            events: [{
                event_id:        이벤트 고유 ID,
                game:            게임명,
                event_type:      이벤트 유형,
                category:        Notion "변경카테고리" 원문 (예: "소재변경", "캠페인조정"),
                date:            날짜 (YYYY-MM-DD),
                title:           이벤트 제목,
                description:     상세 설명,
                manager:         담당자,
                target:          대상 유저 (예: "신규유저,복귀유저"),
                source_url:      출처 Notion URL,
                prev_event:      직전 이벤트 요약,
                next_event:      직후 이벤트 요약,
                page_content:    source_url로 연결된 Notion 원문 (벡터 DB, 최대 1500자),
                page_chunk_count: 해당 페이지의 총 청크 수
            }],
            timeline_summary: ["2026-04-12: [client_update] v2.3.1 패치", ...] 형태의 요약 목록
        }
        found=false이면 해당 게임명이 온톨로지에 없거나 조건에 맞는 이벤트가 없음.
    """
    _t0 = time.time()
    _err = None
    _result: dict | None = None
    try:
        if _get_event_chain is None:
            return {
                "game": game or "",
                "found": False,
                "error": "timeline_search 모듈을 로드할 수 없습니다",
            }
        graph = _get_falkordb()

        # keyword: 쉼표 구분 다중 지정 지원 ("재무실,결산")
        kw_list = [k.strip() for k in keyword.split(",") if k.strip()] if keyword else []

        # 동의어 → canonical 정규화: "드래곤슈퍼" → "DS"
        # ingest 시 canonical로 저장되므로 canonical로 조회해야 이벤트가 연결됩니다.
        # 구버전 데이터 호환: canonical로 결과가 없으면 원본 이름으로 재시도합니다.
        game_canonical = _syn_resolve(game) if game else None
        _result = _get_event_chain(
            graph,
            game=game_canonical,
            event_type=event_type or None,
            from_date=from_date or None,
            to_date=to_date or None,
            limit=limit,
            keywords=kw_list or None,
        )
        if (not _result.get("events")) and game and game_canonical != game:
            # 구버전 데이터(비정규 이름으로 저장된 경우) 폴백
            _result = _get_event_chain(
                graph,
                game=game,
                event_type=event_type or None,
                from_date=from_date or None,
                to_date=to_date or None,
                limit=limit,
                keywords=kw_list or None,
            )

        # ── source_url → 벡터 DB 원문 연결 (Explicit Parent Document Retrieval) ──
        # 그래프에서 찾은 :Event 노드의 source_url로 Qdrant를 직접 필터링해
        # 이벤트별 Notion 원문(page_content)을 첨부합니다.
        if _result and _result.get("events"):
            unique_urls = list(
                {ev["source_url"] for ev in _result["events"] if ev.get("source_url")}
            )
            if unique_urls:
                try:
                    qc = _get_qdrant()
                    pages = _fetch_pages_by_source_urls(
                        qc, COLLECTION_NAME, unique_urls, max_chars=1500
                    )
                    for ev in _result["events"]:
                        url = ev.get("source_url", "")
                        if url in pages:
                            ev["page_content"] = pages[url]["content"]
                            ev["page_chunk_count"] = pages[url]["chunk_count"]
                        else:
                            ev["page_content"] = ""
                            ev["page_chunk_count"] = 0
                except Exception:
                    pass  # Qdrant 실패해도 이벤트 목록은 반환

        return _result
    except Exception as e:
        _err = str(e)
        raise
    finally:
        log_mcp_request(
            dept=DEPT_NAME,
            tool="timeline_search",
            query=f"{game} {event_type} {from_date}~{to_date}",
            result_count=(_result.get("total", 0) if _result else 0),
            duration_ms=int((time.time() - _t0) * 1000),
            error=_err,
        )


# ─── 실행 ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="JoyCity Ontology MCP 서버")
    parser.add_argument(
        "--dept",
        default="",
        help="본부 이름 (config/departments.yaml의 key). 미지정 시 legacy 모드",
    )
    parser.add_argument(
        "--transport",
        default="streamable-http",
        choices=["stdio", "streamable-http", "sse"],
        help="전송 방식 (기본: streamable-http)",
    )
    parser.add_argument("--host", default="0.0.0.0", help="호스트 (기본: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8765, help="포트 (기본: 8765)")
    args = parser.parse_args()

    # 비즈니스 용어집 사전 미리 로드 (동의어 해결기 워밍업)
    _syn_preload()

    # 본부 설정 로드 (포트도 departments.yaml 에서 가져올 수 있음)
    if args.dept:
        _load_dept_config(args.dept)
        # departments.yaml 포트 사용 (--port 명시 시 명시값 우선)
        if args.port == 8765:  # 기본값이면 yaml 포트 사용
            try:
                import yaml

                cfg_path = Path(__file__).parent.parent.parent / "config" / "departments.yaml"
                with cfg_path.open(encoding="utf-8") as f:
                    yaml_cfg = yaml.safe_load(f)
                yaml_port = yaml_cfg["departments"][args.dept].get("mcp_port", 8765)
                args.port = yaml_port
            except Exception:
                pass

    if args.transport == "streamable-http":
        print(f"🚀 {DEPT_NAME} Ontology MCP 서버 시작 (Streamable HTTP)")
        print(f"   MCP 주소: http://{args.host}:{args.port}/mcp")
        print(f"   REST API: python src/mcp/rest_api.py --dept {args.dept or ''} (포트 8766)")
        print(
            f"   Claude Code 등록: claude mcp add --transport http {args.dept or 'joycity'}-ontology http://<서버IP>:{args.port}/mcp"
        )
        mcp.run(transport="streamable-http", host=args.host, port=args.port)
    elif args.transport == "sse":
        print(f"🚀 {DEPT_NAME} Ontology MCP 서버 시작 (SSE 레거시)")
        print(f"   주소: http://{args.host}:{args.port}/sse")
        print(
            f"   Claude Code 등록: claude mcp add --transport sse {args.dept or 'joycity'}-ontology http://<서버IP>:{args.port}/sse"
        )
        mcp.run(transport="sse", host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")
