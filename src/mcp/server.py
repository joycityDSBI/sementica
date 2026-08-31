"""
JoyCity Ontology MCP 서버
Claude Code에서 사용할 수 있는 3가지 검색 도구를 제공합니다:

  semantic_search  : Qdrant 벡터 검색 — 의미 기반 유사 페이지 탐색
  graph_search     : FalkorDB 그래프 탐색 — 엔티티 관계 조회
  hybrid_search    : 벡터 + 그래프 결합 — 가장 풍부한 답변

실행:
  # stdio (Claude Code 로컬 연결)
  python src/mcp/server.py

  # SSE (원격 연결, 포트 8765)
  python src/mcp/server.py --transport sse --port 8765

Claude Code 등록:
  claude mcp add --transport sse joycity-ontology http://<서버IP>:8765/sse
"""

import argparse
import json
import os
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


# ─── FastMCP 서버 ─────────────────────────────────────────────────────────────
from fastmcp import FastMCP

mcp = FastMCP(
    name="JoyCity Ontology",
    instructions=(
        "JoyCity Notion 지식 그래프 검색 서버입니다. "
        "담당자, 팀, 프로세스, 정책 등 업무 관련 정보를 검색할 수 있습니다. "
        "semantic_search로 의미 기반 검색, graph_search로 관계 탐색, "
        "hybrid_search로 두 가지를 결합한 검색을 수행하세요."
    ),
)


@mcp.tool()
def semantic_search(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """
    Notion 페이지를 의미 기반(벡터)으로 검색합니다.
    자연어 질문이나 키워드로 관련 페이지를 찾을 때 사용하세요.

    Args:
        query: 검색할 자연어 질문 또는 키워드 (한국어 가능)
        limit: 반환할 최대 결과 수 (기본값: 5)

    Returns:
        관련 페이지 목록 (title, source_url, text_preview, score)
    """
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


@mcp.tool()
def graph_search(entity: str, depth: int = 1) -> dict[str, Any]:
    """
    엔티티(사람, 팀, 프로세스 등)를 중심으로 관련 관계를 그래프에서 탐색합니다.
    "김도형이 소속된 팀", "운영팀이 담당하는 업무" 같은 관계 질문에 사용하세요.

    Args:
        entity: 탐색할 엔티티 이름 (예: "운영팀", "김도형", "점검 시작")
        depth:  탐색 깊이 (1=직접 관계, 2=2홉 관계, 기본값: 1)

    Returns:
        {entity, type, relations: [{relation, target_name, target_type, condition, source_url}]}
    """
    graph = _get_falkordb()

    # 노드 검색 (이름 부분 일치)
    node_query = (
        "MATCH (n) WHERE n.name CONTAINS $name "
        "RETURN n.name AS name, labels(n)[0] AS type LIMIT 5"
    )
    node_result = graph.query(node_query, {"name": entity})

    if not node_result.result_set:
        return {"entity": entity, "found": False, "relations": []}

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

    return {
        "entity":      matched_name,
        "type":        matched_type,
        "found":       True,
        "outgoing":    relations,
        "incoming":    incoming,
    }


@mcp.tool()
def hybrid_search(query: str, limit: int = 5) -> dict[str, Any]:
    """
    벡터 검색 + 그래프 탐색을 결합한 혼합 검색입니다.
    가장 정확하고 풍부한 답변이 필요할 때 사용하세요.
    검색 결과에서 핵심 엔티티를 추출해 그래프 관계까지 함께 반환합니다.

    Args:
        query: 검색할 자연어 질문 (한국어 가능)
        limit: 벡터 검색 결과 수 (기본값: 5)

    Returns:
        {semantic_results, graph_results, entity_summary}
    """
    # 1. 벡터 검색
    semantic = semantic_search(query, limit=limit)

    # 2. 쿼리에서 핵심 명사를 추출해 그래프 탐색
    #    (간단히: 쿼리의 2글자 이상 단어 중 명사 후보를 탐색)
    graph = _get_falkordb()
    words = [w for w in query.split() if len(w) >= 2]

    graph_hits = []
    seen_entities = set()
    for word in words[:3]:   # 최대 3개 단어로 그래프 탐색
        node_q = (
            "MATCH (n) WHERE n.name CONTAINS $name "
            "RETURN n.name AS name, labels(n)[0] AS type LIMIT 3"
        )
        nodes = graph.query(node_q, {"name": word})
        for row in nodes.result_set:
            entity_name = row[0]
            if entity_name in seen_entities:
                continue
            seen_entities.add(entity_name)
            g = graph_search(entity_name, depth=1)
            if g.get("found"):
                graph_hits.append(g)

    # 3. 결합 요약
    entity_summary = []
    for g in graph_hits:
        if g.get("outgoing"):
            for rel in g["outgoing"][:3]:
                entity_summary.append(
                    f"{g['entity']} → {rel['relation']} → {rel['target_name']}"
                )

    return {
        "semantic_results": semantic,
        "graph_results":    graph_hits,
        "entity_summary":   entity_summary,
    }


# ─── 실행 ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="JoyCity Ontology MCP 서버")
    parser.add_argument("--dept",      default="",
                        help="본부 이름 (config/departments.yaml의 key). 미지정 시 legacy 모드")
    parser.add_argument("--transport", default="sse", choices=["stdio", "sse"],
                        help="전송 방식 (기본: sse)")
    parser.add_argument("--host",      default="0.0.0.0", help="SSE 호스트 (기본: 0.0.0.0)")
    parser.add_argument("--port",      type=int, default=8765, help="SSE 포트 (기본: 8765)")
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

    if args.transport == "sse":
        print(f"🚀 {DEPT_NAME} Ontology MCP 서버 시작 (SSE)")
        print(f"   주소: http://{args.host}:{args.port}/sse")
        print(f"   Claude Code 등록: claude mcp add --transport sse {args.dept or 'joycity'}-ontology http://<서버IP>:{args.port}/sse")
        mcp.run(transport="sse", host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")
