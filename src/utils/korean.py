"""
한국어 엔티티 매칭 유틸
─────────────────────────────────────────────────────────────────────────────
그래프 검색에서 질문 문장과 노드 이름을 매칭할 때 조사(은/는/이/가/와/과 …)
때문에 발생하는 매칭 실패를 해결합니다.

문제:
    질문 "데사실은 어느 부서와 협업 관계인가요?"
    → .split() → ["데사실은", "어느", "부서와", ...]
    → MATCH (n) WHERE n.name CONTAINS "데사실은"
    → 노드 이름은 "데사실" 이므로 매칭 실패 (조사 "은" 이 붙어 있음)

해법 2가지를 함께 사용합니다:
    1) strip_particle()      — 토큰 뒤 조사를 떼어 후보를 늘림
    2) 역방향 CONTAINS 매칭  — 질문 문장이 노드 이름을 포함하는지 확인
                               (NODE_IN_TEXT_QUERY)

역방향 매칭이 더 견고합니다. 조사가 무엇이든 노드 이름 "데사실" 은
원문 "데사실은 …" 안에 그대로 들어 있기 때문입니다.
"""

# 조사·어미 목록 — 긴 것부터 매칭해야 "에서"를 "서"로 잘못 처리하지 않습니다.
_PARTICLES: tuple[str, ...] = (
    "에게서",
    "이라도",
    "으로써",
    "으로서",
    "에서는",
    "에게는",
    "이라는",
    "라는",
    "에서",
    "에게",
    "한테",
    "으로",
    "처럼",
    "만큼",
    "보다",
    "부터",
    "까지",
    "조차",
    "마저",
    "밖에",
    "이나",
    "이랑",
    "하고",
    "같이",
    "께서",
    "이란",
    "라도",
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "의",
    "에",
    "와",
    "과",
    "도",
    "만",
    "로",
    "랑",
    "께",
)

# 조사를 떼고 남는 최소 길이 — 1글자 후보는 오탐이 너무 많습니다.
_MIN_STEM = 2


def contains_as_token(text: str, name: str) -> bool:
    """text 안에 name 이 '독립된 토큰'으로 등장하는지 확인합니다.

    영문·숫자 코드는 앞뒤가 영숫자면 우연한 부분 문자열이므로 제외합니다.
    게임 코드에 ONE·GOD·DS 처럼 짧은 것이 있어 단순 CONTAINS 는 오탐이 납니다.

    한글은 조사가 바로 뒤에 붙어 경계 판정이 불가능하므로 그대로 통과시킵니다
    (조사 처리는 strip_particle / 역방향 매칭이 담당).

    >>> contains_as_token("MILESTONE 달성", "ONE")
    False
    >>> contains_as_token("ONE 업데이트", "ONE")
    True
    >>> contains_as_token("POTC-2026 패치", "POTC")
    True
    >>> contains_as_token("데사실은 어느 부서와", "데사실")
    True
    """
    if not name or not text:
        return False
    # 순수 ASCII 영숫자(하이픈 허용) 코드만 경계 검사 — 그 외(한글 등)는 통과
    probe = name.replace("-", "").replace("_", "")
    if not (probe.isascii() and probe.isalnum()):
        return name in text

    start = 0
    while True:
        idx = text.find(name, start)
        if idx < 0:
            return False
        before = text[idx - 1] if idx > 0 else " "
        after = text[idx + len(name)] if idx + len(name) < len(text) else " "
        if not (before.isalnum() or after.isalnum()):
            return True
        start = idx + 1


def node_in_text_query(limit: int = 30) -> str:
    """질문 문장에 이름이 등장하는 노드를 찾는 역방향 매칭 Cypher.

    $text 파라미터에 질문 원문을 넘깁니다.
    길이 필터·정렬은 FalkorDB의 size() 지원 여부에 의존하지 않도록
    호출부(Python)에서 처리합니다 — match_nodes_in_text() 참고.
    """
    return (
        "MATCH (n) WHERE n.name IS NOT NULL AND $text CONTAINS n.name "
        f"RETURN n.name AS name, labels(n)[0] AS type LIMIT {int(limit)}"
    )


def match_nodes_in_text(graph, text: str, limit: int = 5) -> list[tuple[str, str]]:
    """질문 문장에 등장하는 그래프 노드를 찾습니다 (조사 무관).

    긴 이름을 우선 반환합니다 — "데이터사이언스실"이 "데이터"보다 구체적입니다.

    Returns:
        [(name, type), ...] — 실패 시 빈 리스트
    """
    try:
        res = graph.query(node_in_text_query(), {"text": text})
    except Exception:
        return []

    rows = [
        (str(r[0]), str(r[1]) if len(r) > 1 and r[1] else "")
        for r in res.result_set
        if r
        and r[0]
        and len(str(r[0])) >= _MIN_STEM
        # 영문 코드가 다른 단어에 우연히 포함된 경우 제외 (ONE ⊄ MILESTONE)
        and contains_as_token(text, str(r[0]))
    ]
    rows.sort(key=lambda x: len(x[0]), reverse=True)
    return rows[:limit]


def strip_particle(word: str) -> str:
    """토큰 끝의 조사를 1회 제거합니다. 조사가 없으면 원본을 반환합니다.

    >>> strip_particle("데사실은")
    '데사실'
    >>> strip_particle("빅쿼리와")
    '빅쿼리'
    >>> strip_particle("분석서버")
    '분석서버'
    """
    for p in _PARTICLES:
        if word.endswith(p) and len(word) - len(p) >= _MIN_STEM:
            return word[: -len(p)]
    return word


def entity_candidates(text: str, limit: int = 6) -> list[str]:
    """질문 문장에서 그래프 노드 매칭용 후보 토큰을 추출합니다.

    원본 토큰과 조사 제거 토큰을 모두 포함하므로
    조사 제거가 잘못된 경우에도 원본으로 매칭될 수 있습니다.

    >>> entity_candidates("데사실은 어느 부서와 협업 관계에 있나요?")
    ['데사실은', '데사실', '어느', '부서와', '부서', '협업']
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in text.split():
        # 문장부호 제거 (조사 판정을 방해함) — 유니코드 인용부호는 의도적으로 포함
        tok = raw.strip("?!.,;:()[]{}\"'“”‘’·…")
        if len(tok) < _MIN_STEM:
            continue
        for cand in (tok, strip_particle(tok)):
            if len(cand) >= _MIN_STEM and cand not in seen:
                seen.add(cand)
                out.append(cand)
        if len(out) >= limit:
            break
    return out[:limit]
