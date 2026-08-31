"""
Semantica 프레임워크 통합 헬퍼

다섯 가지 기능을 제공합니다:

1. merge_node()            — FalkorDB MERGE 기반 엔티티 중복 제거
                             같은 이름+타입의 노드가 이미 존재하면 생성하지 않고 기존 노드 ID 반환

2. extract_with_fallback() — LLM 추출 실패 시 Semantica NER/RE 로 fallback
                             Semantica 미설치 또는 한국어 미지원 시 빈 리스트 반환

3. find_shortest_path()    — FalkorDB shortestPath Cypher 로 두 엔티티 간 최단 경로 탐색

4. is_decision_triplet()   — 트리플이 의사결정에 해당하는지 판단 (한국어 결정 키워드 기반)

5. record_decision_node()  — FalkorDB에 :Decision 노드 기록 + 인과 연결 (LED_TO 엣지)

6. trace_decision_chain()  — 엔티티 이름으로 관련 의사결정 체인 탐색

의존성:
  pip install semantica[graph-falkordb]   # NER/RE fallback 사용 시
"""

import re
import sys
import uuid
from datetime import datetime, timezone

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


# ─── 4‑6. 의사결정 추적 (trace_decision_chain) ───────────────────────────────

# 의사결정을 나타내는 한국어 술어 키워드
DECISION_KEYWORDS: frozenset = frozenset([
    "승인", "결정", "채택", "선택", "완료", "확정", "검토",
    "허가", "처리", "배정", "지정", "선정", "의결", "보고",
    "승낙", "거부", "반려", "취소", "변경", "수정", "합의",
    "위임", "지시", "요청", "승계", "이관",
])


def is_decision_triplet(triplet: dict) -> bool:
    """
    트리플의 술어(predicate)가 의사결정에 해당하는지 확인.

    Args:
        triplet: {"subject": ..., "predicate": {"name": "승인"}, "object": ...}

    Returns:
        True if predicate.name contains any DECISION_KEYWORDS
    """
    pred_name = ""
    pred = triplet.get("predicate")
    if isinstance(pred, dict):
        pred_name = pred.get("name", "")
    elif isinstance(pred, str):
        pred_name = pred
    return any(kw in pred_name for kw in DECISION_KEYWORDS)


def record_decision_node(graph, triplet: dict, source_url: str) -> int:
    """
    의사결정 트리플을 FalkorDB의 :Decision 노드로 기록하고,
    인과 관계(LED_TO 엣지)를 자동 생성.

    :Decision 노드 속성:
        decision_id  — uuid5 기반 안정적 식별자 (중복 방지)
        subject      — 결정 주체 (누가)
        action       — 결정 행위 (승인, 지시 등)
        outcome      — 결정 결과/대상 (무엇을)
        source_url   — 출처 Notion 페이지 URL
        ts           — 기록 시각 (ISO 8601)

    인과 연결 (LED_TO):
        기존 Decision에서 outcome == 이 노드의 subject  → (기존)─[LED_TO]→(이 노드)
        기존 Decision에서 subject == 이 노드의 outcome  → (이 노드)─[LED_TO]→(기존)

    Returns:
        FalkorDB node id (실패 시 -1)
    """
    subj  = triplet.get("subject",   {})
    pred  = triplet.get("predicate", {})
    obj   = triplet.get("object",    {})

    subj_name = subj.get("name", "") if isinstance(subj, dict) else str(subj)
    pred_name = pred.get("name", "") if isinstance(pred, dict) else str(pred)
    obj_name  = obj.get("name",  "") if isinstance(obj,  dict) else str(obj)

    if not subj_name or not pred_name or not obj_name:
        return -1

    # 안정적 ID: 출처 + 트리플 내용 기반 uuid5
    did = str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"{source_url}|{subj_name}|{pred_name}|{obj_name}",
    ))
    ts = datetime.now(timezone.utc).isoformat()

    try:
        r = graph.query(
            "MERGE (d:Decision {decision_id: $did}) "
            "ON CREATE SET d.subject = $subject, d.action = $action, "
            "              d.outcome = $outcome, d.source_url = $url, d.ts = $ts "
            "RETURN id(d) AS nid",
            {"did": did, "subject": subj_name, "action": pred_name,
             "outcome": obj_name, "url": source_url, "ts": ts},
        )
        if not r.result_set:
            return -1
        node_id = r.result_set[0][0]
    except Exception as e:
        print(f"    ⚠️  Decision 노드 기록 실패 ({subj_name}/{pred_name}): {e}")
        return -1

    # ── 인과 연결: 이전 결정의 outcome이 이 결정의 subject와 같으면 LED_TO 생성
    try:
        graph.query(
            "MATCH (prev:Decision) WHERE prev.outcome = $subject "
            "  AND prev.decision_id <> $did "
            "MATCH (d:Decision {decision_id: $did}) "
            "MERGE (prev)-[:LED_TO]->(d)",
            {"subject": subj_name, "did": did},
        )
    except Exception:
        pass  # LED_TO 생성 실패는 치명적이지 않음

    # ── 인과 연결: 이 결정의 outcome이 이후 결정의 subject와 같으면 LED_TO 생성
    try:
        graph.query(
            "MATCH (next:Decision) WHERE next.subject = $outcome "
            "  AND next.decision_id <> $did "
            "MATCH (d:Decision {decision_id: $did}) "
            "MERGE (d)-[:LED_TO]->(next)",
            {"outcome": obj_name, "did": did},
        )
    except Exception:
        pass

    return node_id


def trace_decision_chain(graph, entity_name: str, max_depth: int = 4) -> dict:
    """
    엔티티 이름으로 관련 의사결정 체인을 탐색.

    1. entity_name이 subject 또는 outcome에 포함된 :Decision 노드를 조회
    2. 각 Decision에서 LED_TO 엣지를 따라 상하류 인과 체인을 구성
    3. 시계열 순서(ts)로 정렬해 반환

    Args:
        graph:       FalkorDB graph 객체
        entity_name: 탐색할 엔티티 이름 (부분 일치)
        max_depth:   LED_TO 탐색 최대 깊이 (기본 4)

    Returns:
        {
          "entity":   str,
          "found":    bool,
          "decisions": [
            {
              "decision_id": str,
              "subject":     str,
              "action":      str,
              "outcome":     str,
              "source_url":  str,
              "ts":          str,
              "leads_to":    [{"subject", "action", "outcome", "ts"}, ...],  # 하류 결정
              "led_by":      [{"subject", "action", "outcome", "ts"}, ...],  # 상류 결정
            },
            ...
          ],
          "chain_summary": [str, ...],   # "주체 → 행위 → 결과" 텍스트 목록 (시계열)
        }
    """
    try:
        # 1. 관련 Decision 노드 조회
        r = graph.query(
            "MATCH (d:Decision) "
            "WHERE d.subject CONTAINS $name OR d.outcome CONTAINS $name "
            "RETURN d.decision_id AS did, d.subject AS subject, "
            "       d.action AS action, d.outcome AS outcome, "
            "       d.source_url AS url, d.ts AS ts "
            "ORDER BY d.ts ASC LIMIT 30",
            {"name": entity_name},
        )

        if not r.result_set:
            return {
                "entity":       entity_name,
                "found":        False,
                "decisions":    [],
                "chain_summary": [],
            }

        # 2. 각 Decision의 상·하류 연결 조회
        decisions = []
        for row in r.result_set:
            did, subj, action, outcome, url, ts = row[0], row[1], row[2], row[3], row[4], row[5]

            # 하류: 이 결정이 이어지는 결정들 (LED_TO 순방향)
            down_r = graph.query(
                f"MATCH (d:Decision {{decision_id: $did}})"
                f"-[:LED_TO*1..{max_depth}]->(next:Decision) "
                "RETURN next.subject, next.action, next.outcome, next.ts "
                "ORDER BY next.ts ASC LIMIT 10",
                {"did": did},
            )
            leads_to = [
                {"subject": dr[0], "action": dr[1], "outcome": dr[2], "ts": dr[3]}
                for dr in (down_r.result_set or [])
            ]

            # 상류: 이 결정을 유발한 결정들 (LED_TO 역방향)
            up_r = graph.query(
                f"MATCH (prev:Decision)-[:LED_TO*1..{max_depth}]->"
                f"(d:Decision {{decision_id: $did}}) "
                "RETURN prev.subject, prev.action, prev.outcome, prev.ts "
                "ORDER BY prev.ts ASC LIMIT 10",
                {"did": did},
            )
            led_by = [
                {"subject": ur[0], "action": ur[1], "outcome": ur[2], "ts": ur[3]}
                for ur in (up_r.result_set or [])
            ]

            decisions.append({
                "decision_id": did,
                "subject":     subj,
                "action":      action,
                "outcome":     outcome,
                "source_url":  url or "",
                "ts":          ts or "",
                "leads_to":    leads_to,
                "led_by":      led_by,
            })

        # 3. 체인 요약 (시계열 순)
        chain_summary = [
            f"{d['subject']} → {d['action']} → {d['outcome']}"
            for d in decisions
        ]

        return {
            "entity":        entity_name,
            "found":         True,
            "decisions":     decisions,
            "chain_summary": chain_summary,
        }

    except Exception as e:
        return {
            "entity":        entity_name,
            "found":         False,
            "decisions":     [],
            "chain_summary": [],
            "error":         str(e),
        }
