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
#
# 8 로 확정 (2026-09-10). 근거: limit=10(운영값)에서 oversample 8 로 검색했을 때
# 40문항 A/B 의 벡터 의존 30문항 **전부**(100%)가 근거 페이지를 컨텍스트에
# 받았습니다. Q03·Q08 은 4 배 설정에서 "검색 실패 — 임베딩·청킹 문제"로 잡히던
# 문항인데, 청크 풀만 넓히니 회복됐습니다. 임베딩 문제가 아니었습니다.
#
# 100% 는 상한이므로 4 가 얼마든 8 이 그보다 나쁠 수 없습니다. 비용은
# Qdrant top-k 가 40 → 80 청크로 커지는 것뿐입니다 — 반환 **페이지** 수는
# limit 이 정하므로(DEFAULT_PAGE_LIMIT) 페이지 조립 비용은 그대로입니다.
#
# ※ 이전 주석의 "1배 57.5% / 2배 70.0% / 4배 77.5%" 는 무효였습니다. 측정
#   도구가 oversample 을 낮추는 대신 결과 페이지 수를 잘라, 실제로는
#   "페이지를 몇 개 넘기느냐"를 재고 있었습니다. 값을 바꾸려면 반드시
#   수정된 tools/ab_retrieval.py 로 재측정하세요 (12·16 설정 포함).
CHUNK_OVERSAMPLE: int = int(os.environ.get("CHUNK_OVERSAMPLE", "8"))

# 여러 서브쿼리에 공통으로 걸린 문서에 주는 가산율.
# A/B 측정상 0.0 / 0.10 / 0.20 모두 근거 포함률이 같았습니다 — 예산이 찰
# 때까지 채우므로 순위가 조금 달라져도 결국 포함되기 때문입니다.
# (이 비교는 같은 oversample 끼리라 위 결함의 영향을 받지 않습니다.)
# 답변 품질에는 영향이 있을 수 있어 현행값을 유지합니다.
COVERAGE_BOOST: float = float(os.environ.get("COVERAGE_BOOST", "0.20"))

# near-duplicate 제거 — **기본 비활성(1.0)**.
# 같은 문서의 여러 버전이 컨텍스트를 중복 점유하는 문제를 노렸으나,
# A/B 측정 결과 근거 포함률이 **떨어졌습니다** (동일 oversample 비교).
# 0.85 임계값에서 Q03·Q05, 그리고 정작 고치려던 Q36 까지 잃었습니다 —
# 유사해 보이는 문서도 세부가 다르고 답은 그 세부에 있으며, 근거가 중복
# 그룹의 최상위가 아니면 지워지기 때문입니다.
# 파이프라인에는 **연결되어 있지 않습니다** — dedupe_documents 를 호출하는 곳은
# tools/ab_retrieval.py 뿐이라, 이 환경변수만 바꿔서는 아무것도 달라지지 않습니다.
# 실제로 켜려면 merge_semantic_results 소비자 쪽에서 호출을 추가해야 하며,
# 그 전에 반드시 tools/ab_retrieval.py 로 재측정하세요.
NEAR_DUP_THRESHOLD: float = float(os.environ.get("NEAR_DUP_THRESHOLD", "1.0"))
NEAR_DUP_PREFIX: int = int(os.environ.get("NEAR_DUP_PREFIX", "600"))

# 서브쿼리당 가져올 페이지 수. 평가와 서비스가 같은 값을 써야 골든셋 점수가
# 실제 서비스 결과를 뜻합니다 (이전: 평가 10 / 서비스 12).
DEFAULT_PAGE_LIMIT: int = int(os.environ.get("RETRIEVE_LIMIT", "10"))

# 쿼리 분해용 모델 — 평가와 서비스가 반드시 같아야 합니다. 분해는 검색의 첫
# 단계라 여기서 갈리면 이후 모든 결과가 갈립니다 (이전: 서비스 Haiku / 평가
# Sonnet). 서비스는 Anthropic API, 평가는 Vertex 라 모델 ID 표기만 다릅니다.
DECOMPOSE_MODEL_API: str = os.environ.get("DECOMPOSE_MODEL_API", "claude-haiku-4-5-20251001")
DECOMPOSE_MODEL_VERTEX: str = os.environ.get("DECOMPOSE_MODEL_VERTEX", "claude-haiku-4-5@20251001")

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


def search_queries(query: str, complete_fn) -> tuple[list, bool]:
    """검색에 사용할 쿼리 목록과 분해 여부를 반환합니다.

    분해된 경우 **원본 질문을 맨 앞에 함께 포함**합니다. 분해 과정에서 긴 엔티티
    이름이 축약되면(예: "ADNW/빅미디어 규모-효율 진단" → "ADNW 빅미디어 진단")
    그래프 노드 매칭($text CONTAINS n.name)이 깨져, 원본에는 온전히 들어 있는
    이름을 쓰지 못하게 됩니다.

    부수 효과로 원본 질문에 걸린 문서는 coverage 가 1 늘어 재랭킹에서 우대됩니다.

    Returns:
        (검색 쿼리 목록, 분해 여부)
    """
    if not is_complex_query(query):
        return [query], False
    subs = decompose_query(query, complete_fn)
    if len(subs) <= 1:
        return [query], False
    return [query, *subs], True


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

    anchor_index 는 벡터 검색에 걸린 chunk_index 하나 또는 **여러 개**(점수
    내림차순)입니다. 검색에 걸린 청크가 최대한 많이 들어가는 윈도우를 고릅니다.
    앞부분만 남기면 문서 뒤쪽에서 찾아낸 근거를 그대로 버리게 됩니다 —
    검색해놓고 전달 직전에 잃는 셈입니다.

    여러 개를 받는 이유: 예전에는 **최고점 청크 하나**만 기준으로 삼았습니다.
    앞쪽 청크가 최고점이고 정작 근거가 뒤쪽에 있으면 그대로 잘렸습니다.
    실측 — 24,774자 문서에서 근거는 청크 20·21 인데 앵커가 청크 0 이라
    윈도우 밖으로 밀려 0.0 점을 받았습니다.
    """
    if len(full_text) <= max_chars:
        return full_text
    if anchor_index is None:
        return full_text[:max_chars]

    anchors = [anchor_index] if isinstance(anchor_index, int) else list(anchor_index or [])
    if not anchors:
        return full_text[:max_chars]

    # 각 청크의 [시작, 끝) 오프셋 (조립 시 "\n\n" 로 이었으므로 그만큼 가산)
    spans: dict = {}
    off = 0
    for c in sorted_chunks:
        spans[c["index"]] = (off, off + len(c["text"]))
        off += len(c["text"]) + 2

    targets = [spans[i] for i in anchors if i in spans]
    if not targets:
        return full_text[:max_chars]

    # 매칭 청크를 가장 많이 담는 윈도우를 선택합니다. 동률이면 점수가 높은
    # 앵커(= anchors 의 앞쪽)를 기준으로 한 것이 먼저 선택됩니다.
    best_start, best_cover = None, -1
    for s, _e in targets:
        start = max(0, s - max_chars // 3)  # 앞쪽 1/3 은 선행 문맥
        end = min(len(full_text), start + max_chars)
        start = max(0, end - max_chars)  # 끝에 닿았으면 앞으로 당겨 예산을 모두 사용
        cover = sum(1 for ts, te in targets if ts >= start and te <= end)
        if cover > best_cover:
            best_start, best_cover = start, cover

    start = best_start or 0
    end = min(len(full_text), start + max_chars)

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
            # 검색에 걸린 청크 — 진단 도구가 "앵커가 근거 청크인가"를 판정합니다.
            "anchor_index": anchors.get(key),
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
    page_hits: dict = {}
    for h in hits.points:
        p = h.payload or {}
        pid = p.get("page_id", "")
        if not pid:
            continue
        score = round(h.score, 4)
        # 페이지당 **걸린 청크를 모두** 모읍니다. 최고점 하나만 앵커로 쓰면
        # 근거가 뒤쪽 청크에 있을 때 윈도우 밖으로 밀려납니다.
        page_hits.setdefault(pid, []).append((score, p.get("chunk_index", 0)))
        if pid not in page_scores or score > page_scores[pid]:
            page_scores[pid] = score

    anchors = {
        pid: [ci for _s, ci in sorted(hs, key=lambda x: x[0], reverse=True)]
        for pid, hs in page_hits.items()
    }

    top_pids = sorted(page_scores, key=lambda k: page_scores[k], reverse=True)[:limit]
    full_pages = fetch_full_pages(qc, collection_name, top_pids, max_chars, anchors)

    result = [{**page, "score": page_scores.get(pid, 0.0)} for pid, page in full_pages.items()]
    result.sort(key=lambda x: x["score"], reverse=True)
    return result


def _ngrams(text: str, n: int = 3) -> set:
    """문자 n-gram 집합 — near-duplicate 판정용."""
    t = "".join(text.split())  # 공백 차이는 무시
    return {t[i : i + n] for i in range(len(t) - n + 1)} if len(t) >= n else {t}


def content_similarity(a: str, b: str, prefix: int = NEAR_DUP_PREFIX) -> float:
    """두 본문의 앞부분 n-gram Jaccard 유사도 (0.0~1.0).

    전문을 비교하면 비용이 크고, 같은 문서의 다른 버전은 대개 앞부분이
    동일하므로 앞 prefix 자만 봅니다.
    """
    ga, gb = _ngrams(a[:prefix]), _ngrams(b[:prefix])
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def dedupe_documents(
    docs: list,
    threshold: float = NEAR_DUP_THRESHOLD,
    prefix: int = NEAR_DUP_PREFIX,
) -> list:
    """내용이 거의 같은 문서를 제거합니다 (점수 상위를 보존).

    같은 문서의 여러 버전이 Notion 에 남아 있으면 컨텍스트를 중복 점유해
    다른 근거가 들어갈 자리를 빼앗습니다. 실측 사례: 상위 6건이 모두
    "점검 진행 프로세스"라는 같은 제목의 유사 문서였습니다.

    docs 는 점수 내림차순으로 정렬되어 있다고 가정합니다.
    """
    if threshold >= 1.0 or len(docs) < 2:
        return list(docs)

    kept: list = []
    for d in docs:
        body = str(d.get("content", ""))
        if any(
            content_similarity(body, str(k.get("content", "")), prefix) >= threshold for k in kept
        ):
            continue
        kept.append(d)
    return kept


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


def rank_entity_matches(rows: list, forms: list) -> list:
    """부분 일치로 걸린 노드들을 "찾던 것에 가까운 순"으로 정렬합니다.

    graph_search 는 `n.name CONTAINS form` 으로 노드를 찾은 뒤 **그중 하나**의
    관계만 탐색합니다. 그런데 부분 일치는 엉뚱한 것을 많이 답니다 — 실측으로
    "RESU" 는 아래 전부에 걸렸습니다:

        RESU                                              엣지 12   ← 찾던 것
        RESU Live History DB                              엣지  2
        RESU 빌드 알림 채널                                엣지  2
        RESU 기획팀                                       엣지  1
        data-science-division-216308.RESU.server_opentime 엣지  2

    Cypher 에 ORDER BY 가 없으면 어느 것이 첫 줄로 오는지는 정해져 있지 않으므로,
    같은 질문이 실행마다 다른 답을 낼 수 있었습니다. 엣지 12개짜리 본체 대신
    2개짜리 테이블을 잡으면 관계가 통째로 사라집니다.

    또 같은 이름이 여러 라벨로 쪼개져 있기도 합니다 (실측 24건). 예를 들어
    "RESU" 는 Game(엣지 0) · Team(2) · System(12) · Process(2) 네 노드입니다.
    첫 줄만 쓰면 엣지 0개짜리를 잡고 "관계 없음"이 될 수 있습니다.

    정렬 기준 (앞선 것이 우선):
        ① 표기를 지운 이름이 검색어와 **완전히 같은가**
        ② 검색어로 **시작**하는가        ("RESU 기획팀" < "…216308.RESU.…")
        ③ 엣지가 많은가                  실제로 정보를 가진 노드
        ④ 이름이 짧은가 → 이름 → 라벨    동점 시 결과를 고정하기 위한 기준

    Args:
        rows:  [(name, label, degree), ...]
        forms: 동의어 확장된 검색어 목록

    Returns:
        정렬된 [(name, label, degree), ...]

    이름이 같은 것들이 먼저 오고(라벨이 갈려 있어도), 그 안에서 엣지가 많은
    순입니다. 그 뒤가 접두 일치입니다.

    >>> rows = [("RESU Live History DB", "System", 2), ("RESU", "System", 12),
    ...         ("RESU", "Game", 0), ("RESU 기획팀", "Team", 1)]
    >>> [(n, d) for n, _l, d in rank_entity_matches(rows, ["RESU"])]
    [('RESU', 12), ('RESU', 0), ('RESU Live History DB', 2), ('RESU 기획팀', 1)]
    """
    from utils.synonym_resolver import norm_key

    keys = [k for k in (norm_key(f) for f in forms) if k]

    def _tier(name: str) -> int:
        nk = norm_key(name)
        if any(nk == k for k in keys):
            return 0
        if any(nk.startswith(k) for k in keys):
            return 1
        return 2

    return sorted(
        rows,
        key=lambda r: (_tier(r[0]), -(r[2] or 0), len(r[0]), r[0], r[1] or ""),
    )


def _entity_candidates(graph, forms: list) -> list:
    """검색어가 이름에 포함된 노드를 [(name, label, degree), ...] 로 가져옵니다.

    **대소문자를 구분하지 않습니다.** Cypher 의 CONTAINS 는 구분하기 때문에
    "In-Joy" 로 찾으면 "IN-JOY" 노드는 결과에 들어오지도 않습니다 — 표기가
    갈린 노드를 합치려 해도 한쪽이 보이지 않으면 합칠 수가 없습니다.

    toLower() 를 먼저 시도하고, FalkorDB 가 거부하면 이름을 전부 받아 파이썬에서
    거릅니다. 어차피 CONTAINS 도 전체를 훑으므로 서버 쪽 일의 양은 비슷하고,
    노드 수가 적어(실측 768개) 전송량도 문제가 되지 않습니다.
    """
    lowered = [f.lower() for f in forms if f]
    if not lowered:
        return []

    try:
        rows = (
            graph.query(
                "MATCH (n) WHERE n.name IS NOT NULL "
                "OPTIONAL MATCH (n)-[r:REL]-() "
                "WITH n.name AS name, labels(n)[0] AS lbl, count(r) AS deg "
                "WHERE ANY(f IN $forms WHERE toLower(name) CONTAINS f) "
                "RETURN name, lbl, deg",
                {"forms": lowered},
            ).result_set
            or []
        )
        return [(r[0], r[1] or "", r[2] or 0) for r in rows]
    except Exception:
        pass

    try:
        rows = (
            graph.query(
                "MATCH (n) WHERE n.name IS NOT NULL "
                "OPTIONAL MATCH (n)-[r:REL]-() "
                "RETURN n.name, labels(n)[0], count(r)"
            ).result_set
            or []
        )
    except Exception:
        return []
    return [
        (r[0], r[1] or "", r[2] or 0)
        for r in rows
        if r[0] and any(f in str(r[0]).lower() for f in lowered)
    ]


def lookup_entity(graph, forms: list) -> tuple:
    """검색어에 해당하는 그래프 노드를 고릅니다.

    같은 대상이 그래프에서 여러 노드로 쪼개져 있습니다 (실측 768개 노드 기준):

      · 라벨만 다른 경우 24건 — "RESU" 가 Game(엣지 0)·Team(2)·System(12)·
        Process(2) 네 노드. 이름이 같으므로 이름으로 조회하면 자연히 합쳐집니다.
      · 표기만 다른 경우 3건 — "IN-JOY" 와 "In-Joy", "마케팅사이언스팀" 과
        "마케팅 사이언스팀". 이름이 다르므로 한쪽만 조회하면 나머지 엣지를
        통째로 놓칩니다.

    그래서 대표 이름 하나가 아니라 **같은 것으로 판정된 이름 전체**를 돌려주고,
    호출부가 `n.name IN $names` 로 한꺼번에 조회하게 합니다. 노드를 병합하거나
    지우지 않습니다 — 되돌릴 수 없는 작업을 할 만큼의 이득이 없고, 재인제스트가
    같은 중복을 다시 만들기 때문에 근본 해결도 아닙니다.

    Cypher LIMIT 을 쓰지 않는 이유: CONTAINS 는 어차피 전체를 훑으므로 LIMIT 이
    일을 줄이지 못하고, **정렬 전에 자르면** 정작 필요한 노드가 잘려나갑니다.

    Returns:
        (name, type, types, names) — 못 찾으면 ("", "", [], []).
        name  대표 이름 (보고용)
        type  대표 라벨
        types 같은 것으로 묶인 노드들의 라벨 전체
        names 같은 것으로 묶인 이름 전체 — 관계 조회에 이걸 쓰세요
    """
    from utils.synonym_resolver import norm_key

    rows = _entity_candidates(graph, forms)
    if not rows:
        return ("", "", [], [])

    ranked = rank_entity_matches(rows, forms)
    if not ranked:
        return ("", "", [], [])

    # 대표와 **표기만 다른** 것들까지 한 묶음으로. 부분 일치로 걸린 다른 노드
    # ("RESU Live History DB" 등)는 별개 대상이므로 포함하지 않습니다.
    key = norm_key(ranked[0][0])
    group = [(n, lbl, d) for n, lbl, d in ranked if norm_key(n) == key]
    return (
        ranked[0][0],
        ranked[0][1],
        sorted({lbl for _n, lbl, _d in group if lbl}),
        sorted({n for n, _l, _d in group}),
    )


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
