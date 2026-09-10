"""
JoyCity 비즈니스 용어집 기반 동의어 해결기
─────────────────────────────────────────────────────────────────────────────
https://catalog.joycityplay.com/api/glossary/all 에서 전체 용어집을 로드하여
엔티티 이름을 canonical form(term)으로 정규화하거나 동의어 전체로 확장합니다.

  resolve(name)            → canonical term. 미등록이면 원본 반환.
  expand(name)             → [canonical, synonym1, ...]. 미등록이면 [name].
  category_of(name)        → "game" | "organization" | "KPI" | ... 미등록이면 "".
  resolve_in(name, cat)    → 해당 카테고리 안에서만 canonical. 없으면 "".
  terms_in(cat)            → 카테고리의 canonical term 목록.
  preload()                → 인제스트·서버 시작 시 명시적 사전 로드.

API 구조 (catalog.joycityplay.com):
  GET /api/glossary/all
  {
    "count": 42,
    "terms": [
      {
        "id": 7,
        "term": "POTC",                             ← canonical
        "synonyms": ["캐리비안의 해적", "해적"],       ← aliases
        "category": "game",                         ← 용어 분류
        "definition": "...",
        ...
      },
      ...
    ]
  }

  GET /api/glossary/category?category=game
    → /all 응답에 category 가 없는 경우의 폴백. 응답에 all_categories 포함.

인증 불필요. term 이 canonical, synonyms 가 대안 표현, category 가 분류.
카테고리는 이벤트 주체가 게임인지 조직인지 판정하는 근거로 사용합니다
(semantica_helper.classify_scope 참고).

오프라인 폴백:
  API 접근이 불가능한 환경(운영 VM 등)에서는 config/glossary_snapshot.json
  을 대신 읽습니다. 용어집이 아예 없으면 동의어 정규화와 주체 판정이 모두
  꺼지므로, 스냅샷은 접근 가능한 환경에서 갱신해 커밋해 두어야 합니다:
      python tools/fetch_glossary_snapshot.py
  경로는 GLOSSARY_SNAPSHOT 환경변수로 바꿀 수 있습니다.
"""

import json
import logging
import os
import threading
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

GLOSSARY_URL: str = os.environ.get(
    "GLOSSARY_API_URL",
    "https://catalog.joycityplay.com/api/glossary/all",
)
# 카테고리별 조회 엔드포인트 — /all 응답에 category 가 없을 때만 폴백으로 사용
GLOSSARY_CATEGORY_URL: str = os.environ.get(
    "GLOSSARY_CATEGORY_API_URL",
    "https://catalog.joycityplay.com/api/glossary/category",
)
# 폴백 시 조회할 카테고리 (이벤트 주체 분류에 필요한 것만)
_FALLBACK_CATEGORIES: tuple[str, ...] = ("game", "organization")

# 오프라인 폴백 스냅샷 — 용어집 API에 접근할 수 없는 환경(운영 VM 등)에서 사용.
# tools/fetch_glossary_snapshot.py 로 갱신합니다.
_SNAPSHOT_PATH: Path = Path(
    os.environ.get(
        "GLOSSARY_SNAPSHOT",
        str(Path(__file__).parent.parent.parent / "config" / "glossary_snapshot.json"),
    )
)

_TTL: float = 3600.0  # 1시간 캐시
_HTTP_TIMEOUT: float = float(os.environ.get("GLOSSARY_TIMEOUT", "5"))
# 로드 실패 후 재시도까지의 최소 간격. 이것이 없으면 캐시가 비어 있는 동안
# resolve()/resolve_in() 호출마다 네트워크 재시도가 일어나, 이벤트 단위로
# 호출되는 인제스트가 타임아웃마다 멈춥니다.
_FAIL_RETRY: float = 120.0
# 연속 실패가 이 횟수에 도달하면 해당 프로세스에서는 더 시도하지 않습니다.
# (용어집 서버에 접근할 수 없는 환경에서 인제스트 전체가 지연되는 것을 방지)
_MAX_FAILS: int = 3
# _MAX_FAILS 도달 후의 재시도 간격. 영구 포기 대신 아주 드물게만 시도합니다 —
# 장기 실행 프로세스가 부팅 직후의 일시적 장애 때문에 계속 꺼져 있지 않도록.
_GIVEUP_RETRY: float = 1800.0
_fail_count: int = 0
_last_attempt: float = -1e9
# 여러 스레드가 동시에 같은 로드를 수행하지 않도록 보호합니다.
_load_lock = threading.Lock()

# {alias/term → canonical_term}
_alias_map: dict[str, str] = {}
# {canonical_term → [canonical_term, synonym1, ...]}
_expand_map: dict[str, list[str]] = {}
# {canonical_term → category}  예: {"POTC": "game", "데이터사이언스실": "organization"}
_category_of: dict[str, str] = {}
# {category → {alias/term → canonical_term}}  카테고리 한정 조회용
_by_category: dict[str, dict[str, str]] = {}
_loaded_at: float = 0.0


def _index_terms(terms: list, alias: dict, expand_m: dict, cat_of: dict, by_cat: dict) -> None:
    """term 목록을 alias/expand/category 인덱스에 반영합니다."""
    for entry in terms:
        if not isinstance(entry, dict) or not entry.get("term"):
            continue
        canonical: str = entry["term"]
        synonyms: list[str] = entry.get("synonyms") or []
        all_forms: list[str] = [canonical, *synonyms]

        # 모든 표현 → canonical
        for form in all_forms:
            alias[form] = canonical

        # canonical → 모든 표현 (검색 확장용)
        expand_m[canonical] = all_forms

        # 카테고리 인덱스 — "이 용어가 게임인가 조직인가"를 판정하는 근거
        category = str(entry.get("category") or "").strip()
        if category:
            cat_of[canonical] = category
            bucket = by_cat.setdefault(category, {})
            for form in all_forms:
                bucket[form] = canonical


def _load_from_snapshot() -> bool:
    """오프라인 스냅샷 파일에서 사전을 로드합니다. 성공하면 True.

    용어집 API에 접근할 수 없는 환경에서도 동의어 정규화와 게임/조직 판정이
    동작하도록 하는 폴백입니다. 파일이 없거나 비어 있으면 False 를 반환하고
    호출부가 기존 실패 처리를 이어갑니다.
    """
    global _alias_map, _expand_map, _category_of, _by_category, _loaded_at

    try:
        if not _SNAPSHOT_PATH.exists():
            return False
        data = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("용어집 스냅샷 읽기 실패 (%s): %s", _SNAPSHOT_PATH, exc)
        return False

    alias: dict[str, str] = {}
    expand_m: dict[str, list[str]] = {}
    cat_of: dict[str, str] = {}
    by_cat: dict[str, dict[str, str]] = {}
    _index_terms(data.get("terms", []), alias, expand_m, cat_of, by_cat)
    if not alias:
        return False

    # _alias_map 은 "로드됨" 판정 기준이므로 맨 마지막에 대입합니다 (_load 와 동일).
    _expand_map = expand_m
    _category_of = cat_of
    _by_category = by_cat
    # TTL 을 적용해 이후 API 재시도 기회를 남깁니다.
    _loaded_at = time.monotonic()
    _alias_map = alias
    logger.info(
        "용어집 스냅샷 로드: %d개 term, 카테고리 %s (생성 %s)",
        len(expand_m),
        {c: len(set(v.values())) for c, v in by_cat.items()} or "없음",
        (data.get("_meta") or {}).get("fetched_at", "?"),
    )
    return True


def _load() -> None:
    """용어집 API에서 전체 사전을 로드하고 내부 캐시를 갱신합니다."""
    global _alias_map, _expand_map, _category_of, _by_category, _loaded_at
    global _fail_count, _last_attempt

    # 성공·실패와 무관하게 시도 시각을 기록해 재시도 폭주를 막습니다.
    _last_attempt = time.monotonic()

    try:
        resp = httpx.get(GLOSSARY_URL, timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        alias: dict[str, str] = {}
        expand_m: dict[str, list[str]] = {}
        cat_of: dict[str, str] = {}
        by_cat: dict[str, dict[str, str]] = {}

        _index_terms(data.get("terms", []), alias, expand_m, cat_of, by_cat)

        # /all 응답에 category 가 없는 구버전 API 대응 —
        # 주체 분류에 필요한 카테고리만 개별 조회해 보완합니다.
        if not by_cat:
            for cat in _FALLBACK_CATEGORIES:
                try:
                    r = httpx.get(
                        GLOSSARY_CATEGORY_URL, params={"category": cat}, timeout=_HTTP_TIMEOUT
                    )
                    r.raise_for_status()
                    _index_terms(r.json().get("terms", []), alias, expand_m, cat_of, by_cat)
                except Exception as cat_exc:
                    logger.warning("용어집 카테고리 조회 실패 (%s): %s", cat, cat_exc)

        # _alias_map 을 **마지막에** 바꿉니다. _ensure() 가 "_alias_map 이
        # 차 있으면 로드됨"으로 판단하므로, 이걸 먼저 대입하면 다른 스레드가
        # 새 alias 와 아직 갱신되지 않은 category 인덱스를 함께 보게 되어
        # category_of()/resolve_in() 이 빈 값을 돌려줍니다.
        _expand_map = expand_m
        _category_of = cat_of
        _by_category = by_cat
        _loaded_at = time.monotonic()
        _fail_count = 0
        _alias_map = alias
        logger.info(
            "용어집 로드 완료: %d개 term, %d개 alias, 카테고리 %s (URL: %s)",
            len(expand_m),
            len(alias),
            {c: len(v) for c, v in by_cat.items()} or "없음",
            GLOSSARY_URL,
        )

    except Exception as exc:
        _fail_count += 1
        if not _alias_map:
            # 최초 로드 실패 — 오프라인 스냅샷으로 폴백 시도
            if _load_from_snapshot():
                logger.warning(
                    "용어집 API 실패(%s) — 스냅샷으로 대체: %s",
                    exc,
                    _SNAPSHOT_PATH.name,
                )
                return
            giving_up = " (이 프로세스에서 재시도 중단)" if _fail_count >= _MAX_FAILS else ""
            logger.warning(
                "용어집 최초 로드 실패 %d/%d — 동의어 해결 비활성화%s: %s",
                _fail_count,
                _MAX_FAILS,
                giving_up,
                exc,
            )
        else:
            # 갱신 실패 — 이전 캐시 유지
            logger.warning(
                "용어집 갱신 실패 — 이전 캐시(%d개 term) 유지: %s",
                len(_expand_map),
                exc,
            )


def _ensure() -> None:
    """필요 시 사전을 로드/갱신합니다.

    ※ 재시도 폭주 방지: resolve()/resolve_in() 은 인제스트 중 이벤트마다
      호출되므로, 캐시가 비어 있다고 매번 네트워크를 때리면 타임아웃마다
      파이프라인이 멈춥니다. 실패 시에는 _FAIL_RETRY 간격을 두고,
      연속 _MAX_FAILS 회 실패한 뒤에는 _GIVEUP_RETRY 로 간격을 크게 늘립니다.

    ※ 완전히 포기하지는 않습니다. MCP 서버는 Restart=always 로 며칠씩 떠
      있는데, 부팅 직후 몇 분간 용어집 서버가 닫혀 있었다는 이유로 프로세스
      수명 내내 동의어 해결이 꺼져 있으면 안 됩니다.

    ※ 여러 스레드가 동시에 들어와도 로드는 한 번만 수행합니다.
    """
    if _alias_map:
        if time.monotonic() - _loaded_at > _TTL:
            with _load_lock:
                if time.monotonic() - _loaded_at > _TTL:
                    _load()
        return

    # 아직 한 번도 로드하지 못한 상태
    wait = _GIVEUP_RETRY if _fail_count >= _MAX_FAILS else _FAIL_RETRY
    if time.monotonic() - _last_attempt <= wait:
        return
    with _load_lock:
        # 락을 기다리는 동안 다른 스레드가 이미 로드했을 수 있습니다.
        if _alias_map:
            return
        wait = _GIVEUP_RETRY if _fail_count >= _MAX_FAILS else _FAIL_RETRY
        if time.monotonic() - _last_attempt > wait:
            _load()


# ── 공개 API ──────────────────────────────────────────────────────────────────


def resolve(name: str) -> str:
    """
    alias 또는 canonical → canonical(term).

    용어집에 없으면 name 원본을 그대로 반환합니다.
    merge_node() 직전에 호출하여 FalkorDB 노드 이름을 정규화합니다.

    예:
        resolve("드래곤슈퍼")  → "DS"
        resolve("월간활성유저") → "MAU"
        resolve("미등록단어")   → "미등록단어"
    """
    _ensure()
    return _alias_map.get(name, name)


def expand(name: str) -> list[str]:
    """
    name(canonical 또는 alias) → [canonical, synonym1, synonym2, ...].

    용어집에 없으면 [name]을 반환합니다.
    graph_search / timeline_search 에서 쿼리를 동의어 전체로 확장할 때 사용합니다.

    예:
        expand("DS")         → ["DS", "드래곤슈퍼", "Dragon Super"]
        expand("드래곤슈퍼") → ["DS", "드래곤슈퍼", "Dragon Super"]
        expand("미등록")     → ["미등록"]
    """
    _ensure()
    canonical = _alias_map.get(name, name)
    return list(_expand_map.get(canonical, [name]))


def category_of(name: str) -> str:
    """
    용어의 카테고리를 반환합니다. 미등록이면 빈 문자열.

    카테고리 예: "game", "organization", "KPI", "marketing", "business"

    예:
        category_of("POTC")           → "game"
        category_of("캐리비안의 해적") → "game"   (동의어도 동일 판정)
        category_of("미등록단어")      → ""
    """
    _ensure()
    return _category_of.get(_alias_map.get(name, name), "")


def resolve_in(name: str, category: str) -> str:
    """
    지정한 카테고리 안에서만 canonical 을 찾습니다. 없으면 빈 문자열.

    "이 값이 게임인가?"를 판정하면서 동시에 정규화할 때 사용합니다.

    예:
        resolve_in("캐리비안의 해적", "game")  → "POTC"
        resolve_in("재무실", "game")           → ""      (게임이 아님)
        resolve_in("재무실", "organization")   → "재무실"
    """
    _ensure()
    return _by_category.get(category, {}).get(name, "")


def terms_in(category: str) -> list[str]:
    """지정한 카테고리의 canonical term 목록을 반환합니다.

    예: terms_in("game") → ["POTC", "RESU", ...]
    """
    _ensure()
    return sorted(set(_by_category.get(category, {}).values()))


def categories() -> dict[str, int]:
    """로드된 카테고리별 term 수 — 진단용. 예: {"game": 2, "organization": 3}"""
    _ensure()
    return {c: len(set(m.values())) for c, m in _by_category.items()}


def is_available() -> bool:
    """용어집이 로드되어 사용 가능한 상태인지 — 진단용.

    로드를 먼저 시도합니다. 그냥 캐시만 보면, 아직 아무도 조회하지 않은
    시점에 호출했을 때 (스냅샷으로 정상 동작할 상황에서도) False 가 나옵니다.
    """
    _ensure()
    return bool(_alias_map)


def preload() -> None:
    """
    인제스트 파이프라인·MCP 서버 시작 시 명시적으로 미리 로드합니다.
    TTL·실패 카운터와 무관하게 강제로 한 번 시도합니다.
    """
    global _fail_count
    _fail_count = 0  # 명시적 요청이므로 이전 실패 이력을 무시하고 재시도
    _load()
