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
except Exception:
    _find_path = None
    _trace_decision = None

# ─── FastMCP 서버 ─────────────────────────────────────────────────────────────
from fastmcp import FastMCP

mcp = FastMCP(
    name="JoyCity Ontology",
    instructions=(
        "JoyCity Notion 지식 그래프 검색 서버입니다. "
        "담당자, 팀, 프로세스, 정책 등 업무 관련 정보를 검색할 수 있습니다. "
        "semantic_search로 의미 기반 검색, graph_search로 관계 탐색, "
        "hybrid_search로 두 가지를 결합한 검색을 수행하세요. "
        "path_search로 두 엔티티 간 최단 경로를, "
        "decision_trace로 의사결정 인과 체인을 탐색할 수 있습니다."
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
    엔티티(사람, 팀, 프로세스 등)를 중심으로 관련 관계를 그래프에서 탐색합니다.
    "김도형이 소속된 팀", "운영팀이 담당하는 업무" 같은 관계 질문에 사용하세요.

    Args:
        entity: 탐색할 엔티티 이름 (예: "운영팀", "김도형", "점검 시작")
        depth:  탐색 깊이 (1=직접 관계, 2=2홉 관계, 기본값: 1)

    Returns:
        {entity, type, relations: [{relation, target_name, target_type, condition, source_url}]}
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
    벡터 검색 + 그래프 탐색을 결합한 혼합 검색입니다.
    가장 정확하고 풍부한 답변이 필요할 때 사용하세요.
    검색 결과에서 핵심 엔티티를 추출해 그래프 관계까지 함께 반환합니다.

    Args:
        query: 검색할 자연어 질문 (한국어 가능)
        limit: 벡터 검색 결과 수 (기본값: 5)

    Returns:
        {semantic_results, graph_results, entity_summary}
    """
    _t0 = time.time()
    _err = None
    _result = None
    try:
        # 1. 벡터 검색
        semantic = semantic_search(query, limit=limit)

        # 2. 쿼리에서 핵심 명사를 추출해 그래프 탐색
        graph = _get_falkordb()
        words = [w for w in query.split() if len(w) >= 2]

        graph_hits = []
        seen_entities = set()
        for word in words[:3]:
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

        _result = {
            "semantic_results": semantic,
            "graph_results":    graph_hits,
            "entity_summary":   entity_summary,
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
    두 엔티티(사람, 팀, 프로세스 등) 사이의 최단 연결 경로를 탐색합니다.
    "A와 B는 어떤 관계인가?", "A에서 B까지 이어지는 경로는?" 같은 질문에 사용하세요.

    Args:
        start_entity: 시작 엔티티 이름 (예: "운영팀")
        end_entity:   도착 엔티티 이름 (예: "점검 완료")
        max_hops:     최대 탐색 깊이 (기본값: 6)

    Returns:
        {found, start, end, path_nodes, path_relations, hops}
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
    특정 엔티티(사람, 팀, 프로세스 등)와 관련된 의사결정 체인을 추적합니다.
    "A 프로세스 승인 경위는?", "운영팀이 내린 결정들은?" 같은 질문에 사용하세요.

    의사결정 체인 탐색 원리:
      - 인제스천/동기화 시점에 '승인', '결정', '채택' 등 결정 키워드가 포함된 트리플은
        :Decision 노드로 별도 기록됩니다.
      - 이전 결정의 결과(outcome)가 다음 결정의 주체(subject)와 같으면
        LED_TO 엣지로 인과 관계가 자동 연결됩니다.

    Args:
        entity:    탐색할 엔티티 이름 (예: "운영팀", "점검 프로세스", "김도형")
        max_depth: LED_TO 인과 체인 탐색 최대 깊이 (기본값: 4)

    Returns:
        {
          entity, found,
          decisions: [{decision_id, subject, action, outcome, source_url, ts,
                       leads_to: [...], led_by: [...]}],
          chain_summary: ["주체 → 행위 → 결과", ...]
        }
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
