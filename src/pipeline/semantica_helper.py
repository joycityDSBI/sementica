"""
Semantica 프레임워크 통합 헬퍼

세 가지 기능을 제공합니다:

1. merge_node()         — FalkorDB MERGE 기반 엔티티 중복 제거
                          같은 이름+타입의 노드가 이미 존재하면 생성하지 않고 기존 노드 ID 반환

2. extract_with_fallback() — LLM 추출 실패 시 Semantica NER/RE 로 fallback
                              Semantica 미설치 또는 한국어 미지원 시 빈 리스트 반환

3. find_shortest_path() — FalkorDB shortestPath Cypher 로 두 엔티티 간 최단 경로 탐색

의존성:
  pip install semantica[graph-falkordb]   # NER/RE fallback 사용 시
"""

import re
import sys

# ─── Semantica 가용 여부 자동 감지 ──────────────────────────────────────────────
_SEM_AVAILABLE   = False   # Semantica 패키지 설치 여부
_KOREAN_OK       = False   # 한국어 엔티티 인식 가능 여부
_sem_checked     = False   # 한 번만 체크


def _check_semantica_once():
    """최초 1회만 Semantica 설치/한국어 지원 여부를 확인."""
    global _SEM_AVAILABLE, _KOREAN_OK, _sem_checked
    if _sem_checked:
        return
    _sem_checked = True
    try:
        from semantica.semantic_extract import NamedEntityRecognizer
        _SEM_AVAILABLE = True
        # 한국어 테스트 문장
        ner = NamedEntityRecognizer(confidence_threshold=0.4)
        result = ner.extract_entities("김도형 팀장이 운영팀의 점검 프로세스를 담당한다.")
        _KOREAN_OK = len(result) > 0
        status = "한국어 지원 ✅" if _KOREAN_OK else "한국어 미지원 ⚠️ (영문만 가능)"
        print(f"  [Semantica] NER/RE 감지됨 — {status}")
    except ImportError:
        print("  [Semantica] 패키지 없음 — fallback 비활성화 (pip install semantica[graph-falkordb])")
    except Exception as e:
        print(f"  [Semantica] 초기화 실패: {e}")


# ─── 1. 엔티티 중복 제거 (MERGE) ─────────────────────────────────────────────

def merge_node(graph, entity_name: str, entity_type: str, source_url: str) -> int:
    """
    FalkorDB에서 (entity_type {name: entity_name}) 노드를 MERGE 방식으로 생성/조회.

    - 이미 존재하는 노드 → 기존 node_id 반환 (중복 생성 방지)
    - 없으면 신규 생성 후 node_id 반환
    - 실패 시 -1 반환

    사용:
        node_id = merge_node(graph, "운영팀", "Team", "https://notion.so/...")
    """
    # 라벨에 ASCII가 아닌 문자가 포함되면 FalkorDB 오류 → sanitize
    safe_type = re.sub(r"[^A-Za-z0-9_]", "_", entity_type) or "Entity"

    try:
        # MERGE 시도 (FalkorDB 2.x 이상 지원)
        r = graph.query(
            f"MERGE (n:{safe_type} {{name: $name}}) "
            "ON CREATE SET n.source_url = $url "
            "RETURN id(n) AS nid",
            {"name": entity_name, "url": source_url},
        )
        if r.result_set:
            return r.result_set[0][0]
    except Exception:
        pass  # MERGE 미지원 시 MATCH → CREATE 방식으로 폴백

    try:
        # MATCH → 없으면 CREATE (안전한 대안)
        r = graph.query(
            f"MATCH (n:{safe_type} {{name: $name}}) RETURN id(n) AS nid LIMIT 1",
            {"name": entity_name},
        )
        if r.result_set:
            return r.result_set[0][0]

        r = graph.query(
            f"CREATE (n:{safe_type} {{name: $name, source_url: $url}}) RETURN id(n) AS nid",
            {"name": entity_name, "url": source_url},
        )
        return r.result_set[0][0] if r.result_set else -1
    except Exception as e:
        print(f"    ⚠️  노드 MERGE 실패 ({entity_name}/{entity_type}): {e}")
        return -1


# ─── 2. LLM 추출 실패 시 Semantica NER/RE fallback ───────────────────────────

def _semantica_extract(text: str) -> list:
    """
    Semantica NER + RelationExtractor 로 트리플 추출.
    한국어 미지원 시 빈 리스트 반환.
    """
    if not _SEM_AVAILABLE:
        return []

    try:
        from semantica.semantic_extract import NamedEntityRecognizer, RelationExtractor

        ner = NamedEntityRecognizer(confidence_threshold=0.4)
        rel = RelationExtractor(confidence_threshold=0.4)

        entities = ner.extract_entities(text[:3000])
        if not entities:
            return []

        relations = rel.extract_relations(text[:3000], entities=entities)
        triplets  = []
        for r in relations:
            subj = r.get("subject") or r.get("head") or {}
            obj  = r.get("object")  or r.get("tail") or {}
            pred = r.get("predicate") or r.get("relation") or {}

            # 다양한 반환 형식 정규화
            subj_name = subj.get("text") or subj.get("name") or str(subj)
            obj_name  = obj.get("text")  or obj.get("name")  or str(obj)
            pred_name = pred.get("text") or pred.get("name") or str(pred)

            if not subj_name or not obj_name or not pred_name:
                continue

            triplets.append({
                "subject":   {"name": subj_name, "type": subj.get("type", "Entity")},
                "predicate": {"name": pred_name},
                "object":    {"name": obj_name,  "type": obj.get("type",  "Entity")},
            })
        return triplets

    except Exception as e:
        print(f"    ⚠️  Semantica 추출 실패: {e}")
        return []


def extract_with_fallback(llm_extractor_fn, text: str) -> tuple[list, str]:
    """
    LLM 추출 우선 → 실패하거나 빈 결과면 Semantica NER/RE 로 fallback.

    Args:
        llm_extractor_fn: LLM 기반 추출 함수 (text → list)
        text:             추출 대상 텍스트

    Returns:
        (triplets: list, source: str)
        source = "llm" | "semantica" | "empty"

    사용:
        triplets, src = extract_with_fallback(
            lambda t: extract_triplets(llm_client, t), body_text
        )
    """
    _check_semantica_once()

    # 1차: LLM 추출
    try:
        result = llm_extractor_fn(text)
        if result:
            return result, "llm"
    except Exception as e:
        print(f"    ⚠️  LLM 추출 실패: {e}")

    # 2차: Semantica fallback
    if _SEM_AVAILABLE and _KOREAN_OK:
        result = _semantica_extract(text)
        if result:
            print(f"    🔄  Semantica fallback 사용 ({len(result)}개 트리플)")
            return result, "semantica"
    elif _SEM_AVAILABLE and not _KOREAN_OK:
        # 한국어 미지원이지만 영문 혼재 가능성 → 시도
        result = _semantica_extract(text)
        if result:
            print(f"    🔄  Semantica fallback 사용 (영문, {len(result)}개 트리플)")
            return result, "semantica"

    return [], "empty"


# ─── 3. 최단 경로 탐색 ────────────────────────────────────────────────────────

def find_shortest_path(graph, start_name: str, end_name: str, max_hops: int = 6) -> dict:
    """
    FalkorDB shortestPath Cypher 로 두 엔티티 간 최단 연결 경로를 탐색.

    Args:
        graph:      FalkorDB graph 객체
        start_name: 시작 엔티티 이름 (부분 일치)
        end_name:   도착 엔티티 이름 (부분 일치)
        max_hops:   최대 탐색 깊이 (기본 6)

    Returns:
        {
          "found": bool,
          "start": str, "end": str,
          "path_nodes": [str, ...],
          "path_relations": [str, ...],
          "hops": int,
        }
    """
    try:
        # 시작/끝 노드 찾기
        r_start = graph.query(
            "MATCH (n) WHERE n.name CONTAINS $name RETURN n.name AS name LIMIT 1",
            {"name": start_name},
        )
        r_end = graph.query(
            "MATCH (n) WHERE n.name CONTAINS $name RETURN n.name AS name LIMIT 1",
            {"name": end_name},
        )
        if not r_start.result_set or not r_end.result_set:
            return {"found": False, "start": start_name, "end": end_name,
                    "reason": "엔티티를 그래프에서 찾을 수 없음"}

        s_name = r_start.result_set[0][0]
        e_name = r_end.result_set[0][0]

        # shortestPath 탐색
        path_r = graph.query(
            f"MATCH (a {{name: $s}}), (b {{name: $e}}) "
            f"MATCH p = shortestPath((a)-[:REL*1..{max_hops}]-(b)) "
            "RETURN [node IN nodes(p) | node.name]          AS path_nodes, "
            "       [rel  IN relationships(p) | rel.rel_name] AS path_rels",
            {"s": s_name, "e": e_name},
        )

        if not path_r.result_set:
            return {"found": False, "start": s_name, "end": e_name,
                    "reason": f"{max_hops}홉 이내 경로 없음"}

        nodes = path_r.result_set[0][0] or []
        rels  = path_r.result_set[0][1] or []
        return {
            "found":          True,
            "start":          s_name,
            "end":            e_name,
            "path_nodes":     nodes,
            "path_relations": rels,
            "hops":           len(rels),
        }

    except Exception as e:
        return {"found": False, "start": start_name, "end": end_name, "error": str(e)}
