"""
Semantica 프레임워크 통합 헬퍼

여덟 가지 기능을 제공합니다:

1. merge_node()            — FalkorDB MERGE 기반 엔티티 중복 제거
                             같은 이름+타입의 노드가 이미 존재하면 생성하지 않고 기존 노드 ID 반환

2. extract_with_fallback() — LLM 추출 실패 시 Semantica NER/RE 로 fallback
                             Semantica 미설치 또는 한국어 미지원 시 빈 리스트 반환

3. find_shortest_path()    — FalkorDB shortestPath Cypher 로 두 엔티티 간 최단 경로 탐색

4. is_decision_triplet()   — 트리플이 의사결정에 해당하는지 판단 (한국어 결정 키워드 기반)

5. record_decision_node()  — FalkorDB에 :Decision 노드 기록 + 인과 연결 (LED_TO 엣지)

6. trace_decision_chain()  — 엔티티 이름으로 관련 의사결정 체인 탐색

7. upsert_event_node()     — FalkorDB에 :Event 노드 MERGE 기록
                             :Game 노드 자동 연결 (HAD_EVENT 엣지)
                             시간 순서대로 FOLLOWED_BY 엣지 자동 생성

8. get_event_chain()       — 게임/서비스의 시계열 이벤트 이력 조회
                             날짜 범위 필터, 이벤트 유형 필터 지원

의존성:
  pip install semantica[graph-falkordb]   # NER/RE fallback 사용 시
"""

import contextlib
import hashlib
import re
import uuid
from datetime import UTC, datetime

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

# 의미있는 엔티티 타입만 허용 (날짜·숫자·컬럼명 제외)
_VALID_NER_TYPES: frozenset = frozenset({
    "PERSON", "ORG", "PRODUCT", "FAC", "WORK_OF_ART", "EVENT", "NORP",
    "Entity",          # Semantica 기본 타입
    "Team", "System", "Process", "Policy", "Document", "Role",  # 커스텀 온톨로지 타입
})
_SKIP_NER_TYPES: frozenset = frozenset({
    "DATE", "TIME", "CARDINAL", "ORDINAL", "PERCENT", "MONEY", "QUANTITY",
    "LOC", "GPE",      # 지명·국가는 업무 온톨로지에서 불필요
})
# 날짜/숫자 패턴 엔티티 이름 제외
_DATE_NUM_RE = re.compile(
    r"^\d+$"                                      # 순수 숫자
    r"|^\d{4}[-/.년]\d{1,2}"                      # YYYY-MM, YYYY년MM
    r"|\d{1,2}시\s*\d{0,2}분?"                    # 시각 (오전 5시 15분)
    r"|^20\d{2}"                                   # 연도 단독 (2026 등)
)


def _is_valid_entity(name: str, etype: str) -> bool:
    """노이즈 엔티티 필터: 날짜·숫자·빈 문자열·너무 짧은 이름 제외."""
    name = name.strip()
    if not name or len(name) < 2:
        return False
    if etype in _SKIP_NER_TYPES:
        return False
    return not _DATE_NUM_RE.search(name)


def _semantica_extract(text: str) -> list:
    """
    Semantica NER + RelationExtractor 로 트리플 추출.
    날짜·숫자·컬럼명 타입 엔티티는 필터링하여 노이즈 최소화.
    한국어 미지원 시 빈 리스트 반환.
    """
    if not _SEM_AVAILABLE:
        return []

    def _attr(obj, *keys):
        """dict 또는 Relation 객체에서 값 추출 (여러 키 시도)."""
        for key in keys:
            val = obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)
            if val is not None:
                return val
        return {}

    def _text(obj) -> str:
        """Entity/Span/dict/str 어느 형태든 텍스트 추출."""
        if not obj:
            return ""
        if isinstance(obj, str):
            return obj
        if isinstance(obj, dict):
            return obj.get("text") or obj.get("name") or ""
        return getattr(obj, "text", None) or getattr(obj, "name", None) or str(obj)

    def _etype(obj) -> str:
        """엔티티 타입 추출 (dict / 객체 모두 처리)."""
        if isinstance(obj, dict):
            return obj.get("type", "Entity") or "Entity"
        return getattr(obj, "type", None) or getattr(obj, "label_", None) or "Entity"

    try:
        from semantica.semantic_extract import NamedEntityRecognizer, RelationExtractor

        # 한국어 처리 시 영문 모델 오분류 빈도 높음 → 신뢰도 임계값 상향
        ner = NamedEntityRecognizer(confidence_threshold=0.6)
        rel = RelationExtractor(confidence_threshold=0.6)

        entities = ner.extract_entities(text[:3000])
        if not entities:
            return []

        relations = rel.extract_relations(text[:3000], entities=entities)
        triplets  = []
        for r in relations:
            subj_raw = _attr(r, "subject",   "head")
            obj_raw  = _attr(r, "object",    "tail")
            pred_raw = _attr(r, "predicate", "relation")

            subj_name = _text(subj_raw)
            obj_name  = _text(obj_raw)
            pred_name = _text(pred_raw)
            subj_type = _etype(subj_raw)
            obj_type  = _etype(obj_raw)

            if not subj_name or not obj_name or not pred_name:
                continue

            # 노이즈 엔티티 제거: 날짜·숫자·너무 짧은 이름
            if not _is_valid_entity(subj_name, subj_type):
                continue
            if not _is_valid_entity(obj_name, obj_type):
                continue

            triplets.append({
                "subject":   {"name": subj_name, "type": subj_type},
                "predicate": {"name": pred_name},
                "object":    {"name": obj_name,  "type": obj_type},
            })

        before = len(relations) if hasattr(relations, "__len__") else "?"
        print(f"    [Semantica] 관계 {before}개 → 필터 후 {len(triplets)}개 트리플")
        return triplets

    except Exception as e:
        print(f"    ⚠️  Semantica 추출 실패: {e}")
        return []


def extract_with_fallback(llm_extractor_fn, text: str) -> tuple[list, str]:
    """
    LLM 기반 트리플 추출. 실패하거나 빈 결과면 빈 리스트 반환.

    Semantica NER/RE fallback 을 사용하지 않는 이유:
      - RelationExtractor 가 의미 기반이 아닌 거리/의존성 기반으로 동작
      - 한국어 업무 문서에서 모든 엔티티 쌍에 관계를 생성 → 노이즈 과다
      - LLM 이 0개를 반환하는 것은 "추출할 관계가 없다"는 정확한 판단
      - 노이즈 엣지가 graph_search 결과 품질을 저하

    Args:
        llm_extractor_fn: LLM 기반 추출 함수 (text → list)
        text:             추출 대상 텍스트

    Returns:
        (triplets: list, source: str)
        source = "llm" | "empty"
    """
    # LLM 추출
    try:
        result = llm_extractor_fn(text)
        if result:
            return result, "llm"
    except Exception as e:
        print(f"    ⚠️  LLM 추출 실패: {e}")

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


# ─── 4-6. 의사결정 추적 (trace_decision_chain) ───────────────────────────────

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
    ts = datetime.now(UTC).isoformat()

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
    with contextlib.suppress(Exception):
        graph.query(
            "MATCH (prev:Decision) WHERE prev.outcome = $subject "
            "  AND prev.decision_id <> $did "
            "MATCH (d:Decision {decision_id: $did}) "
            "MERGE (prev)-[:LED_TO]->(d)",
            {"subject": subj_name, "did": did},
        )

    # ── 인과 연결: 이 결정의 outcome이 이후 결정의 subject와 같으면 LED_TO 생성
    with contextlib.suppress(Exception):
        graph.query(
            "MATCH (next:Decision) WHERE next.subject = $outcome "
            "  AND next.decision_id <> $did "
            "MATCH (d:Decision {decision_id: $did}) "
            "MERGE (d)-[:LED_TO]->(next)",
            {"outcome": obj_name, "did": did},
        )

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


# ─── 7. 이벤트 노드 (upsert_event_node) ─────────────────────────────────────

EVENT_TYPES: frozenset = frozenset([
    "client_update", "server_update", "user_event",
    "season", "content_release", "maintenance", "incident", "kpi_milestone",
    # UA 마케팅 이벤트 (ingest.py / sync.py EVENT_EXTRACT_PROMPT와 동기화)
    "ua_budget", "ua_creative", "ua_channel", "ua_targeting", "ua_abtest",
    # UA 변경 이력 (Notion DB '변경카테고리' 값)
    "ua_campaign",
])

# ─── DB 속성 키 별칭 ─────────────────────────────────────────────────────────
# Notion DB 컬럼명은 자유롭게 설정되므로, 소문자 비교(case-insensitive)로 처리한다.
# ingest.py / sync.py 에서 공통으로 사용하는 단일 정의.
DB_DATE_KEYS    = {
    "이벤트날짜", "날짜", "일자", "date", "event_date",
    "시작일", "시작날짜", "변경일", "적용일",
}
DB_GAME_KEYS    = {
    "게임명", "게임", "game", "product", "서비스명", "서비스",
    "project",          # RESU UA 히스토리 등 영문 PROJECT 컬럼 지원
}
DB_TYPE_KEYS    = {
    "이벤트유형", "유형", "event_type", "type", "종류",
    "변경카테고리", "카테고리", "category", "change_type", "변경유형",
}
DB_MANAGER_KEYS = {
    "담당자", "담당팀", "manager", "owner", "담당",
    "생성자", "작성자", "creator",   # Notion DB 생성자·작성자 컬럼 지원
}

# 변경카테고리 원문 → EVENT_TYPES 정규값 매핑
# 매핑에 없는 값은 그대로 event_type 으로 사용 (EVENT_TYPES 에 없으면 ua_campaign 으로 폴백)
_CATEGORY_TO_EVENT_TYPE: dict[str, str] = {
    "캠페인조정":  "ua_campaign",
    "캠페인 조정": "ua_campaign",
    "소재변경":    "ua_creative",
    "소재 변경":   "ua_creative",
    "예산변경":    "ua_budget",
    "예산 변경":   "ua_budget",
    "국가변경":    "ua_targeting",
    "국가 변경":   "ua_targeting",
    "타겟변경":    "ua_targeting",
    "타겟 변경":   "ua_targeting",
    "채널변경":    "ua_channel",
    "채널 변경":   "ua_channel",
    "ab테스트":    "ua_abtest",
    "a/b테스트":   "ua_abtest",
}


def event_from_db_props(db_props: dict, source_url: str, title: str) -> dict | None:
    """
    Notion DB 속성 딕셔너리에서 :Event 노드 dict를 생성합니다.
    LLM 없이 100% 정확하게 처리됩니다.

    조건:
        날짜 필드가 있어야 Event로 변환합니다 (게임명이 없으면 "기타"로 처리).

    지원 컬럼명:
        날짜   : 날짜, date, 이벤트날짜, 변경일, 적용일 …
        게임/프로젝트: 게임명, game, PROJECT, product …
        유형   : 이벤트유형, 변경카테고리, category …
        담당자 : 담당자, 생성자, creator …

    컬럼명 매칭은 대소문자 무관(case-insensitive)으로 처리됩니다.
    """
    # 소문자 키 매핑으로 case-insensitive 비교
    lower = {k.lower(): v for k, v in db_props.items()}

    def _first(keys: set) -> str | None:
        for k in keys:
            v = lower.get(k.lower())
            if v is not None:
                return ", ".join(str(x) for x in v) if isinstance(v, list) else str(v)
        return None

    date = _first(DB_DATE_KEYS)
    if not date:
        return None  # 날짜 없으면 이벤트 아님

    game     = _first(DB_GAME_KEYS) or "기타"
    raw_type = _first(DB_TYPE_KEYS) or ""
    # 변경카테고리 → EVENT_TYPES 정규값 변환
    event_type = _CATEGORY_TO_EVENT_TYPE.get(raw_type.strip(), raw_type.strip())
    if event_type not in EVENT_TYPES:
        event_type = "ua_campaign" if raw_type else "user_event"

    manager_raw = _first(DB_MANAGER_KEYS) or ""

    return {
        "game":        game,
        "event_type":  event_type,
        "date":        date[:10],
        "title":       title,
        "description": "",
        "manager":     manager_raw,
        "source_url":  source_url,
    }


def _date_to_ts(date_str: str) -> int:
    """ISO 8601 날짜 문자열 → Unix timestamp (UTC 기준). 실패 시 0 반환."""
    from datetime import datetime
    for fmt in ("%Y-%m-%d", "%y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            dt = datetime.strptime(date_str.strip(), fmt).replace(tzinfo=UTC)
            return int(dt.timestamp())
        except ValueError:
            continue
    return 0


def _month_to_quarter(month: int) -> str:
    return f"Q{(month - 1) // 3 + 1}"


def upsert_event_node(graph, event: dict) -> int:
    """
    :Event 노드를 FalkorDB에 MERGE 방식으로 생성/갱신.

    event 딕셔너리 필수 키:
        game        — 게임/서비스 이름 (예: "POTC")
        date        — 날짜 문자열 (YYYY-MM-DD)
        title       — 이벤트 제목

    선택 키:
        event_type  — 이벤트 유형 (EVENT_TYPES 중 하나, 기본 "user_event")
        description — 이벤트 상세 설명
        target      — 대상 유저 세그먼트 (콤마 구분 문자열)
        manager     — 담당자/팀 이름 (기존 Person/Team 노드와 연결)
        source_url  — 출처 URL

    부수 효과:
        - :Game 노드 MERGE (없으면 자동 생성)
        - (Game)-[:HAD_EVENT]->(Event) 엣지 생성
        - 같은 게임의 이전/이후 이벤트와 FOLLOWED_BY 엣지 자동 연결

    Returns:
        FalkorDB node id (실패 시 -1)
    """
    from datetime import datetime

    game        = str(event.get("game",        "")).strip()
    event_type  = str(event.get("event_type",  "")).strip()
    date        = str(event.get("date",        "")).strip()
    title       = str(event.get("title",       "")).strip()
    description = str(event.get("description", ""))
    target      = str(event.get("target",      ""))
    manager     = str(event.get("manager",     ""))
    source_url  = str(event.get("source_url",  ""))

    if not (game and date and title):
        return -1

    # event_type 정규화
    if event_type not in EVENT_TYPES:
        event_type = "user_event"

    # 날짜 파싱
    date_ts = _date_to_ts(date)
    if date_ts == 0:
        return -1

    try:
        dt      = datetime.fromtimestamp(date_ts, tz=UTC)
        year    = dt.year
        month   = dt.month
        quarter = _month_to_quarter(month)
    except Exception:
        year, month, quarter = 0, 0, ""

    # 안정적 ID: game | event_type | date
    event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{game}|{event_type}|{date}"))
    ts       = datetime.now(UTC).isoformat()

    # ── 1. :Event 노드 MERGE ────────────────────────────────────────────────
    try:
        r = graph.query(
            "MERGE (e:Event {event_id: $eid}) "
            "ON CREATE SET "
            "  e.game = $game, e.event_type = $etype, e.date = $date, "
            "  e.date_ts = $date_ts, e.year = $year, e.month = $month, "
            "  e.quarter = $quarter, e.title = $title, "
            "  e.description = $desc, e.target = $target, "
            "  e.source_url = $url, e.ts = $ts "
            "RETURN id(e) AS nid",
            {
                "eid":     event_id,  "game":    game,    "etype":   event_type,
                "date":    date,      "date_ts": date_ts, "year":    year,
                "month":   month,     "quarter": quarter, "title":   title,
                "desc":    description, "target": target,
                "url":     source_url,  "ts":     ts,
            },
        )
        if not r.result_set:
            return -1
        event_node_id = r.result_set[0][0]
    except Exception as e:
        print(f"    ⚠️  Event 노드 생성 실패 ({game}/{date}/{title}): {e}")
        return -1

    # ── 2. :Game 노드 MERGE + HAD_EVENT 엣지 ───────────────────────────────
    try:
        graph.query(
            "MERGE (g:Game {name: $name}) ON CREATE SET g.source_url = $url "
            "RETURN id(g)",
            {"name": game, "url": source_url},
        )
        graph.query(
            "MATCH (g:Game {name: $game}), (e:Event {event_id: $eid}) "
            "MERGE (g)-[:HAD_EVENT {date: $date}]->(e)",
            {"game": game, "eid": event_id, "date": date},
        )
    except Exception:
        pass

    # ── 3. 담당자/팀 MANAGED_BY 엣지 ───────────────────────────────────────
    if manager:
        try:
            mgr_r = graph.query(
                "MATCH (m) WHERE m.name = $name RETURN id(m) LIMIT 1",
                {"name": manager},
            )
            if mgr_r.result_set:
                graph.query(
                    "MATCH (m {name: $mgr}), (e:Event {event_id: $eid}) "
                    "MERGE (e)-[:MANAGED_BY]->(m)",
                    {"mgr": manager, "eid": event_id},
                )
        except Exception:
            pass

    # ── 4. FOLLOWED_BY 자동 연결 (같은 게임, 날짜 순서) ─────────────────────
    try:
        # 직전 이벤트
        prev_r = graph.query(
            "MATCH (e:Event) WHERE e.game = $game AND e.date_ts < $ts "
            "RETURN e.event_id, e.date_ts ORDER BY e.date_ts DESC LIMIT 1",
            {"game": game, "ts": date_ts},
        )
        if prev_r.result_set:
            prev_eid  = prev_r.result_set[0][0]
            prev_ts_v = prev_r.result_set[0][1]
            days_diff = round((date_ts - prev_ts_v) / 86400)
            graph.query(
                "MATCH (p:Event {event_id: $p}), (c:Event {event_id: $c}) "
                "MERGE (p)-[:FOLLOWED_BY {days_diff: $dd}]->(c)",
                {"p": prev_eid, "c": event_id, "dd": days_diff},
            )

        # 직후 이벤트
        next_r = graph.query(
            "MATCH (e:Event) WHERE e.game = $game AND e.date_ts > $ts "
            "RETURN e.event_id, e.date_ts ORDER BY e.date_ts ASC LIMIT 1",
            {"game": game, "ts": date_ts},
        )
        if next_r.result_set:
            next_eid  = next_r.result_set[0][0]
            next_ts_v = next_r.result_set[0][1]
            days_diff = round((next_ts_v - date_ts) / 86400)
            graph.query(
                "MATCH (c:Event {event_id: $c}), (n:Event {event_id: $n}) "
                "MERGE (c)-[:FOLLOWED_BY {days_diff: $dd}]->(n)",
                {"c": event_id, "n": next_eid, "dd": days_diff},
            )
    except Exception:
        pass  # FOLLOWED_BY 실패는 치명적이지 않음

    return event_node_id


# ─── 8. 이벤트 체인 조회 (get_event_chain) ──────────────────────────────────

def get_event_chain(
    graph,
    game: str,
    event_type: str | None = None,
    from_date:  str | None = None,
    to_date:    str | None = None,
    limit: int = 20,
) -> dict:
    """
    게임/서비스의 시계열 이벤트를 날짜순으로 조회.

    Args:
        graph:      FalkorDB graph 객체
        game:       게임/서비스 이름 (부분 일치)
        event_type: 필터링할 이벤트 유형 (None이면 전체)
        from_date:  시작 날짜 YYYY-MM-DD (None이면 제한 없음)
        to_date:    종료 날짜 YYYY-MM-DD (None이면 제한 없음)
        limit:      최대 반환 개수 (기본 20)

    Returns:
        {
          "game": str,
          "found": bool,
          "total": int,
          "events": [
            {
              "event_id", "game", "event_type", "date", "title",
              "description", "target", "source_url",
              "prev_event": {"title", "date"} | None,
              "next_event": {"title", "date"} | None,
            }, ...
          ],
          "timeline_summary": ["2026-04-12: [client_update] 클라이언트 업데이트", ...]
        }
    """
    from_ts = _date_to_ts(from_date) if from_date else 0
    to_ts   = _date_to_ts(to_date)   if to_date   else 9_999_999_999

    try:
        # 게임명 부분 일치로 실제 이름 확인
        game_r = graph.query(
            "MATCH (g:Game) WHERE g.name CONTAINS $name RETURN g.name LIMIT 1",
            {"name": game},
        )
        # 게임 노드가 없으면 event.game 필드에서 직접 탐색
        if game_r.result_set:
            actual_game = game_r.result_set[0][0]
        else:
            ev_r = graph.query(
                "MATCH (e:Event) WHERE e.game CONTAINS $name RETURN e.game LIMIT 1",
                {"name": game},
            )
            actual_game = ev_r.result_set[0][0] if ev_r.result_set else game

        # 이벤트 유형 필터 조건 분기 (FalkorDB IS NULL 파라미터 미지원 대응)
        if event_type:
            type_clause = "AND e.event_type = $etype "
            params = {"game": actual_game, "etype": event_type, "from_ts": from_ts, "to_ts": to_ts}
        else:
            type_clause = ""
            params = {"game": actual_game, "from_ts": from_ts, "to_ts": to_ts}

        # OPTIONAL MATCH 으로 prev/next 를 단일 쿼리에서 조회 (이벤트당 2회 N+1 제거)
        cypher = (
            "MATCH (e:Event) "
            "WHERE e.game = $game "
            f"  {type_clause}"
            "  AND e.date_ts >= $from_ts AND e.date_ts <= $to_ts "
            "OPTIONAL MATCH (prev:Event)-[:FOLLOWED_BY]->(e) "
            "OPTIONAL MATCH (e)-[:FOLLOWED_BY]->(nxt:Event) "
            "RETURN e.event_id, e.game, e.event_type, e.date, "
            "       e.title, e.description, e.target, e.source_url, "
            "       prev.title AS prev_title, prev.date AS prev_date, "
            "       nxt.title AS next_title, nxt.date AS next_date "
            f"ORDER BY e.date_ts ASC LIMIT {int(limit)}"
        )
        r = graph.query(cypher, params)

        if not r.result_set:
            return {
                "game": actual_game, "found": False,
                "total": 0, "events": [], "timeline_summary": [],
            }

        # row: [event_id, game, event_type, date, title, description,
        #       target, source_url, prev_title, prev_date, next_title, next_date]
        events = [
            {
                "event_id":    row[0],
                "game":        row[1],
                "event_type":  row[2],
                "date":        row[3],
                "title":       row[4],
                "description": row[5] or "",
                "target":      row[6] or "",
                "source_url":  row[7] or "",
                "prev_event":  {"title": row[8],  "date": row[9]}  if row[8]  else None,
                "next_event":  {"title": row[10], "date": row[11]} if row[10] else None,
            }
            for row in r.result_set
        ]

        timeline_summary = [
            f"{e['date']}: [{e['event_type']}] {e['title']}"
            for e in events
        ]

        return {
            "game":             actual_game,
            "found":            True,
            "total":            len(events),
            "events":           events,
            "timeline_summary": timeline_summary,
        }

    except Exception as ex:
        return {
            "game":             game,
            "found":            False,
            "total":            0,
            "events":           [],
            "timeline_summary": [],
            "error":            str(ex),
        }


# ─── 9. 경로 분류 (classify_page) ────────────────────────────────────────────
#
# 페이지마다 LLM 추출 전에 호출해 처리 경로를 결정합니다.
#   core     → 지식 추출 대상 (LLM 트리플·이벤트 파이프라인 전체 실행)
#   defer    → 벡터 임베딩만, LLM 추출 건너뜀 (정보 밀도 낮음)
#   excluded → 인제스천 완전 제외 (너무 짧거나 임시 문서)
#
# 결정론적 규칙 기반 — AI 호출 없음, 처리 비용 0.

_CORE_TITLE_KEYWORDS: frozenset = frozenset([
    "전략", "기획", "의사결정", "결정", "승인", "회의록", "회의", "미팅",
    "ua", "마케팅", "예산", "목표", "kpi", "매출", "dau", "arpu",
    "분석", "인사이트", "보고서", "계획", "방향", "로드맵", "okr",
    "이슈", "리스크", "문제", "개선", "제안", "검토", "결론",
])
_CORE_BODY_KEYWORDS: frozenset = frozenset([
    "의사결정", "결정함", "승인됨", "예산", "목표", "kpi", "전략",
    "ua", "마케팅", "매출", "인사이트", "이슈", "리스크",
])
_DEFER_TITLE_SIGNALS: frozenset = frozenset([
    "링크 모음", "참고 자료", "자료 모음", "업무 연락", "단순 안내",
    "일정 공유", "todo", "체크리스트",
])
_EXCLUDED_TITLE_PATTERNS: frozenset = frozenset([
    "테스트", "test", "임시", "draft", "삭제 예정", "미사용", "untitled",
])


def classify_page(body: str, meta: dict, word_count: int) -> str:
    """
    페이지를 'core' / 'defer' / 'excluded' 중 하나로 분류합니다.
    결정론적 규칙 기반 — AI 호출 없음.

    Args:
        body:       페이지 본문 텍스트
        meta:       페이지 메타 딕셔너리 (title, db_properties 등)
        word_count: 본문 단어 수

    Returns:
        "core"     — LLM 트리플·이벤트 추출까지 전체 파이프라인 실행
        "defer"    — 벡터 임베딩만, LLM 추출 건너뜀
        "excluded" — 인제스천 완전 제외 (word_count < 30 포함)
    """
    title = (meta.get("title") or "").lower()

    # 1. Notion DB 아이템 우선 처리 — 구조화 속성이 있으면 word_count 기준 완화
    # DB row는 속성값을 합성한 body를 쓰므로 단어 수가 적어도 의미 있는 데이터.
    # 속성이 하나라도 있으면 core로 처리 (5단어 미만만 제외).
    if meta.get("db_properties"):
        if word_count < 5:
            return "excluded"
        return "core"

    # 2. 완전 제외 조건 (일반 페이지)
    if word_count < 30:
        return "excluded"
    for pat in _EXCLUDED_TITLE_PATTERNS:
        if pat in title:
            return "excluded"

    # 3. 제목 키워드 우선 판정
    for kw in _CORE_TITLE_KEYWORDS:
        if kw in title:
            return "core"
    for sig in _DEFER_TITLE_SIGNALS:
        if sig in title:
            return "defer"

    # 4. 본문 길이 기반 — 짧은 문서는 defer (LLM 대비 효용 낮음)
    if word_count < 80:
        return "defer"

    # 5. 본문 앞 500자 키워드 확인
    body_preview = body[:500].lower()
    for kw in _CORE_BODY_KEYWORDS:
        if kw in body_preview:
            return "core"

    # 6. 기본값 — 충분히 길면 core (나중에 재분류 가능)
    return "core"


# ─── 10. 본문 해시 (content_hash) ────────────────────────────────────────────

def content_hash(text: str) -> str:
    """
    텍스트의 SHA-256 해시 앞 16자를 반환합니다.
    sync.py가 이전 처리 결과와 비교해 내용 무변경 페이지를 건너뛸 때 사용합니다.

    Returns:
        16자 소문자 hex 문자열 (예: "a3f9e2c1b4d7e0f8")
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ─── 11. 실현 상태 판정 (detect_realization_status) ──────────────────────────

_PLANNED_SIGNALS: frozenset = frozenset([
    "예정", "할 예정", "진행 예정", "검토 중", "검토 예정", "계획",
    "예정입니다", "할 계획", "가능성", "논의 중", "준비 중",
    "예정으로", "검토하고", "진행할", "배포 예정", "오픈 예정",
])
_APPLIED_SIGNALS: frozenset = frozenset([
    "완료", "적용됨", "배포됨", "출시", "오픈됨", "시행됨", "확정됨",
    "실시됨", "반영됨", "실행됨", "시작됨", "완료되었", "됩니다",
    "했습니다", "출시됨", "배포 완료", "오픈 완료", "적용 완료",
])


def find_evidence_chunk_id(
    evidence_quote: str,
    chunks: "list[str]",
    source_url: str,
) -> "str | None":
    """
    evidence_quote가 포함된 Qdrant 청크의 UUID를 반환합니다.

    ingest / sync 시 FalkorDB 엣지에 `evidence_chunk_id`를 기록하면,
    MCP graph_search가 해당 청크를 직접 Qdrant retrieve로 조회할 수 있습니다.
    (벡터 유사도 검색보다 훨씬 빠름 — O(1) ID 조회)

    Args:
        evidence_quote: LLM이 추출한 원문 인용 문구
        chunks:         store_vector에서 분할한 청크 리스트
        source_url:     Notion 페이지 URL (UUID 네임스페이스로 사용)

    Returns:
        청크 UUID 문자열 (str) or None (매칭 청크 없음)
    """
    if not evidence_quote or not chunks:
        return None
    # 앞 40자로 검색 (LLM이 원문을 그대로 인용했다면 충분)
    key = evidence_quote[:40].lower().strip()
    if not key:
        return None
    for i, chunk in enumerate(chunks):
        if key in chunk.lower():
            return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source_url}#chunk{i}"))
    return None


def detect_realization_status(evidence_text: str) -> str:
    """
    트리플의 근거 문구(evidence_quote)에서 계획/실현 신호어를 감지해
    실현 상태를 반환합니다.

    Args:
        evidence_text: LLM이 선택한 원문 인용 문구

    Returns:
        "planned"     — 계획·예정 신호어 감지됨
        "applied"     — 완료·적용 신호어 감지됨
        "unconfirmed" — 신호어 없거나 혼재
    """
    t = evidence_text.lower()
    has_planned = any(sig in t for sig in _PLANNED_SIGNALS)
    has_applied = any(sig in t for sig in _APPLIED_SIGNALS)

    if has_applied and not has_planned:
        return "applied"
    if has_planned and not has_applied:
        return "planned"
    return "unconfirmed"  # 혼재하거나 신호 없음
