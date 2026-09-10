"""
검색 공통 로직 — MCP 서버와 평가 파이프라인이 함께 사용
─────────────────────────────────────────────────────────────────────────────
server.py 와 evaluate.py 가 각자 구현하던 검색 로직을 한곳으로 모읍니다.

두 벌로 나뉘어 있는 동안 실제로 세 번 문제가 생겼습니다:
  · 컨텍스트 예산 불일치 — 평가가 서비스의 1/6 정보량으로 측정
  · utils 임포트 실패 — 평가만 통과해 프로덕션 결함이 가려짐
  · 튜닝 누락 — server.py 만 조정되어 평가가 다른 랭킹을 측정
      (coverage 부스트 0.20 vs 0.15, 복합 판정 12자/5단어 vs 15자/6단어,
       복합 패턴 25개 vs 14개)

이 모듈의 값이 정본입니다. 튜닝은 여기서만 하세요.

LLM·임베딩 클라이언트는 호출부가 주입합니다. MCP 서버는 API 키 방식
(anthropic.Anthropic), 평가는 Vertex(AnthropicVertex)를 쓰기 때문입니다.
"""

import json
import os
import re

# ── 검색 파라미터 (정본) ──────────────────────────────────────────────────────
# 페이지 본문 전달 한도. 초과 시 앵커 청크 중심으로 윈도우를 잡습니다.
# 4000자였을 때 검색된 근거가 전달 직전에 잘려나가 복합 카테고리가 전멸했습니다.
PAGE_MAX_CHARS: int = int(os.environ.get("PAGE_MAX_CHARS", "16000"))

# 청크 오버샘플 배수 — 청크를 page_id 로 묶으면 결과 수가 줄어드는 것을 보정합니다.
CHUNK_OVERSAMPLE: int = int(os.environ.get("CHUNK_OVERSAMPLE", "4"))

# 여러 서브쿼리에 공통으로 걸린 문서에 주는 가산율.
COVERAGE_BOOST: float = float(os.environ.get("COVERAGE_BOOST", "0.20"))

# 복합 쿼리 판정 임계값.
COMPLEX_MIN_CHARS: int = 12
COMPLEX_MIN_WORDS: int = 5

COMPLEX_PATTERNS: frozenset = frozenset(
    [
        "이고",
        "이며",
        "하는",
        "이면서",
        "이자",
        "담당하는",
        "작성한",
        "소속된",
        "승인한",
        "결정한",
        "관련된",
        "연관된",
        "포함된",
        "연결된",
        # 복합 조건을 표현하는 추가 패턴
        "중에서",
        "기준으로",
        "에서의",
        "으로의",
        "누가",
        "어느",
        "어떤 팀",
        "어떤 사람",
        "기반으로",
        "따라서",
        "통해서",
    ]
)

DECOMPOSE_PROMPT = (
    "사내 업무 문서 검색 시스템입니다. "
    "문서에는 담당자·팀·프로세스·정책·시스템·게임 서비스 정보가 담겨 있습니다.\n\n"
    "다음 복합 질문을 독립적으로 검색 가능한 서브쿼리 2~3개로 분해하세요.\n\n"
    "규칙:\n"
    "- 각 서브쿼리는 단독으로 검색해도 유의미한 10~25자 한국어 표현\n"
    "- 원본 질문의 핵심 엔티티(사람·팀·프로세스·정책·게임)를 모두 포함\n"
    "- 서로 다른 관점(담당자 관점, 문서 관점, 관계 관점)으로 분해\n"
    "- JSON 배열만 반환 (설명·마크다운 없이)\n\n"
    "예시 1:\n"
    "Q: 운영팀에서 POTC 점검을 담당하는 사람이 작성한 배포 가이드는?\n"
    'A: ["운영팀 POTC 점검 담당자", "POTC 배포 가이드 문서", "운영팀 작성 점검 절차"]\n\n'
    "예시 2:\n"
    "Q: 전략사업본부 글로벌 게임 출시 승인 절차와 관련 팀은?\n"
    'A: ["글로벌 게임 출시 승인 절차", "전략사업본부 출시 담당팀", "게임 출시 관련 정책"]\n\n'
    "질문: {query}"
)


# ── 쿼리 분해 ────────────────────────────────────────────────────────────────


def is_complex_query(query: str) -> bool:
    """복합 쿼리 여부 휴리스틱 판정.

    >>> is_complex_query("담당자는?")
    False
    >>> is_complex_query("운영팀에서 점검을 담당하는 사람은 누구인가요?")
    True
    """
    if len(query) >= COMPLEX_MIN_CHARS and any(p in query for p in COMPLEX_PATTERNS):
        return True
    return len(query.split()) >= COMPLEX_MIN_WORDS


def decompose_query(query: str, complete_fn) -> list:
    """복합 쿼리를 서브쿼리 2~4개로 분해합니다. 실패 시 [query].

    Args:
        complete_fn: prompt(str) -> 응답 텍스트(str). LLM 호출을 호출부가 주입합니다.
    """
    try:
        text = str(complete_fn(DECOMPOSE_PROMPT.format(query=query))).strip()
        # greedy 매칭 — 서브쿼리 안에 ']' 가 있어도 배열이 잘리지 않도록
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if not m:
            return [query]
        parts = [p.strip() for p in json.loads(m.group()) if isinstance(p, str) and p.strip()]
        if 2 <= len(parts) <= 4:
            return parts
    except Exception:
        pass
    return [query]


# ── 페이지 조립 ──────────────────────────────────────────────────────────────


def window_around_anchor(
    full_text: str,
    sorted_chunks: list,
    max_chars: int,
    anchor_index=None,
) -> str:
    """문서가 max_chars 를 넘을 때 남길 구간을 정합니다.

    anchor_index(벡터 검색에 걸린 chunk_index)가 주어지면 그 청크가 반드시
    포함되도록 윈도우를 잡습니다. 앞부분만 남기면 문서 뒤쪽에서 찾아낸 근거를
    그대로 버리게 됩니다 — 검색해놓고 전달 직전에 잃는 셈입니다.
    """
    if len(full_text) <= max_chars:
        return full_text
    if anchor_index is None:
        return full_text[:max_chars]

    # 앵커 청크의 시작 오프셋 (조립 시 "\n\n" 로 이었으므로 그만큼 가산)
    offset = 0
    for c in sorted_chunks:
        if c["index"] == anchor_index:
            break
        offset += len(c["text"]) + 2
    else:
        return full_text[:max_chars]

    start = max(0, offset - max_chars // 3)  # 앞쪽 1/3 은 선행 문맥
    end = min(len(full_text), start + max_chars)
    start = max(0, end - max_chars)  # 끝에 닿았으면 앞으로 당겨 예산을 모두 사용

    piece = full_text[start:end]
    if start > 0:
        piece = "…(앞부분 생략)…\n\n" + piece
    if end < len(full_text):
        piece = piece + "\n\n…(뒷부분 생략)…"
    return piece


def _assemble(pages: dict, max_chars: int, anchors: dict) -> dict:
    """{key: {title, source_url, page_id, chunks}} → 조립된 본문 dict."""
    out: dict = {}
    for key, page in pages.items():
        ordered = sorted(page["chunks"], key=lambda c: c["index"])
        full = "\n\n".join(c["text"] for c in ordered)
        out[key] = {
            "title": page["title"],
            "source_url": page["source_url"],
            "page_id": page.get("page_id", ""),
            "content": window_around_anchor(full, ordered, max_chars, anchors.get(key)),
            "chunk_count": len(ordered),
            "total_chars": len(full),
            "truncated": len(full) > max_chars,
        }
    return out


def _scroll_chunks(qc, collection_name: str, key_field: str, values: list) -> dict:
    """key_field 가 values 에 속하는 모든 청크를 모아 키별로 묶습니다."""
    from qdrant_client.models import FieldCondition, Filter, MatchAny

    pages: dict = {}
    offset = None
    while True:
        rows, offset = qc.scroll(
            collection_name=collection_name,
            scroll_filter=Filter(
                must=[FieldCondition(key=key_field, match=MatchAny(any=list(values)))]
            ),
            limit=500,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for point in rows:
            p = point.payload or {}
            key = p.get(key_field, "")
            if not key:
                continue
            if key not in pages:
                pages[key] = {
                    "title": p.get("title", ""),
                    "source_url": p.get("source_url", ""),
                    "page_id": p.get("page_id", ""),
                    "chunks": [],
                }
            pages[key]["chunks"].append(
                {"index": p.get("chunk_index", 9999), "text": p.get("text", "")}
            )
        if offset is None:
            break
    return pages


def fetch_full_pages(
    qc,
    collection_name: str,
    page_ids: list,
    max_chars: int = PAGE_MAX_CHARS,
    anchors: dict | None = None,
) -> dict:
    """page_id 목록의 전체 본문을 조립해 반환합니다 (Parent Document Retrieval).

    Args:
        anchors: {page_id: chunk_index} — 잘라야 할 때 포함시킬 기준 청크.

    Returns:
        {page_id: {title, source_url, page_id, content, chunk_count,
                   total_chars, truncated}}
    """
    if not page_ids:
        return {}
    return _assemble(
        _scroll_chunks(qc, collection_name, "page_id", page_ids), max_chars, anchors or {}
    )


def fetch_pages_by_source_urls(
    qc,
    collection_name: str,
    source_urls: list,
    max_chars: int = PAGE_MAX_CHARS,
) -> dict:
    """source_url 목록의 전체 본문을 조립해 반환합니다.

    그래프에서 얻은 출처 URL로 벡터 DB의 원문을 바로 꺼낼 때 사용합니다
    (그래프→벡터 명시적 크로스링킹).

    Returns:
        {source_url: {title, source_url, page_id, content, ...}}
    """
    if not source_urls:
        return {}
    return _assemble(_scroll_chunks(qc, collection_name, "source_url", source_urls), max_chars, {})


# ── 벡터 검색 ────────────────────────────────────────────────────────────────


def vector_search_pages(
    qc,
    collection_name: str,
    vector: list,
    limit: int,
    max_chars: int = PAGE_MAX_CHARS,
    oversample: int = CHUNK_OVERSAMPLE,
) -> list:
    """청크로 검색한 뒤 페이지 단위로 묶어 전문을 반환합니다.

    limit 은 **페이지** 수입니다. 청크를 page_id 로 묶으면 결과가 줄어들므로
    (한 페이지가 상위 청크를 독점하면 1건만 남음) 청크는 oversample 배로
    조회한 뒤 상위 limit 개 페이지만 조립합니다.

    Returns:
        [{title, source_url, content, chunk_count, score, ...}] — score 내림차순
    """
    hits = qc.query_points(
        collection_name=collection_name,
        query=vector,
        limit=max(limit * oversample, limit),
        with_payload=True,
    )

    page_scores: dict = {}
    anchors: dict = {}
    for h in hits.points:
        p = h.payload or {}
        pid = p.get("page_id", "")
        score = round(h.score, 4)
        if pid and (pid not in page_scores or score > page_scores[pid]):
            page_scores[pid] = score
            anchors[pid] = p.get("chunk_index", 0)

    top_pids = sorted(page_scores, key=lambda k: page_scores[k], reverse=True)[:limit]
    full_pages = fetch_full_pages(qc, collection_name, top_pids, max_chars, anchors)

    result = [{**page, "score": page_scores.get(pid, 0.0)} for pid, page in full_pages.items()]
    result.sort(key=lambda x: x["score"], reverse=True)
    return result


def merge_semantic_results(results_per_query: list, boost: float = COVERAGE_BOOST) -> list:
    """서브쿼리별 결과를 source_url 기준으로 병합하고 coverage 로 재랭킹합니다.

    여러 서브쿼리에 공통으로 등장한 문서일수록 질문 전체와 관련이 높다고 보고
    가산합니다: score * (1 + boost * (coverage - 1))
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

    merged = [
        {
            **item,
            "coverage": url_counts[url],
            "score": round(item["score"] * (1 + boost * (url_counts[url] - 1)), 4),
        }
        for url, item in url_best.items()
    ]
    return sorted(merged, key=lambda x: x["score"], reverse=True)


# ── 그래프 엔티티 탐색 ────────────────────────────────────────────────────────


def find_entities_in_query(graph, query: str, limit: int = 5) -> list:
    """질문 문장에 등장하는 그래프 노드 이름을 찾습니다.

    ① 역방향 매칭($text CONTAINS n.name) — 한국어 조사와 무관하게 동작
    ② 실패 시 조사를 제거한 토큰으로 부분 일치

    Returns:
        노드 이름 목록 (긴 이름 우선). 실패 시 빈 리스트.
    """
    from utils.korean import entity_candidates, match_nodes_in_text

    names = [name for name, _t in match_nodes_in_text(graph, query, limit=limit)]
    if names:
        return names

    seen: set = set()
    out: list = []
    for word in entity_candidates(query, limit=6):
        try:
            res = graph.query(
                "MATCH (n) WHERE n.name CONTAINS $name RETURN n.name LIMIT 2",
                {"name": word},
            )
        except Exception:
            continue
        for row in res.result_set:
            if row and row[0] and str(row[0]) not in seen:
                seen.add(str(row[0]))
                out.append(str(row[0]))
    return out[:limit]
