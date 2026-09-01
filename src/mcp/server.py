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
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

# ─── .env 로드 ────────────────────────────────────────────────────────────────
_env_path = Path(__file__).parent.parent.parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# ─── 설정 ─────────────────────────────────────────────────────────────────────
GCP_PROJECT     = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
LOCATION        = os.environ.get("VERTEX_AI_LOCATION", "us-east5")
EMBED_MODEL     = "text-multilingual-embedding-002"
QDRANT_URL      = os.environ.get("QDRANT_URL", "http://localhost:6333")
FALKORDB_HOST   = os.environ.get("FALKORDB_HOST", "localhost")
FALKORDB_PORT   = int(os.environ.get("FALKORDB_PORT", "6379"))

# 기본값 (--dept 없을 때 / legacy)
COLLECTION_NAME = "joycity_pages"
GRAPH_NAME      = "joycity_kg"
DEPT_NAME       = "JoyCity"


def _load_dept_config(dept: str):
    """본부 설정 로드 후 전역 변수 덮어쓰기"""
    global COLLECTION_NAME, GRAPH_NAME, DEPT_NAME
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent / "pipeline"))
        from dept_config import load_dept
        cfg = load_dept(dept)
        COLLECTION_NAME = cfg["qdrant_collection"]
        GRAPH_NAME      = cfg["falkordb_graph"]
        DEPT_NAME       = cfg["name"]
        print(f"  본부: {DEPT_NAME} ({dept})")
        print(f"  컬렉션: {COLLECTION_NAME}  그래프: {GRAPH_NAME}")
    except Exception as e:
        print(f"  ⚠️  본부 설정 로드 실패: {e} → legacy 모드 사용")

# ─── 클라이언트 (지연 초기화) ─────────────────────────────────────────────────
_embed_client  = None
_qdrant        = None
_falkordb      = None


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


# ─── 복합 쿼리 분해 헬퍼 ─────────────────────────────────────────────────────

_COMPLEX_PATTERNS = frozenset([
    "이고", "이며", "하는", "이면서", "이자",
    "담당하는", "작성한", "소속된", "승인한", "결정한",
    "관련된", "연관된", "포함된", "연결된",
])


def _is_complex_query(query: str) -> bool:
    """복합 쿼리 여부 휴리스틱 탐지 (15자+ AND 복합 패턴 OR 6단어+)"""
    if len(query) >= 15 and any(p in query for p in _COMPLEX_PATTERNS):
        return True
    if len(query.split()) >= 6:
        return True
    return False


def _decompose_query(query: str) -> list[str]:
    """Claude Haiku로 복합 쿼리를 독립적 서브쿼리 2~3개로 분해"""
    try:
        import anthropic
        import re as _re
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": (
                    "다음 복합 질문을 독립적으로 검색 가능한 서브쿼리 2~3개로 분해하세요.\n"
                    "JSON 배열만 반환하세요. 예: [\"서브쿼리1\", \"서브쿼리2\"]\n\n"
                    f"질문: {query}"
                ),
            }],
        )
        text = msg.content[0].text.strip()
        m = _re.search(r'\[.*?\]', text, _re.DOTALL)
        if m:
            import json as _json
            parts = _json.loads(m.group())
            parts = [p.strip() for p in parts if isinstance(p, str) and p.strip()]
            if 2 <= len(parts) <= 4:
                return parts
    except Exception:
        pass
    return [query]   # 분해 실패 시 원본 유지


def _run_sub_search(sub_query: str, limit: int) -> tuple[list, list]:
    """서브쿼리 단위 벡터+그래프 검색 (내부 헬퍼)"""
    # ─ 벡터 검색 ─
    sem: list = []
    try:
        vec = _embed(sub_query)
        qc = _get_qdrant()
        result = qc.query_points(
            collection_name=COLLECTION_NAME,
            query=vec,
            limit=limit,
            with_payload=True,
        )
        for h in result.points:
            p = h.payload or {}
            sem.append({
                "title":        p.get("title", ""),
                "source_url":   p.get("source_url", ""),
                "text_preview": p.get("text", "")[:300],
                "score":        round(h.score, 4),
            })
    except Exception:
        pass

    # ─ 그래프 검색 ─
    gph: list = []
    try:
        graph = _get_falkordb()
        words = [w for w in sub_query.split() if len(w) >= 2]
        seen: set = set()
        for word in words[:2]:
            nodes = graph.query(
                "MATCH (n) WHERE n.name CONTAINS $name RETURN n.name LIMIT 2",
                {"name": word},
            )
            for row in nodes.result_set:
                entity_name = row[0]
                if entity_name in seen:
                    continue
                seen.add(entity_name)
                g = graph_search(entity_name, depth=1)
                if g.get("found"):
                    gph.append(g)
    except Exception:
        pass

    return sem, gph


def _merge_semantic_results(results_per_query: list) -> list:
    """서브쿼리별 벡터 결과 URL 중복 제거 + coverage 가중 재랭킹

    여러 서브쿼리에서 공통으로 등장하는 문서일수록 높은 점수를 부여합니다.
    부스트 공식: score * (1 + 0.15 * (coverage - 1))
    """
    url_counts: dict = {}
    url_best: dict = {}

    for results in results_per_query:
        for r in results:
            url = r.get("source_url", "")
            if url not in url_counts:
                url_counts[url] = 0
                url_best[url] = r.copy()
            url_counts[url] += 1
            if r["score"] > url_best[url]["score"]:
                url_best[url] = r.copy()

    merged = []
    for url, item in url_best.items():
        coverage = url_counts[url]
        merged.append({
            **item,
            "coverage": coverage,
            "score": round(item["score"] * (1 + 0.15 * (coverage - 1)), 4),
        })

    return sorted(merged, key=lambda x: x["score"], reverse=True)


# ─── DB 로거 ──────────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
try:
    from db_logger import log_mcp_request
except Exception:
    def log_mcp_request(*a, **kw): pass   # DB 없을 때 no-op

# ─── Semantica 헬퍼 (경로 탐색) ───────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent / "pipeline"))
try:
    from semantica_helper import find_shortest_path as _find_path
    from semantica_helper import trace_decision_chain as _trace_decision
    from semantica_helper import get_event_chain as _get_event_chain
    from semantica_helper import upsert_event_node as _upsert_event_node
except Exception:
    _find_path          = None
    _trace_decision     = None
    _get_event_chain    = None
    _upsert_event_node  = None

# ─── FastMCP 서버 ─────────────────────────────────────────────────────────────
from fastmcp import FastMCP

mcp = FastMCP(
    name="JoyCity Ontology",
    instructions=(
        "JoyCity 전략사업본부 Notion 기반 지식 그래프 검색 서버입니다.\n"
        "\n"
        "【도구 선택 기준】\n"
        "1. semantic_search  — '~에 대한 문서/정책/절차를 알려줘' 처럼 Notion 문서 내용이 필요할 때\n"
        "2. graph_search     — '~팀 담당 업무는?', '~가 속한 팀은?' 처럼 사람·팀·프로세스 간 관계가 필요할 때\n"
        "3. hybrid_search    — 문서 내용 + 관계 정보를 동시에 필요로 하는 복합 질문이거나, "
        "어떤 도구를 써야 할지 불분명할 때 (가장 포괄적)\n"
        "4. path_search      — '~와 ~ 사이에 어떤 관계/연결고리가 있는가?' 처럼 두 엔티티의 연결 경로가 필요할 때\n"
        "5. decision_trace   — '~의 승인/결정 과정은?', '왜 이런 결정을 내렸는가?' 처럼 의사결정 경위가 필요할 때\n"
        "6. timeline_search  — '~게임의 이벤트 이력은?', '지난 분기 업데이트 목록은?' 처럼 날짜 기반 이벤트 이력이 필요할 때\n"
        "\n"
        "【공통 원칙】\n"
        "- 한 번의 도구 호출로 답이 불충분하면 다른 도구를 추가로 호출하세요.\n"
        "- 질문에 게임명 + 날짜가 포함되면 timeline_search를 우선 고려하세요.\n"
        "- 질문에 사람 이름 또는 팀 이름이 포함되면 graph_search를 우선 고려하세요.\n"
        "- 확신이 없으면 hybrid_search를 사용하세요."
    ),
)


@mcp.tool()
def semantic_search(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """
    Notion 문서를 의미 기반(벡터 유사도)으로 검색합니다.
    키워드가 정확히 일치하지 않아도 의미가 유사한 문서를 찾아줍니다.

    【이 도구를 사용해야 하는 경우】
    - 특정 주제·정책·절차에 관한 Notion 문서 내용이 필요할 때
    - "~에 대해 설명해줘", "~관련 문서 찾아줘", "~절차는 어떻게 돼?" 같은 질문
    - 정확한 담당자나 팀 이름을 모르고 문서 내용으로 탐색할 때

    【이 도구를 사용하면 안 되는 경우】
    - 사람·팀·프로세스 간 관계가 궁금할 때 → graph_search 사용
    - 두 엔티티의 연결 경로가 궁금할 때 → path_search 사용
    - 게임 이벤트 날짜·이력이 궁금할 때 → timeline_search 사용
    - 의사결정 경위·승인 과정이 궁금할 때 → decision_trace 사용

    【예시 질문】
    - "POTC 서버 점검 절차는?"
    - "신규 유저 이벤트 기획 가이드"
    - "글로벌 서비스 운영 정책 문서"
    - "정기 업데이트 배포 프로세스"

    Args:
        query: 검색할 자연어 질문 또는 키워드 (한국어 가능).
               구체적일수록 정확도가 높아집니다.
               예: "POTC 운영팀 점검 공지 절차" (좋음) vs "점검" (너무 짧음)
        limit: 반환할 최대 결과 수. 기본값 5.
               확신이 없으면 10으로 늘려 더 많은 후보를 확인하세요.

    Returns:
        list of {
            title:        Notion 페이지 제목,
            source_url:   Notion 원본 URL,
            text_preview: 관련 본문 앞 300자,
            score:        유사도 점수 (0~1, 높을수록 관련성 높음)
        }
    """
    _t0 = time.time()
    _err = None
    try:
        vec = _embed(query)
        qc = _get_qdrant()
        result = qc.query_points(
            collection_name=COLLECTION_NAME,
            query=vec,
            limit=limit,
            with_payload=True,
        )
        results = []
        for h in result.points:
            p = h.payload or {}
            results.append({
                "title":        p.get("title", ""),
                "source_url":   p.get("source_url", ""),
                "text_preview": p.get("text", "")[:300],
                "score":        round(h.score, 4),
            })
        return results
    except Exception as e:
        _err = str(e)
        raise
    finally:
        log_mcp_request(
            dept=DEPT_NAME, tool="semantic_search", query=query,
            result_count=len(results) if _err is None else 0,
            duration_ms=int((time.time() - _t0) * 1000), error=_err,
        )


@mcp.tool()
def graph_search(entity: str, depth: int = 1) -> dict[str, Any]:  # noqa: C901
    """
    특정 엔티티(사람·팀·프로세스·시스템 등)를 중심으로 연결된 관계를 그래프에서 탐색합니다.
    문서 내용이 아닌 '구조적 관계'(누가 무엇을 담당하는지, 어떤 팀에 속하는지 등)를 파악할 때 사용합니다.

    【이 도구를 사용해야 하는 경우】
    - "~팀이 담당하는 업무/게임은?"
    - "~가 소속된 팀은?", "~의 담당자는 누구인가?"
    - "~프로세스에 참여하는 팀/사람은?"
    - "~시스템을 운영하는 주체는?"
    - 특정 이름(사람·팀·프로세스)이 질문에 명시되어 있고, 그 관계망을 펼쳐보고 싶을 때

    【이 도구를 사용하면 안 되는 경우】
    - 문서 내용(정책·절차 본문)이 필요할 때 → semantic_search 사용
    - 두 특정 엔티티 간 경로를 찾고 싶을 때 → path_search 사용 (graph_search는 단일 엔티티 중심)
    - 게임 이벤트 이력이 필요할 때 → timeline_search 사용

    【예시 질문】
    - "운영팀 담당 업무" → entity="운영팀"
    - "김도형 소속 팀과 역할" → entity="김도형"
    - "POTC 관련 팀과 담당자" → entity="POTC"
    - "점검 프로세스에 관련된 팀" → entity="점검 프로세스"

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
            outgoing: [{relation, target_name, target_type, condition, order, source_url}]
                      이 엔티티에서 나가는 관계 목록,
            incoming: [{relation, source_name, source_type, source_url}]
                      이 엔티티로 들어오는 관계 목록 (누가 이 엔티티와 관계를 맺는지)
        }
    """
    _t0 = time.time()
    _err = None
    _result = None
    try:
        graph = _get_falkordb()

        # 노드 검색 (이름 부분 일치)
        node_query = (
            "MATCH (n) WHERE n.name CONTAINS $name "
            "RETURN n.name AS name, labels(n)[0] AS type LIMIT 5"
        )
        node_result = graph.query(node_query, {"name": entity})

        if not node_result.result_set:
            _result = {"entity": entity, "found": False, "relations": []}
            return _result

        # 첫 번째 매칭 노드 기준으로 관계 탐색
        matched_name = node_result.result_set[0][0]
        matched_type = node_result.result_set[0][1]

        if depth == 1:
            rel_query = (
                "MATCH (n {name: $name})-[r:REL]->(m) "
                "RETURN r.rel_name AS relation, m.name AS target, labels(m)[0] AS target_type, "
                "r.condition AS condition, r.order AS order, r.source_url AS source_url "
                "LIMIT 20"
            )
        else:
            rel_query = (
                "MATCH (n {name: $name})-[r:REL*1..2]->(m) "
                "RETURN [rel in r | rel.rel_name] AS relations, m.name AS target, "
                "labels(m)[0] AS target_type, r[-1].source_url AS source_url "
                "LIMIT 30"
            )

        rel_result = graph.query(rel_query, {"name": matched_name})

        relations = []
        for row in rel_result.result_set:
            rel = {
                "relation":    row[0],
                "target_name": row[1],
                "target_type": row[2],
            }
            if len(row) > 3 and row[3]:
                rel["condition"] = row[3]
            if len(row) > 4 and row[4]:
                rel["order"] = row[4]
            if len(row) > 5 and row[5]:
                rel["source_url"] = row[5]
            relations.append(rel)

        # 역방향 관계도 탐색 (누가 이 엔티티와 관계를 맺는지)
        rev_query = (
            "MATCH (m)-[r:REL]->(n {name: $name}) "
            "RETURN r.rel_name AS relation, m.name AS source, labels(m)[0] AS source_type, "
            "r.source_url AS source_url "
            "LIMIT 10"
        )
        rev_result = graph.query(rev_query, {"name": matched_name})
        incoming = []
        for row in rev_result.result_set:
            incoming.append({
                "relation":    row[0],
                "source_name": row[1],
                "source_type": row[2],
                "source_url":  row[3] if len(row) > 3 else "",
            })

        _result = {
            "entity":      matched_name,
            "type":        matched_type,
            "found":       True,
            "outgoing":    relations,
            "incoming":    incoming,
        }
        return _result
    except Exception as e:
        _err = str(e)
        raise
    finally:
        log_mcp_request(
            dept=DEPT_NAME, tool="graph_search", query=entity,
            result_count=len(_result.get("outgoing", [])) if _result else 0,
            duration_ms=int((time.time() - _t0) * 1000), error=_err,
        )


@mcp.tool()
def hybrid_search(query: str, limit: int = 5) -> dict[str, Any]:
    """
    벡터 검색(문서 내용) + 그래프 탐색(관계 구조)을 동시에 수행하는 통합 검색입니다.
    복합 질문을 자동으로 서브쿼리로 분해하여 각각 검색한 뒤 결과를 병합합니다.

    【이 도구를 사용해야 하는 경우】
    - 문서 내용과 관계 정보를 동시에 알아야 할 때
    - 어떤 도구를 써야 할지 판단이 어려울 때 (가장 포괄적인 도구)
    - 질문이 복합적일 때: "~팀에서 ~업무를 담당하는 사람이 작성한 문서는?"
    - 처음 탐색을 시작할 때 전반적인 맥락을 파악하고 싶을 때

    【이 도구를 사용하면 안 되는 경우】
    - 게임 이벤트 날짜·이력만 필요할 때 → timeline_search 사용 (hybrid_search는 Event 노드 미포함)
    - 두 엔티티 간 정확한 연결 경로가 필요할 때 → path_search 사용
    - 의사결정 승인 과정만 필요할 때 → decision_trace 사용

    【복합 쿼리 자동 분해 동작】
    질문이 길거나 복합 조건이 있으면 Claude Haiku가 자동으로 2~3개 서브쿼리로 분해합니다.
    예: "운영팀에서 POTC 점검을 담당하는 사람이 작성한 배포 가이드는?"
    → ["운영팀 POTC 점검 담당자", "POTC 점검 배포 가이드", "운영팀 작성 문서"]
    각 서브쿼리 결과에서 공통으로 등장하는 문서에 가중치를 부여해 재랭킹합니다.

    【예시 질문】
    - "POTC 운영을 담당하는 팀과 관련 정책 문서"
    - "전략사업본부 신규유저 이벤트 담당자와 기획 가이드"
    - "글로벌 서비스 점검 프로세스와 담당팀"
    - "게임 배포 승인 절차와 관련 팀 구성"

    Args:
        query: 검색할 자연어 질문 (한국어 가능). 복합 질문도 그대로 입력하세요.
               AI가 자동으로 서브쿼리로 분해합니다.
        limit: 벡터 검색 결과 수 (기본값: 5). 복합 질문이면 8~10 권장.

    Returns:
        {
            semantic_results: 벡터 검색 결과 목록 [{title, source_url, text_preview, score, coverage}],
            graph_results:    그래프 탐색 결과 목록 [{entity, type, outgoing, incoming}],
            entity_summary:   관계 요약 문자열 목록 ["엔티티A → 관계 → 엔티티B", ...],
            decomposed:       복합 쿼리 분해 여부 (true/false),
            sub_queries:      분해된 서브쿼리 목록 (decomposed=true일 때만)
        }
    """
    _t0 = time.time()
    _err = None
    _result = None
    try:
        # ── 1. 복합 쿼리 감지 및 서브쿼리 분해 ─────────────────────────────
        sub_queries = [query]
        decomposed = False
        if _is_complex_query(query):
            sub_queries = _decompose_query(query)
            decomposed = len(sub_queries) > 1

        # ── 2. 서브쿼리별 벡터+그래프 검색 ────────────────────────────────
        sem_per_q: list[list] = []
        all_graph_hits: list = []

        for sq in sub_queries:
            sem, gph = _run_sub_search(sq, limit=limit)
            sem_per_q.append(sem)
            all_graph_hits.extend(gph)

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
        entity_summary: list[str] = []
        for g in graph_hits:
            for rel in (g.get("outgoing") or [])[:3]:
                entity_summary.append(
                    f"{g['entity']} → {rel['relation']} → {rel['target_name']}"
                )

        _result = {
            "semantic_results": semantic,
            "graph_results":    graph_hits,
            "entity_summary":   entity_summary,
            "decomposed":       decomposed,
            "sub_queries":      sub_queries if decomposed else [],
        }
        return _result
    except Exception as e:
        _err = str(e)
        raise
    finally:
        log_mcp_request(
            dept=DEPT_NAME, tool="hybrid_search", query=query,
            result_count=len(_result.get("semantic_results", [])) if _result else 0,
            duration_ms=int((time.time() - _t0) * 1000), error=_err,
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
            dept=DEPT_NAME, tool="path_search",
            query=f"{start_entity} → {end_entity}",
            result_count=1 if (_result and _result.get("found")) else 0,
            duration_ms=int((time.time() - _t0) * 1000), error=_err,
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
            return {"entity": entity, "found": False,
                    "error": "decision_trace 모듈을 로드할 수 없습니다"}
        graph = _get_falkordb()
        _result = _trace_decision(graph, entity, max_depth)
        return _result
    except Exception as e:
        _err = str(e)
        raise
    finally:
        log_mcp_request(
            dept=DEPT_NAME, tool="decision_trace", query=entity,
            result_count=len(_result.get("decisions", [])) if _result else 0,
            duration_ms=int((time.time() - _t0) * 1000), error=_err,
        )


@mcp.tool()
def timeline_search(
    game: str,
    event_type: str = "",
    from_date:  str = "",
    to_date:    str = "",
    limit: int = 20,
) -> dict[str, Any]:
    """
    게임/서비스의 시계열 이벤트 이력을 날짜 오름차순으로 조회합니다.
    업데이트·이벤트·점검·장애·시즌 등 날짜 기반 운영 이력이 필요할 때 사용합니다.

    【이 도구를 사용해야 하는 경우】
    - "~게임의 이벤트 이력을 알려줘"
    - "~게임 마지막 클라이언트 업데이트는 언제인가?"
    - "올해 2분기(Q2)에 진행된 유저 이벤트 목록은?"
    - "~게임 이번 달 점검 일정은?"
    - "~게임의 신규/복귀유저 이벤트 내역"
    - "~서비스에 장애가 발생했던 시점은?"
    - 질문에 게임 이름 + 날짜/기간/분기/이벤트 유형이 포함된 경우

    【이 도구를 사용하면 안 되는 경우】
    - 게임 이벤트가 아닌 팀·사람·정책 관계 → graph_search 사용
    - 이벤트 관련 Notion 문서 본문이 필요할 때 → semantic_search 사용
    - 게임명 없이 일반 업무 문서 검색 → semantic_search 또는 hybrid_search 사용

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
        event_type: 이벤트 유형 필터. 위 목록 중 하나 또는 빈 문자열(전체).
                    유형을 모르거나 전체가 필요하면 빈 문자열로 두세요.
        from_date:  조회 시작 날짜. YYYY-MM-DD 형식.
                    예: "2026-01-01" / 제한 없으면 빈 문자열.
        to_date:    조회 종료 날짜. YYYY-MM-DD 형식.
                    예: "2026-06-30" / 제한 없으면 빈 문자열.
        limit:      최대 반환 이벤트 수 (기본값: 20).
                    전체 이력을 보고 싶으면 50~100으로 늘리세요.

    Returns:
        {
            game:   실제 매칭된 게임명,
            found:  이벤트 존재 여부,
            total:  조건에 맞는 전체 이벤트 수,
            events: [{
                event_id:    이벤트 고유 ID,
                game:        게임명,
                event_type:  이벤트 유형,
                date:        날짜 (YYYY-MM-DD),
                title:       이벤트 제목,
                description: 상세 설명,
                target:      대상 유저 (예: "신규유저,복귀유저"),
                source_url:  출처 Notion URL,
                prev_event:  직전 이벤트 요약,
                next_event:  직후 이벤트 요약
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
            return {"game": game, "found": False, "error": "timeline_search 모듈을 로드할 수 없습니다"}
        graph = _get_falkordb()
        _result = _get_event_chain(
            graph,
            game       = game,
            event_type = event_type or None,
            from_date  = from_date  or None,
            to_date    = to_date    or None,
            limit      = limit,
        )
        return _result
    except Exception as e:
        _err = str(e)
        raise
    finally:
        log_mcp_request(
            dept=DEPT_NAME, tool="timeline_search",
            query=f"{game} {event_type} {from_date}~{to_date}",
            result_count=(_result.get("total", 0) if _result else 0),
            duration_ms=int((time.time() - _t0) * 1000), error=_err,
        )


# ─── 실행 ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="JoyCity Ontology MCP 서버")
    parser.add_argument("--dept",      default="",
                        help="본부 이름 (config/departments.yaml의 key). 미지정 시 legacy 모드")
    parser.add_argument("--transport", default="streamable-http",
                        choices=["stdio", "streamable-http", "sse"],
                        help="전송 방식 (기본: streamable-http)")
    parser.add_argument("--host",      default="0.0.0.0", help="호스트 (기본: 0.0.0.0)")
    parser.add_argument("--port",      type=int, default=8765, help="포트 (기본: 8765)")
    args = parser.parse_args()

    # 본부 설정 로드 (포트도 departments.yaml 에서 가져올 수 있음)
    if args.dept:
        _load_dept_config(args.dept)
        # departments.yaml 포트 사용 (--port 명시 시 명시값 우선)
        if args.port == 8765:   # 기본값이면 yaml 포트 사용
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
        print(f"   주소: http://{args.host}:{args.port}/mcp")
        print(f"   Claude Code 등록: claude mcp add --transport http {args.dept or 'joycity'}-ontology http://<서버IP>:{args.port}/mcp")
        mcp.run(transport="streamable-http", host=args.host, port=args.port)
    elif args.transport == "sse":
        print(f"🚀 {DEPT_NAME} Ontology MCP 서버 시작 (SSE 레거시)")
        print(f"   주소: http://{args.host}:{args.port}/sse")
        print(f"   Claude Code 등록: claude mcp add --transport sse {args.dept or 'joycity'}-ontology http://<서버IP>:{args.port}/sse")
        mcp.run(transport="sse", host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")
