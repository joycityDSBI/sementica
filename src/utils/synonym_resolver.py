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
"""

import logging
import os
import time

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

_TTL: float = 3600.0  # 1시간 캐시

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


def _load() -> None:
    """용어집 API에서 전체 사전을 로드하고 내부 캐시를 갱신합니다."""
    global _alias_map, _expand_map, _category_of, _by_category, _loaded_at

    try:
        resp = httpx.get(GLOSSARY_URL, timeout=10)
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
                    r = httpx.get(GLOSSARY_CATEGORY_URL, params={"category": cat}, timeout=10)
                    r.raise_for_status()
                    _index_terms(r.json().get("terms", []), alias, expand_m, cat_of, by_cat)
                except Exception as cat_exc:
                    logger.warning("용어집 카테고리 조회 실패 (%s): %s", cat, cat_exc)

        _alias_map = alias
        _expand_map = expand_m
        _category_of = cat_of
        _by_category = by_cat
        _loaded_at = time.monotonic()
        logger.info(
            "용어집 로드 완료: %d개 term, %d개 alias, 카테고리 %s (URL: %s)",
            len(expand_m),
            len(alias),
            {c: len(v) for c, v in by_cat.items()} or "없음",
            GLOSSARY_URL,
        )

    except Exception as exc:
        if not _alias_map:
            # 최초 로드 실패 — 동의어 해결 없이 계속 동작
            logger.warning("용어집 최초 로드 실패 — 동의어 해결 비활성화: %s", exc)
        else:
            # 갱신 실패 — 이전 캐시 유지
            logger.warning(
                "용어집 갱신 실패 — 이전 캐시(%d개 term) 유지: %s",
                len(_expand_map),
                exc,
            )


def _ensure() -> None:
    """TTL 초과 시 사전을 갱신합니다."""
    if time.monotonic() - _loaded_at > _TTL:
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


def preload() -> None:
    """
    인제스트 파이프라인·MCP 서버 시작 시 명시적으로 미리 로드합니다.
    TTL과 무관하게 강제 갱신합니다.
    """
    _load()
