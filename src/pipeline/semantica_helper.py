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

8. get_event_chain()       — 시계열 이벤트 이력 조회
                             게임/서비스 또는 부서·조직(keywords) 기준,
                             날짜 범위·이벤트 유형 필터 지원

의존성:
  pip install semantica[graph-falkordb]   # NER/RE fallback 사용 시
"""

import contextlib
import hashlib
import os
import re
import threading
import uuid
from datetime import UTC, datetime

# ─── 동의어 해결기 (비즈니스 용어집 API) ────────────────────────────────────────
try:
    from utils.synonym_resolver import (
        resolve as _resolve_entity,
        resolve_in as _resolve_in_category,
    )
except ImportError:

    def _resolve_entity(name: str) -> str:  # type: ignore[misc]
        return name

    def _resolve_in_category(name: str, category: str) -> str:  # type: ignore[misc]
        return ""


# ─── Semantica 가용 여부 자동 감지 ──────────────────────────────────────────────
_SEM_AVAILABLE = False  # Semantica 패키지 설치 여부
_KOREAN_OK = False  # 한국어 엔티티 인식 가능 여부
_sem_checked = False  # 한 번만 체크


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
        print(
            "  [Semantica] 패키지 없음 — fallback 비활성화 (pip install semantica[graph-falkordb])"
        )
    except Exception as e:
        print(f"  [Semantica] 초기화 실패: {e}")


# ─── 0-b. 임베딩 배치 분할 ───────────────────────────────────────────────────
# Vertex 임베딩은 **요청당 총 토큰**이 제한됩니다(현재 20,000). 개수만 보고
# 묶으면 한도를 넘습니다 — 실측: 800자 청크 50개가 24,608토큰으로 400 을 받아,
# 34,263자짜리 문서 하나가 세 번의 --reset 내내 벡터 없이 남았습니다.
# (트리플은 만들어졌으므로 관계 질문에는 답하는데 문서 검색에는 안 잡히는
#  상태였고, 요약 로그에는 아무 이상이 없었습니다.)
#
# 토큰을 정확히 세려면 API 호출이 필요하므로 문자 수로 근사합니다.
# 실측 비율은 한국어에서 24,608토큰 / 40,750자 ≈ 0.60 토큰/자 이므로,
# 20,000자 배치는 약 12,000토큰 — 한도 대비 40% 여유입니다.
EMBED_BATCH_MAX_CHARS: int = int(os.environ.get("EMBED_BATCH_MAX_CHARS", "20000"))


def batch_texts(texts: list, max_chars: int = EMBED_BATCH_MAX_CHARS, max_items: int = 50) -> list:
    """문자 수와 개수를 **모두** 지키도록 배치를 나눕니다.

    한 항목이 혼자 max_chars 를 넘으면 그것만 단독 배치로 보냅니다 (쪼개지
    않습니다 — 청크는 이미 상위에서 나뉘어 있고, 여기서 또 자르면 벡터와
    저장된 원문이 어긋납니다).

    >>> [len(b) for b in batch_texts(["a" * 100] * 5, max_chars=250, max_items=50)]
    [2, 2, 1]
    >>> [len(b) for b in batch_texts(["a"] * 7, max_chars=1000, max_items=3)]
    [3, 3, 1]
    >>> batch_texts([])
    []
    """
    out: list = []
    cur: list = []
    cur_chars = 0
    for s in texts:
        n = len(s)
        if cur and (cur_chars + n > max_chars or len(cur) >= max_items):
            out.append(cur)
            cur, cur_chars = [], 0
        cur.append(s)
        cur_chars += n
    if cur:
        out.append(cur)
    return out


# ─── 0-a. 추출 입력 분할 ─────────────────────────────────────────────────────
# LLM 추출은 오랫동안 본문 앞 3000자만 보고 있었습니다. 실측: 299페이지
# 344,338자 중 **45%(155,294자)가 추출 대상에서 잘려나갔고**, 가장 긴 문서는
# 34,263자였습니다. 그 뒤쪽에 있는 관계·이벤트는 애초에 추출될 기회가
# 없었습니다 — 페이지 절단으로 복합 카테고리가 전멸했던 것과 같은 구조이며,
# 그때는 전달 단계, 이번엔 추출 단계입니다.
#
# 상한을 없애고 통째로 넣는 방법은 쓰지 않습니다. 입력은 들어가도 **응답**이
# max_tokens 에서 잘려 JSON 파싱이 실패하고, 지금은 그 실패가 예외로 올라가
# 페이지 전체가 error 가 됩니다. 그래서 겹치는 창으로 나눠 여러 번 호출하고
# 결과를 합칩니다. 겹침은 창 경계에 걸친 관계를 놓치지 않기 위한 것입니다.
EXTRACT_WINDOW_CHARS: int = int(os.environ.get("EXTRACT_WINDOW_CHARS", "6000"))
EXTRACT_WINDOW_OVERLAP: int = int(os.environ.get("EXTRACT_WINDOW_OVERLAP", "600"))


def text_windows(
    text: str,
    size: int = EXTRACT_WINDOW_CHARS,
    overlap: int = EXTRACT_WINDOW_OVERLAP,
) -> list[str]:
    """본문을 겹치는 창으로 나눕니다. size 이하면 통째로 1개.

    >>> text_windows("abc", size=10)
    ['abc']
    >>> [len(w) for w in text_windows("x" * 25, size=10, overlap=3)]
    [10, 10, 10, 4]
    >>> text_windows("   ")
    []
    """
    text = text or ""
    if len(text) <= size:
        return [text] if text.strip() else []
    step = max(1, size - overlap)
    out: list[str] = []
    start = 0
    while start < len(text):
        piece = text[start : start + size]
        if piece.strip():
            out.append(piece)
        if start + size >= len(text):
            break
        start += step
    return out


def _warn_if_output_truncated(resp, label: str) -> None:
    """응답이 max_tokens 에서 잘렸으면 경고합니다.

    잘리면 JSON 이 미완성이라 파싱이 실패하고, 그 예외가 페이지 전체를 error 로
    만듭니다. 원인을 "LLM 오류"로 오해하지 않도록 창 크기를 줄이라고 알려줍니다.
    """
    if getattr(resp, "stop_reason", None) == "max_tokens":
        print(
            f"    ⚠️  {label} 응답이 max_tokens 에서 잘렸습니다 — "
            f"EXTRACT_WINDOW_CHARS({EXTRACT_WINDOW_CHARS})를 줄이세요"
        )


# ─── 0. 인덱스 ───────────────────────────────────────────────────────────────
# (label, property). ingest 와 scripts/create_indexes.py 가 같은 목록을 씁니다.
#
# 인덱스는 조회 성능만의 문제가 아닙니다. 인제스트 자체가 노드마다
# MERGE (n:Label {name: ...}) 를, 이벤트마다 MERGE (e:Event {event_id: ...}) 를
# 실행하므로, 인덱스가 없으면 매 건이 레이블 전체 스캔이 되어 O(n²) 로 늘어납니다.
INDEX_SPECS: list[tuple[str, str]] = [
    # ── 트리플 엔티티 8종 (+ 타입 미상 폴백) — merge_node 가 name 으로 MERGE ──
    ("Person", "name"),
    ("Team", "name"),
    ("Process", "name"),
    ("System", "name"),
    ("Policy", "name"),
    ("Document", "name"),
    ("Role", "name"),
    ("Decision", "name"),
    ("Unknown", "name"),
    # ── Decision 온톨로지 ─────────────────────────────────────────────
    ("Decision", "subject"),
    ("Decision", "outcome"),
    ("Decision", "date"),
    # ── Event·Game 온톨로지 ───────────────────────────────────────────
    ("Event", "event_id"),  # upsert_event_node 의 MERGE 키
    ("Event", "scope"),  # FOLLOWED_BY 앞뒤 탐색 · get_event_chain 필터
    ("Event", "game"),
    ("Event", "event_type"),
    ("Event", "date_ts"),  # range 탐색 핵심
    ("Game", "name"),
]

# 이미 존재하는 인덱스를 다시 만들 때 FalkorDB 가 내는 메시지들
_INDEX_EXISTS_MARKERS = ("already indexed", "already exists", "equivalent index")


def ensure_indexes(graph, verbose: bool = True) -> dict:
    """INDEX_SPECS 를 멱등적으로 생성합니다. {created, existing, failed} 반환.

    그래프를 삭제하면 인덱스도 함께 사라지므로, --reset 인제스트에서는
    데이터를 넣기 **전에** 호출해야 합니다.
    """
    stats = {"created": 0, "existing": 0, "failed": 0}
    for label, prop in INDEX_SPECS:
        try:
            graph.query(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
            stats["created"] += 1
        except Exception as exc:
            if any(m in str(exc).lower() for m in _INDEX_EXISTS_MARKERS):
                stats["existing"] += 1
            else:
                stats["failed"] += 1
                if verbose:
                    print(f"    ⚠️  인덱스 실패 {label}.{prop} — {exc}")
    if verbose:
        print(
            f"  🔑 인덱스: 생성 {stats['created']} / 기존 {stats['existing']}"
            + (f" / 실패 {stats['failed']}" if stats["failed"] else "")
        )
    return stats


# ─── 1. 엔티티 중복 제거 (MERGE) ─────────────────────────────────────────────


def merge_node(graph, entity_name: str, entity_type: str, source_url: str) -> int:
    """
    FalkorDB에서 (entity_type {name: entity_name}) 노드를 MERGE 방식으로 생성/조회.

    - 이미 존재하는 노드 → 기존 node_id 반환 (중복 생성 방지)
    - 없으면 신규 생성 후 node_id 반환
    - 실패 시 -1 반환
    - 비즈니스 용어집 API를 통해 entity_name을 canonical form으로 정규화 후 저장

    사용:
        node_id = merge_node(graph, "운영팀", "Team", "https://notion.so/...")
    """
    # 동의어 → canonical(term) 정규화: "드래곤슈퍼" → "DS", "월간활성유저" → "MAU"
    entity_name = _resolve_entity(entity_name)

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
_VALID_NER_TYPES: frozenset = frozenset(
    {
        "PERSON",
        "ORG",
        "PRODUCT",
        "FAC",
        "WORK_OF_ART",
        "EVENT",
        "NORP",
        "Entity",  # Semantica 기본 타입
        "Team",
        "System",
        "Process",
        "Policy",
        "Document",
        "Role",  # 커스텀 온톨로지 타입
    }
)
_SKIP_NER_TYPES: frozenset = frozenset(
    {
        "DATE",
        "TIME",
        "CARDINAL",
        "ORDINAL",
        "PERCENT",
        "MONEY",
        "QUANTITY",
        "LOC",
        "GPE",  # 지명·국가는 업무 온톨로지에서 불필요
    }
)
# 날짜/숫자 패턴 엔티티 이름 제외
_DATE_NUM_RE = re.compile(
    r"^\d+$"  # 순수 숫자
    r"|^\d{4}[-/.년]\d{1,2}"  # YYYY-MM, YYYY년MM
    r"|\d{1,2}시\s*\d{0,2}분?"  # 시각 (오전 5시 15분)
    r"|^20\d{2}"  # 연도 단독 (2026 등)
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
        triplets = []
        for r in relations:
            subj_raw = _attr(r, "subject", "head")
            obj_raw = _attr(r, "object", "tail")
            pred_raw = _attr(r, "predicate", "relation")

            subj_name = _text(subj_raw)
            obj_name = _text(obj_raw)
            pred_name = _text(pred_raw)
            subj_type = _etype(subj_raw)
            obj_type = _etype(obj_raw)

            if not subj_name or not obj_name or not pred_name:
                continue

            # 노이즈 엔티티 제거: 날짜·숫자·너무 짧은 이름
            if not _is_valid_entity(subj_name, subj_type):
                continue
            if not _is_valid_entity(obj_name, obj_type):
                continue

            triplets.append(
                {
                    "subject": {"name": subj_name, "type": subj_type},
                    "predicate": {"name": pred_name},
                    "object": {"name": obj_name, "type": obj_type},
                }
            )

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
        source = "llm" | "empty" | "error"

        "empty" 와 "error" 는 반드시 구분해야 합니다. 둘 다 빈 리스트지만
        "empty" 는 "추출할 관계가 없다"는 확정 판단이고 "error" 는 아무것도
        알지 못한다는 뜻입니다. 호출부가 이를 구분하지 못하면 일시적 API 오류를
        "관계 없는 페이지"로 기록하고 재시도하지 않게 됩니다.
    """
    # LLM 추출
    try:
        result = llm_extractor_fn(text)
        if result:
            return result, "llm"
    except Exception as e:
        print(f"    ⚠️  LLM 추출 실패: {e}")
        return [], "error"

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
            return {
                "found": False,
                "start": start_name,
                "end": end_name,
                "reason": "엔티티를 그래프에서 찾을 수 없음",
            }

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
            return {
                "found": False,
                "start": s_name,
                "end": e_name,
                "reason": f"{max_hops}홉 이내 경로 없음",
            }

        nodes = path_r.result_set[0][0] or []
        rels = path_r.result_set[0][1] or []
        return {
            "found": True,
            "start": s_name,
            "end": e_name,
            "path_nodes": nodes,
            "path_relations": rels,
            "hops": len(rels),
        }

    except Exception as e:
        return {"found": False, "start": start_name, "end": end_name, "error": str(e)}


# ─── 4-6. 의사결정 추적 (trace_decision_chain) ───────────────────────────────

# 의사결정을 나타내는 한국어 술어 키워드
DECISION_KEYWORDS: frozenset = frozenset(
    [
        "승인",
        "결정",
        "채택",
        "선택",
        "완료",
        "확정",
        "검토",
        "허가",
        "처리",
        "배정",
        "지정",
        "선정",
        "의결",
        "보고",
        "승낙",
        "거부",
        "반려",
        "취소",
        "변경",
        "수정",
        "합의",
        "위임",
        "지시",
        "요청",
        "승계",
        "이관",
    ]
)


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
    subj = triplet.get("subject", {})
    pred = triplet.get("predicate", {})
    obj = triplet.get("object", {})

    subj_name = subj.get("name", "") if isinstance(subj, dict) else str(subj)
    pred_name = pred.get("name", "") if isinstance(pred, dict) else str(pred)
    obj_name = obj.get("name", "") if isinstance(obj, dict) else str(obj)

    if not subj_name or not pred_name or not obj_name:
        return -1

    # 안정적 ID: 출처 + 트리플 내용 기반 uuid5
    did = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{source_url}|{subj_name}|{pred_name}|{obj_name}",
        )
    )
    ts = datetime.now(UTC).isoformat()

    try:
        r = graph.query(
            "MERGE (d:Decision {decision_id: $did}) "
            "ON CREATE SET d.subject = $subject, d.action = $action, "
            "              d.outcome = $outcome, d.source_url = $url, d.ts = $ts "
            "RETURN id(d) AS nid",
            {
                "did": did,
                "subject": subj_name,
                "action": pred_name,
                "outcome": obj_name,
                "url": source_url,
                "ts": ts,
            },
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
                "entity": entity_name,
                "found": False,
                "decisions": [],
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

            decisions.append(
                {
                    "decision_id": did,
                    "subject": subj,
                    "action": action,
                    "outcome": outcome,
                    "source_url": url or "",
                    "ts": ts or "",
                    "leads_to": leads_to,
                    "led_by": led_by,
                }
            )

        # 3. 체인 요약 (시계열 순)
        chain_summary = [f"{d['subject']} → {d['action']} → {d['outcome']}" for d in decisions]

        return {
            "entity": entity_name,
            "found": True,
            "decisions": decisions,
            "chain_summary": chain_summary,
        }

    except Exception as e:
        return {
            "entity": entity_name,
            "found": False,
            "decisions": [],
            "chain_summary": [],
            "error": str(e),
        }


# ─── 7. 이벤트 노드 (upsert_event_node) ─────────────────────────────────────

EVENT_TYPES: frozenset = frozenset(
    [
        "client_update",
        "server_update",
        "user_event",
        "season",
        "content_release",
        "maintenance",
        "incident",
        "kpi_milestone",
        # UA 마케팅 이벤트 (ingest.py / sync.py EVENT_EXTRACT_PROMPT와 동기화)
        "ua_budget",
        "ua_creative",
        "ua_channel",
        "ua_targeting",
        "ua_abtest",
        # UA 변경 이력 (Notion DB '변경카테고리' 값)
        "ua_campaign",
    ]
)

# ─── DB 속성 키 별칭 ─────────────────────────────────────────────────────────
# Notion DB 컬럼명은 자유롭게 설정되므로, 소문자 비교(case-insensitive)로 처리한다.
# ingest.py / sync.py 에서 공통으로 사용하는 단일 정의.
# ※ 전부 **튜플**입니다 — set 을 쓰면 안 됩니다.
#   한 DB 에 후보 컬럼이 둘 이상 있을 때(예: 담당자와 생성자, 변경일과 적용일)
#   set 순회 순서는 PYTHONHASHSEED 에 따라 프로세스마다 달라져, 같은 페이지가
#   실행할 때마다 다른 담당자·다른 날짜로 저장됩니다. 앞에 있을수록 우선.
DB_TITLE_KEYS = (
    # Notion DB에서 실질적인 제목/메모를 담는 텍스트 컬럼 후보
    "메모",
    "memo",
    "제목",
    "이벤트명",
    "이벤트제목",
    "내용",
    "description",
    "설명",
    "name",
    "이름",
)

DB_DATE_KEYS = (
    "이벤트날짜",
    "날짜",
    "일자",
    "date",
    "event_date",
    "시작일",
    "시작날짜",
    "변경일",
    "적용일",
)
DB_GAME_KEYS = (
    "게임명",
    "게임",
    "game",
    "product",
    "서비스명",
    "서비스",
    "project",  # RESU UA 히스토리 등 영문 PROJECT 컬럼 지원
)
DB_TYPE_KEYS = (
    "이벤트유형",
    "유형",
    "event_type",
    "type",
    "종류",
    "변경카테고리",
    "카테고리",
    "category",
    "change_type",
    "변경유형",
)
DB_MANAGER_KEYS = (
    "담당자",
    "담당팀",
    "manager",
    "owner",
    "담당",
    "생성자",
    "작성자",
    "creator",  # Notion DB 생성자·작성자 컬럼 지원
)

# 변경카테고리 원문 → EVENT_TYPES 정규값 매핑
# 매핑에 없는 값은 그대로 event_type 으로 사용 (EVENT_TYPES 에 없으면 ua_campaign 으로 폴백)
_CATEGORY_TO_EVENT_TYPE: dict[str, str] = {
    "캠페인조정": "ua_campaign",
    "캠페인 조정": "ua_campaign",
    "소재변경": "ua_creative",
    "소재 변경": "ua_creative",
    "예산변경": "ua_budget",
    "예산 변경": "ua_budget",
    "국가변경": "ua_targeting",
    "국가 변경": "ua_targeting",
    "타겟변경": "ua_targeting",
    "타겟 변경": "ua_targeting",
    "채널변경": "ua_channel",
    "채널 변경": "ua_channel",
    "ab테스트": "ua_abtest",
    "a/b테스트": "ua_abtest",
}


SCOPE_GAME = "game"
SCOPE_ORG = "org"
SCOPE_UNKNOWN = "unknown"

# ─── 주체 판정 리포트 ─────────────────────────────────────────────────────────
# 인제스트 중 마스터에 없는 게임 후보와 판정 실패 건을 모아 요약에 노출합니다.
# 이것이 없으면 미등록 게임이 조용히 unverified 로 저장되고, 용어집이
# 갱신되지 않은 채 방치됩니다.
_scope_lock = threading.Lock()
_unverified_games: dict[str, int] = {}
_unknown_scopes: dict[str, int] = {}  # {샘플 제목: 횟수}
_unknown_total = 0
_UNKNOWN_SAMPLE_LIMIT = 8


def _record_scope_issue(kind: str, label: str) -> None:
    """미검증/미분류 주체를 기록합니다 (스레드 안전 — 인제스트는 병렬 실행)."""
    global _unknown_total
    with _scope_lock:
        if kind == SCOPE_GAME:
            _unverified_games[label] = _unverified_games.get(label, 0) + 1
        else:
            _unknown_total += 1
            if label and (label in _unknown_scopes or len(_unknown_scopes) < _UNKNOWN_SAMPLE_LIMIT):
                _unknown_scopes[label] = _unknown_scopes.get(label, 0) + 1


def scope_report() -> dict:
    """주체 판정 리포트를 반환합니다.

    Returns:
        {
          "unverified_games": {값: 횟수},   # 게임 컬럼 출처인데 용어집 미등록
          "unknown_total": int,             # 주체를 판정하지 못한 이벤트 수
          "unknown_samples": {제목: 횟수},  # 그중 일부 샘플
        }
    """
    with _scope_lock:
        return {
            "unverified_games": dict(_unverified_games),
            "unknown_total": _unknown_total,
            "unknown_samples": dict(_unknown_scopes),
        }


def reset_scope_report() -> None:
    """리포트를 초기화합니다. 인제스트/동기화 시작 시 호출하세요."""
    global _unknown_total
    with _scope_lock:
        _unverified_games.clear()
        _unknown_scopes.clear()
        _unknown_total = 0


def format_scope_report(report: dict | None = None) -> str:
    """리포트를 사람이 읽을 형태로 포맷합니다. 이슈가 없으면 빈 문자열."""
    rep = report if report is not None else scope_report()
    unverified = rep.get("unverified_games") or {}
    unknown_total = rep.get("unknown_total") or 0
    if not unverified and not unknown_total:
        return ""

    lines: list[str] = []
    if unverified:
        items = sorted(unverified.items(), key=lambda x: x[1], reverse=True)
        listed = ", ".join(f'"{name}"({cnt}건)' for name, cnt in items[:10])
        more = f" 외 {len(items) - 10}종" if len(items) > 10 else ""
        lines.append(f"  ⚠️  용어집 미등록 게임 {len(items)}종: {listed}{more}")
        lines.append("      → 용어집(category=game)에 등록하거나 Notion 컬럼 배치를 확인하세요")
    if unknown_total:
        samples = sorted(
            (rep.get("unknown_samples") or {}).items(), key=lambda x: x[1], reverse=True
        )
        listed = ", ".join(f'"{t[:30]}"' for t, _ in samples[:5])
        lines.append(
            f"  ⚠️  주체 미분류 이벤트 {unknown_total}건" + (f" (예: {listed})" if listed else "")
        )
        lines.append("      → 제목에 부서명이 없거나 :Team 노드가 아직 없는 경우입니다")
    return "\n".join(lines)


# game 컬럼이 비었을 때 쓰이던 기존 플레이스홀더 — 주체로 취급하지 않습니다.
_SCOPE_PLACEHOLDERS: frozenset = frozenset({"", "기타", "미정", "없음", "-", "n/a", "na"})


def classify_scope(
    value: str = "",
    source_key: str = "",
    text: str = "",
    graph=None,
) -> dict:
    """이벤트 주체(scope)와 그 종류를 판정합니다.

    기존에는 게임명이 없으면 무조건 "기타" 로 저장돼, 재무실·인사팀 등
    서로 다른 부서의 일정이 한 버킷에 뒤섞였습니다. 이 함수는 주체를
    게임/조직으로 구분해 그 문제를 해소합니다.

    판정 순서:
      ① 용어집 game 카테고리 매칭       → game, verified=True
      ② 용어집 organization 카테고리    → org,  verified=True
      ③ 게임 컬럼 출처인데 용어집에 없음 → game, verified=False (마스터 등록 후보)
      ④ 제목·본문에 등장하는 :Team 노드  → org,  verified=False
      ⑤ 해당 없음                       → unknown

    ④가 필요한 이유: Notion DB에 부서 컬럼이 따로 없어 조직 정보가
    제목·본문에만 존재합니다. 그래프에 이미 적재된 :Team 노드와 대조합니다.

    Args:
        value:      주체 후보 값 (DB 게임 컬럼 값 또는 LLM 추출 game)
        source_key: value 를 읽어온 Notion 컬럼명. 없으면 빈 문자열.
        text:       제목·본문 등 조직명을 찾을 텍스트
        graph:      FalkorDB graph — ④에만 사용. None 이면 ④ 생략.

    Returns:
        {"scope": str, "scope_type": "game"|"org"|"unknown", "scope_verified": bool}
    """
    val = str(value or "").strip()
    is_placeholder = val.lower() in _SCOPE_PLACEHOLDERS

    if val and not is_placeholder:
        # ① 게임 마스터 (동의어 포함): "캐리비안의 해적" → "POTC"
        canonical = _resolve_in_category(val, "game")
        if canonical:
            return {"scope": canonical, "scope_type": SCOPE_GAME, "scope_verified": True}

        # ② 조직 마스터 (용어집에 organization 카테고리가 생기면 자동 적용)
        canonical = _resolve_in_category(val, "organization")
        if canonical:
            return {"scope": canonical, "scope_type": SCOPE_ORG, "scope_verified": True}

        # ③ 게임 컬럼에서 왔지만 마스터 미등록 — 신규 게임 후보로 표시
        if source_key and source_key.lower() in {k.lower() for k in DB_GAME_KEYS}:
            return {"scope": val, "scope_type": SCOPE_GAME, "scope_verified": False}

    # ④ 텍스트에서 조직 탐색 — 그래프의 :Team 노드와 대조
    if graph is not None and text:
        try:
            from utils.korean import contains_as_token

            res = graph.query(
                "MATCH (t:Team) WHERE t.name IS NOT NULL AND $text CONTAINS t.name "
                "RETURN DISTINCT t.name AS name LIMIT 20",
                {"text": text},
            )
            cands = [
                str(r[0])
                for r in res.result_set
                if r and r[0] and len(str(r[0])) >= 2 and contains_as_token(text, str(r[0]))
            ]
            if cands:
                # 가장 구체적인(긴) 이름 우선: "데이터사이언스실" > "데이터"
                cands.sort(key=len, reverse=True)
                return {
                    "scope": _resolve_entity(cands[0]),
                    "scope_type": SCOPE_ORG,
                    "scope_verified": False,
                }
        except Exception:
            pass  # 그래프 조회 실패 시 unknown 으로 처리

    # ⑤ 판정 불가 — 값이 있으면 보존하되 종류는 unknown
    return {
        "scope": "" if is_placeholder else val,
        "scope_type": SCOPE_UNKNOWN,
        "scope_verified": False,
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

    def _first_with_key(keys: tuple) -> tuple[str | None, str]:
        """(값, 매칭된 컬럼명) — 값의 출처를 알아야 주체 종류를 추론할 수 있습니다.

        keys 는 우선순위 튜플입니다. set 을 넘기면 순회 순서가 프로세스마다
        달라져 같은 페이지가 실행할 때마다 다른 값으로 저장됩니다.
        """
        for k in keys:
            v = lower.get(k.lower())
            if v is not None:
                val = ", ".join(str(x) for x in v) if isinstance(v, list) else str(v)
                return val, k
        return None, ""

    def _first(keys: tuple) -> str | None:
        return _first_with_key(keys)[0]

    date = _first(DB_DATE_KEYS)
    if not date:
        return None  # 날짜 없으면 이벤트 아님

    game_raw, game_key = _first_with_key(DB_GAME_KEYS)
    game = game_raw or "기타"
    raw_type = _first(DB_TYPE_KEYS) or ""
    # 변경카테고리 → EVENT_TYPES 정규값 변환
    event_type = _CATEGORY_TO_EVENT_TYPE.get(raw_type.strip(), raw_type.strip())
    if event_type not in EVENT_TYPES:
        event_type = "ua_campaign" if raw_type else "user_event"

    manager_raw = _first(DB_MANAGER_KEYS) or ""

    # DB 속성에서 실질적 제목 추출 (메모/내용 등 우선, 없으면 페이지 meta title 폴백)
    # meta title은 Notion 파일명(page_id)일 수 있으므로 DB 컬럼을 먼저 확인한다.
    db_title = ""
    for tk in DB_TITLE_KEYS:
        v = lower.get(tk.lower())
        if v is not None:
            db_title = ", ".join(str(x) for x in v) if isinstance(v, list) else str(v)
            if db_title.strip():
                break
    resolved_title = db_title.strip() or title

    return {
        "game": game,
        "event_type": event_type,
        "category": raw_type,  # 변경카테고리 원문 보존 (캠페인조정, 소재 변경 등)
        "date": date[:10],
        "title": resolved_title,
        "description": "",
        "manager": manager_raw,
        "source_url": source_url,
        # 주체 판정용 — upsert_event_node 의 classify_scope 가 사용합니다.
        # 어느 컬럼에서 읽었는지 알아야 게임/조직을 추론할 수 있습니다.
        "scope_source_key": game_key,
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

    주체(scope) 판정:
        classify_scope() 로 게임/조직을 구분해 다음 속성을 기록합니다.
          e.scope          — 주체 이름 (게임 코드 또는 조직명)
          e.scope_type     — game | org | unknown
          e.scope_verified — 용어집 마스터로 확인되었는지

        게임으로 판정된 경우에만 e.game 이 유지됩니다. 조직이면 e.game 은
        비워집니다 — 이전에는 game="기타" 로 저장돼 :Game 노드에 게임이
        아닌 것이 섞였습니다.

    부수 효과:
        - scope_type=game → :Game 노드 MERGE + (Game)-[:HAD_EVENT]->(Event)
        - scope_type=org  → :Team 노드 MERGE + (Team)-[:HAD_EVENT]->(Event)
        - 같은 scope 의 이전/이후 이벤트와 FOLLOWED_BY 엣지 자동 연결
          (scope 가 비면 체인을 만들지 않아 서로 무관한 주체가 섞이지 않음)

    Returns:
        FalkorDB node id (실패 시 -1)
    """
    from datetime import datetime

    game = str(event.get("game", "")).strip()
    event_type = str(event.get("event_type", "")).strip()
    category = str(event.get("category", "")).strip()  # 변경카테고리 원문 (예: "캠페인조정")
    date = str(event.get("date", "")).strip()
    title = str(event.get("title", "")).strip()
    description = str(event.get("description", ""))
    target = str(event.get("target", ""))
    manager = str(event.get("manager", ""))
    source_url = str(event.get("source_url", ""))

    if not (game and date and title):
        return -1

    # ── 주체(scope) 판정 ────────────────────────────────────────────────────
    # game 값이 "기타" 같은 플레이스홀더면 제목·본문에서 조직을 찾습니다.
    # 이전에는 전부 game="기타" 로 뭉쳐져 부서별 구분이 불가능했습니다.
    scope_info = classify_scope(
        value=game,
        source_key=str(event.get("scope_source_key", "")),
        text=f"{title} {description}",
        graph=graph,
    )
    scope = scope_info["scope"]
    scope_type = scope_info["scope_type"]
    scope_verified = scope_info["scope_verified"]
    # 게임으로 판정된 경우에만 game 필드를 유지합니다 (:Game 노드 오염 방지).
    # 판정 실패 시에는 기존 값을 그대로 두어 하위 호환을 유지합니다.
    if scope_type == SCOPE_GAME:
        game = scope or game
    elif scope_type == SCOPE_ORG:
        game = ""

    # 인제스트 요약에 노출할 이슈 기록 — 용어집 갱신·데이터 점검의 근거
    if scope_type == SCOPE_GAME and not scope_verified:
        _record_scope_issue(SCOPE_GAME, scope or game)
    elif scope_type == SCOPE_UNKNOWN:
        _record_scope_issue(SCOPE_UNKNOWN, title)

    # event_type 정규화
    if event_type not in EVENT_TYPES:
        event_type = "user_event"

    # 날짜 파싱
    date_ts = _date_to_ts(date)
    if date_ts == 0:
        return -1

    try:
        dt = datetime.fromtimestamp(date_ts, tz=UTC)
        year = dt.year
        month = dt.month
        quarter = _month_to_quarter(month)
    except Exception:
        year, month, quarter = 0, 0, ""

    # 안정적 ID: source_url이 있으면 Notion 페이지 URL 기준 (행마다 고유)
    # source_url이 없으면 game|event_type|date|title 해시로 폴백
    # ※ 이전: game|event_type|date 만 사용 → 같은 날 같은 유형 여러 행이 충돌
    if source_url:
        event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, source_url))
    else:
        import hashlib

        title_hash = hashlib.md5(title.encode()).hexdigest()[:8]
        event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{game}|{event_type}|{date}|{title_hash}"))
    ts = datetime.now(UTC).isoformat()

    # ── 1. :Event 노드 MERGE ────────────────────────────────────────────────
    # ON CREATE 와 ON MATCH 는 e.ts(최초 생성 시각)만 빼고 완전히 같은 필드를
    # 씁니다. 두 목록을 따로 적었더니 실제로 어긋났습니다 — manager 는 ON CREATE
    # 에 없어서 신규 이벤트의 담당자가 항상 null 이었고(키워드 검색이 조용히
    # 실패), date·date_ts·event_type·target 은 ON MATCH 에 없어서 Notion 에서
    # 날짜를 고쳐도 그래프에 영원히 반영되지 않았습니다. 아래처럼 한 벌만
    # 정의해 양쪽에 끼워 넣어야 다시 어긋나지 않습니다.
    _event_fields = (
        "e.game = $game, e.event_type = $etype, e.category = $category, "
        "e.scope = $scope, e.scope_type = $scope_type, "
        "e.scope_verified = $scope_verified, "
        "e.date = $date, e.date_ts = $date_ts, "
        "e.year = $year, e.month = $month, e.quarter = $quarter, "
        "e.title = $title, e.description = $desc, e.target = $target, "
        "e.manager = $mgr, e.source_url = $url"
    )
    try:
        r = graph.query(
            "MERGE (e:Event {event_id: $eid}) "
            f"ON CREATE SET {_event_fields}, e.ts = $ts "
            f"ON MATCH SET {_event_fields} "
            "RETURN id(e) AS nid",
            {
                "eid": event_id,
                "game": game,
                "etype": event_type,
                "category": category,
                "scope": scope,
                "scope_type": scope_type,
                "scope_verified": scope_verified,
                "date": date,
                "date_ts": date_ts,
                "year": year,
                "month": month,
                "quarter": quarter,
                "title": title,
                "desc": description,
                "target": target,
                "mgr": manager,
                "url": source_url,
                "ts": ts,
            },
        )
        if not r.result_set:
            return -1
        event_node_id = r.result_set[0][0]
    except Exception as e:
        print(f"    ⚠️  Event 노드 생성 실패 ({game}/{date}/{title}): {e}")
        return -1

    # ── 2. 주체 노드 연결 (HAD_EVENT) ──────────────────────────────────────
    # 게임이면 :Game, 조직이면 :Team 에 연결합니다.
    # 이전에는 game 값이 "기타"여도 :Game 노드를 만들어, 게임이 아닌 것이
    # 게임 목록에 섞여 들어갔습니다.
    if scope_type == SCOPE_GAME and game:
        try:
            graph.query(
                "MERGE (g:Game {name: $name}) ON CREATE SET g.source_url = $url RETURN id(g)",
                {"name": game, "url": source_url},
            )
            graph.query(
                "MATCH (g:Game {name: $game}) MATCH (e:Event {event_id: $eid}) "
                "MERGE (g)-[:HAD_EVENT {date: $date}]->(e)",
                {"game": game, "eid": event_id, "date": date},
            )
        except Exception:
            pass
    elif scope_type == SCOPE_ORG and scope:
        try:
            # 조직 노드는 트리플 추출이 이미 만들었을 가능성이 높으므로 MERGE 로
            # 기존 노드를 재사용합니다.
            graph.query(
                "MERGE (t:Team {name: $name}) ON CREATE SET t.source_url = $url RETURN id(t)",
                {"name": scope, "url": source_url},
            )
            graph.query(
                "MATCH (t:Team {name: $name}) MATCH (e:Event {event_id: $eid}) "
                "MERGE (t)-[:HAD_EVENT {date: $date}]->(e)",
                {"name": scope, "eid": event_id, "date": date},
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
                # 찾은 그 노드에만 연결합니다. 이름만으로 다시 MATCH 하면
                # 같은 이름이 :Team 과 :Role 로 각각 존재할 때(LLM 이 문서마다
                # 다른 타입을 붙이는 일이 흔합니다) 양쪽에 엣지가 생깁니다.
                graph.query(
                    "MATCH (m) WHERE id(m) = $mid "
                    "MATCH (e:Event {event_id: $eid}) "
                    "MERGE (e)-[:MANAGED_BY]->(m)",
                    {"mid": mgr_r.result_set[0][0], "eid": event_id},
                )
        except Exception:
            pass

    # ── 4. FOLLOWED_BY 자동 연결 (같은 주체, 날짜 순서) ─────────────────────
    # scope 기준으로 연결합니다. game 기준이던 이전 방식은 주체가 없는
    # 이벤트를 모두 "기타"로 묶어, 재무실 일정과 인사팀 일정이 하나의
    # 체인으로 잘못 이어졌습니다. scope 가 비면 체인을 만들지 않습니다.
    if scope:
        try:
            # 직전 이벤트
            prev_r = graph.query(
                "MATCH (e:Event) WHERE e.scope = $scope AND e.date_ts < $ts "
                "RETURN e.event_id, e.date_ts ORDER BY e.date_ts DESC LIMIT 1",
                {"scope": scope, "ts": date_ts},
            )
            prev_eid = prev_r.result_set[0][0] if prev_r.result_set else None

            # 직후 이벤트
            next_r = graph.query(
                "MATCH (e:Event) WHERE e.scope = $scope AND e.date_ts > $ts "
                "RETURN e.event_id, e.date_ts ORDER BY e.date_ts ASC LIMIT 1",
                {"scope": scope, "ts": date_ts},
            )
            next_eid = next_r.result_set[0][0] if next_r.result_set else None

            # 이 이벤트가 기존 prev→next 구간을 가릅니다. 낡은 건너뛰기 엣지를
            # 먼저 지우지 않으면 A→C 가 남은 채 A→B, B→C 가 추가되어, A 의
            # "직후 이벤트"가 B 인지 C 인지 비결정적이 되고 존재하지 않는
            # 간격(A→C 의 days_diff)이 그대로 보고됩니다. 인제스트가 병렬이라
            # 날짜 역순 도착은 예외가 아니라 일상입니다.
            if prev_eid and next_eid:
                graph.query(
                    "MATCH (p:Event {event_id: $p})-[r:FOLLOWED_BY]->(n:Event {event_id: $n}) "
                    "DELETE r",
                    {"p": prev_eid, "n": next_eid},
                )

            if prev_eid:
                prev_ts_v = prev_r.result_set[0][1]
                graph.query(
                    "MATCH (p:Event {event_id: $p}) MATCH (c:Event {event_id: $c}) "
                    "MERGE (p)-[r:FOLLOWED_BY]->(c) SET r.days_diff = $dd",
                    {"p": prev_eid, "c": event_id, "dd": round((date_ts - prev_ts_v) / 86400)},
                )

            if next_eid:
                next_ts_v = next_r.result_set[0][1]
                graph.query(
                    "MATCH (c:Event {event_id: $c}) MATCH (n:Event {event_id: $n}) "
                    "MERGE (c)-[r:FOLLOWED_BY]->(n) SET r.days_diff = $dd",
                    {"c": event_id, "n": next_eid, "dd": round((next_ts_v - date_ts) / 86400)},
                )
        except Exception:
            pass  # FOLLOWED_BY 실패는 치명적이지 않음

    return event_node_id


# ─── 8. 이벤트 체인 조회 (get_event_chain) ──────────────────────────────────


def get_event_chain(
    graph,
    game: str | None = None,
    event_type: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int = 20,
    keywords: list[str] | None = None,
) -> dict:
    """
    시계열 이벤트를 날짜순으로 조회.

    game 으로 특정 게임을 조회하거나, keywords 로 게임이 아닌 주체
    (부서·조직 등)의 일정을 조회할 수 있습니다.

    ※ game 이 없는 이벤트는 인제스트 시 game="기타" 로 저장되므로
      부서명으로는 game 매칭이 되지 않습니다. keywords 경로가 그 경우를
      제목·설명·카테고리·담당자에서 찾아 보완합니다.

    Args:
        graph:      FalkorDB graph 객체
        game:       게임/서비스 이름 (부분 일치). None 이면 게임 필터 없음.
        event_type: 필터링할 이벤트 유형 (None이면 전체)
        from_date:  시작 날짜 YYYY-MM-DD (None이면 제한 없음)
        to_date:    종료 날짜 YYYY-MM-DD (None이면 제한 없음)
        limit:      최대 반환 개수 (기본 20)
        keywords:   제목·설명·카테고리·담당자·game 에서 찾을 키워드 목록.
                    2자 미만은 무시됩니다. 예: ["재무실"]

    ※ game·keywords·날짜가 모두 없으면 전체 Event 스캔이 되므로
      조회하지 않고 빈 결과를 반환합니다.

    Returns:
        {
          "game": str,
          "found": bool,
          "total": int,
          "events": [
            {
              "event_id", "game", "event_type", "date", "title",
              "description", "target", "source_url",
              "category",  # Notion "변경카테고리" 원문 (예: "소재변경")
              "manager",   # 담당자
              "prev_event": {"title", "date"} | None,
              "next_event": {"title", "date"} | None,
            }, ...
          ],
          "timeline_summary": ["2026-06-19: [소재변경] 소재 3건 OFF", ...]
        }
    """
    from_ts = _date_to_ts(from_date) if from_date else 0
    to_ts = _date_to_ts(to_date) if to_date else 9_999_999_999
    kws = [str(k).strip() for k in (keywords or []) if k and len(str(k).strip()) >= 2]
    has_date = bool(from_date or to_date)

    def _empty(label: str = "") -> dict:
        return {
            "game": label,
            "found": False,
            "total": 0,
            "events": [],
            "timeline_summary": [],
        }

    # game·keywords·날짜가 모두 없으면 전체 Event 스캔이 되므로 조회하지 않습니다.
    if not game and not kws and not has_date:
        return _empty()

    try:
        where_parts = ["e.date_ts >= $from_ts", "e.date_ts <= $to_ts"]
        params: dict = {"from_ts": from_ts, "to_ts": to_ts}
        actual_game = ""

        if game:
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
            # scope 로도 매칭 — 조직 주체이거나 game 필드가 비워진 이벤트 대응.
            # 재인제스트 전 데이터는 scope 가 없으므로 game 조건이 함께 필요합니다.
            where_parts.append("(e.game = $game OR e.scope = $game)")
            params["game"] = actual_game

        # 이벤트 유형 필터 (FalkorDB IS NULL 파라미터 미지원 → 조건 분기로 처리)
        if event_type:
            where_parts.append("e.event_type = $etype")
            params["etype"] = event_type

        # 키워드 조회 — 게임명이 특정되지 않는 주체(부서·조직 등)를 위한 경로.
        # game 속성이 "기타"로 뭉개진 이벤트도 제목·설명·카테고리로 찾을 수 있습니다.
        if kws:
            where_parts.append(
                "ANY(k IN $kws WHERE e.title CONTAINS k OR e.description CONTAINS k "
                "OR e.game CONTAINS k OR e.category CONTAINS k OR e.manager CONTAINS k "
                "OR e.scope CONTAINS k)"
            )
            params["kws"] = kws

        # OPTIONAL MATCH 으로 prev/next 를 단일 쿼리에서 조회 (이벤트당 2회 N+1 제거)
        #
        # LIMIT 은 **OPTIONAL MATCH 앞**에 둡니다. 뒤에 두면 한 이벤트에 선행·
        # 후행이 여러 개일 때 행이 곱해져서, LIMIT 20 이 이벤트 20건이 아니라
        # 행 20개를 뜻하게 됩니다 (같은 이벤트가 네 번 나오고 total 도 부풀려짐).
        # 남는 행 중복은 아래에서 event_id 로 제거합니다.
        cypher = (
            "MATCH (e:Event) WHERE " + " AND ".join(where_parts) + " "
            f"WITH e ORDER BY e.date_ts ASC LIMIT {int(limit)} "
            "OPTIONAL MATCH (prev:Event)-[:FOLLOWED_BY]->(e) "
            "OPTIONAL MATCH (e)-[:FOLLOWED_BY]->(nxt:Event) "
            "RETURN e.event_id, e.game, e.event_type, e.date, "
            "       e.title, e.description, e.target, e.source_url, "
            "       prev.title AS prev_title, prev.date AS prev_date, "
            "       nxt.title AS next_title, nxt.date AS next_date, "
            # category: Notion "변경카테고리" 원문 (예: "소재변경", "캠페인조정")
            # manager:  담당자 / scope: 주체(게임 또는 조직), scope_type: game|org|unknown
            # 기존 인덱스를 깨지 않도록 모두 뒤에 추가합니다.
            "       e.category AS category, e.manager AS manager, "
            "       e.scope AS scope, e.scope_type AS scope_type "
            "ORDER BY e.date_ts ASC"
        )
        r = graph.query(cypher, params)

        if not r.result_set:
            return _empty(actual_game)

        # prev/next 가 여러 개면 같은 이벤트가 여러 행으로 나옵니다. 첫 행만 씁니다.
        _seen_eids: set = set()
        rows = []
        for row in r.result_set:
            if row[0] in _seen_eids:
                continue
            _seen_eids.add(row[0])
            rows.append(row)

        # row: [event_id, game, event_type, date, title, description,
        #       target, source_url, prev_title, prev_date, next_title, next_date,
        #       category, manager]
        events = [
            {
                "event_id": row[0],
                "game": row[1],
                "event_type": row[2],
                "date": row[3],
                "title": row[4],
                "description": row[5] or "",
                "target": row[6] or "",
                "source_url": row[7] or "",
                "prev_event": {"title": row[8], "date": row[9]} if row[8] else None,
                "next_event": {"title": row[10], "date": row[11]} if row[10] else None,
                # 구버전 데이터는 아래 필드가 없을 수 있음 → 길이 확인 후 접근
                "category": (row[12] or "") if len(row) > 12 else "",
                "manager": (row[13] or "") if len(row) > 13 else "",
                # scope 미설정(재인제스트 전) 데이터는 game 으로 폴백
                "scope": (row[14] or row[1] or "") if len(row) > 14 else (row[1] or ""),
                "scope_type": (row[15] or "") if len(row) > 15 else "",
            }
            for row in rows
        ]

        # 요약 줄에 category 를 함께 노출 — "어떤 변경 카테고리인가?" 류 질문에
        # events 상세를 파싱하지 않고 요약만으로 답할 수 있게 합니다.
        # game 필터가 없으면 여러 주체가 섞이므로 주체명도 함께 표시합니다.
        if actual_game:
            timeline_summary = [
                f"{e['date']}: [{e['category'] or e['event_type']}] {e['title']}" for e in events
            ]
        else:
            timeline_summary = [
                f"{e['date']}: ({e['scope'] or e['game'] or '미분류'}) "
                f"[{e['category'] or e['event_type']}] {e['title']}"
                for e in events
            ]

        return {
            "game": actual_game,
            "found": True,
            "total": len(events),
            "events": events,
            "timeline_summary": timeline_summary,
            # 어떤 조건으로 조회된 결과인지 — 호출자가 근거를 판단할 수 있게 함
            "filter": {
                "game": actual_game,
                "keywords": kws,
                "event_type": event_type or "",
                "from_date": from_date or "",
                "to_date": to_date or "",
            },
        }

    except Exception as ex:
        return {
            "game": game or "",
            "found": False,
            "total": 0,
            "events": [],
            "timeline_summary": [],
            "error": str(ex),
        }


def detect_event_scopes(graph, text: str, limit: int = 3) -> list[str]:
    """질문 문장에 이름이 등장하는 게임/서비스를 찾습니다.

    :Game 노드를 우선 조회하고, 없으면 :Event 의 game 속성에서 탐색합니다
    (게임 노드 없이 이벤트만 적재된 구버전 데이터 대응).
    긴 이름을 우선해 부분 문자열 오탐을 줄입니다.
    """
    from utils.korean import contains_as_token

    names: list[str] = []
    for cypher in (
        "MATCH (g:Game) WHERE g.name IS NOT NULL AND $text CONTAINS g.name "
        "RETURN DISTINCT g.name AS name LIMIT 20",
        "MATCH (e:Event) WHERE e.game IS NOT NULL AND $text CONTAINS e.game "
        "RETURN DISTINCT e.game AS name LIMIT 20",
    ):
        try:
            res = graph.query(cypher, {"text": text})
        except Exception:
            continue
        names = [
            str(r[0])
            for r in res.result_set
            if r
            and r[0]
            and len(str(r[0])) >= 2
            # 게임 코드에 ONE·GOD·DS 등 짧은 영문이 있어 부분 문자열 오탐 제거
            and contains_as_token(text, str(r[0]))
        ]
        if names:
            break
    names.sort(key=len, reverse=True)
    return names[:limit]


def resolve_timeline_query(
    graph,
    query: str,
    limit: int = 20,
    qc=None,
    collection_name: str = "",
    page_max_chars: int = 1500,
) -> dict:
    """질문 문장에서 조회 조건을 추론해 이벤트 타임라인을 반환합니다.

    hybrid_search 와 평가 파이프라인이 공유하는 진입점입니다. 날짜 기반
    질문이 :Event 노드에 닿지 못하던 문제를 해결하며, 양쪽이 같은 판정을
    쓰도록 로직을 한 곳에 둡니다.

    조건:
      ① 날짜 표현 또는 시계열 키워드가 있어야 함 (필수)
      ② 게임명이 매칭되거나, 주체 후보(부서·조직) 또는 날짜가 잡혀야 함

    주체 결정 순서:
      1) :Game / :Event.game 에 이름이 있으면 그 게임으로 조회
      2) 아니면 질문에 등장하는 그래프 노드 이름을 keywords 로 조회
         — game 이 없는 이벤트는 "기타" 로 저장되므로 부서명으로는 game
           매칭이 되지 않습니다. 제목·설명·카테고리·담당자에서 찾습니다.
      3) 주체가 없고 날짜만 있으면 그 기간 전체를 조회

    "점검 시작은 어느 팀 담당?" 처럼 시계열 키워드만 걸리고 날짜도 주체도
    없는 질문은 빈 dict 를 반환합니다.

    Args:
        qc:              Qdrant 클라이언트. 주면 이벤트별 원문(page_content)을
                         첨부합니다. 서비스와 평가가 같은 데이터를 보도록
                         반드시 전달하세요.
        collection_name: 원문 조회 대상 컬렉션.
        page_max_chars:  이벤트당 첨부할 원문 길이.

    Returns:
        get_event_chain() 결과 dict (+ events[].page_content).
        조건 미충족·조회 실패 시 {}.
    """
    try:
        from utils.datespan import extract_date_range, has_timeline_intent
        from utils.korean import match_nodes_in_text
    except Exception:
        return {}  # utils 미사용 환경 — 타임라인 조회 비활성화

    if not has_timeline_intent(query):
        return {}

    from_date, to_date = extract_date_range(query)
    games = detect_event_scopes(graph, query)
    game_arg: str | None = games[0] if games else None
    keywords: list[str] = []

    if not game_arg:
        # 그래프 노드 이름만 키워드로 사용합니다. 원시 토큰을 쓰면 "업무",
        # "일정" 같은 일반 명사가 필터에 들어가 무관한 이벤트를 끌어옵니다.
        keywords = [name for name, _t in match_nodes_in_text(graph, query, limit=5)]
        if not keywords and not (from_date or to_date):
            return {}

    result = get_event_chain(
        graph,
        game=game_arg,
        event_type=None,  # 질문에서 유형까지 추정하지 않음 — 전체 조회 후 LLM이 판단
        from_date=from_date or None,
        to_date=to_date or None,
        limit=limit,
        keywords=keywords or None,
    )
    if not result or not result.get("events"):
        return {}

    # ── 이벤트별 Notion 원문 첨부 ────────────────────────────────────────────
    # :Event 노드는 제목·날짜·카테고리만 담고 있어, "몇 건인가" 같은 세부는
    # 원문에만 있습니다. 첨부하지 않으면 이벤트를 찾고도 답하지 못합니다.
    # qc 가 없으면(그래프만 쓰는 호출) 조용히 건너뜁니다.
    if qc is not None and collection_name:
        urls = list({ev["source_url"] for ev in result["events"] if ev.get("source_url")})
        if urls:
            try:
                from utils.retrieval import fetch_pages_by_source_urls

                pages = fetch_pages_by_source_urls(
                    qc, collection_name, urls, max_chars=page_max_chars
                )
                for ev in result["events"]:
                    page = pages.get(ev.get("source_url", ""))
                    if page:
                        ev["page_content"] = page.get("content", "")
                        ev["page_chunk_count"] = page.get("chunk_count", 0)
            except Exception:
                pass  # 원문 첨부는 부가 정보 — 실패해도 이벤트 목록은 반환

    return result


# ─── 9. 경로 분류 (classify_page) ────────────────────────────────────────────
#
# 페이지마다 LLM 추출 전에 호출해 처리 경로를 결정합니다.
#   core     → 지식 추출 대상 (LLM 트리플·이벤트 파이프라인 전체 실행)
#   defer    → 벡터 임베딩만, LLM 추출 건너뜀 (정보 밀도 낮음)
#   excluded → 인제스천 완전 제외 (너무 짧거나 임시 문서)
#
# 결정론적 규칙 기반 — AI 호출 없음, 처리 비용 0.

_CORE_TITLE_KEYWORDS: frozenset = frozenset(
    [
        "전략",
        "기획",
        "의사결정",
        "결정",
        "승인",
        "회의록",
        "회의",
        "미팅",
        "ua",
        "마케팅",
        "예산",
        "목표",
        "kpi",
        "매출",
        "dau",
        "arpu",
        "분석",
        "인사이트",
        "보고서",
        "계획",
        "방향",
        "로드맵",
        "okr",
        "이슈",
        "리스크",
        "문제",
        "개선",
        "제안",
        "검토",
        "결론",
    ]
)
_CORE_BODY_KEYWORDS: frozenset = frozenset(
    [
        "의사결정",
        "결정함",
        "승인됨",
        "예산",
        "목표",
        "kpi",
        "전략",
        "ua",
        "마케팅",
        "매출",
        "인사이트",
        "이슈",
        "리스크",
    ]
)
_DEFER_TITLE_SIGNALS: frozenset = frozenset(
    [
        "링크 모음",
        "참고 자료",
        "자료 모음",
        "업무 연락",
        "단순 안내",
        "일정 공유",
        "todo",
        "체크리스트",
    ]
)
_EXCLUDED_TITLE_PATTERNS: frozenset = frozenset(
    [
        "테스트",
        "test",
        "임시",
        "draft",
        "삭제 예정",
        "미사용",
        "untitled",
    ]
)


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
        if word_count < 1:
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

_PLANNED_SIGNALS: frozenset = frozenset(
    [
        "예정",
        "할 예정",
        "진행 예정",
        "검토 중",
        "검토 예정",
        "계획",
        "예정입니다",
        "할 계획",
        "가능성",
        "논의 중",
        "준비 중",
        "예정으로",
        "검토하고",
        "진행할",
        "배포 예정",
        "오픈 예정",
    ]
)
_APPLIED_SIGNALS: frozenset = frozenset(
    [
        "완료",
        "적용됨",
        "배포됨",
        "출시",
        "오픈됨",
        "시행됨",
        "확정됨",
        "실시됨",
        "반영됨",
        "실행됨",
        "시작됨",
        "완료되었",
        "됩니다",
        "했습니다",
        "출시됨",
        "배포 완료",
        "오픈 완료",
        "적용 완료",
    ]
)


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
