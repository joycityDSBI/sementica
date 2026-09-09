"""
JoyCity 비즈니스 용어집 기반 동의어 해결기
─────────────────────────────────────────────────────────────────────────────
https://catalog.joycityplay.com/api/glossary/all 에서 전체 용어집을 로드하여
엔티티 이름을 canonical form(term)으로 정규화하거나 동의어 전체로 확장합니다.

  resolve(name)  → canonical term. 미등록이면 원본 반환.
  expand(name)   → [canonical, synonym1, ...]. 미등록이면 [name].
  preload()      → 인제스트·서버 시작 시 명시적 사전 로드.

API 구조 (catalog.joycityplay.com):
  GET /api/glossary/all
  {
    "count": 42,
    "terms": [
      {
        "id": 1,
        "term": "MAU",                              ← canonical
        "synonyms": ["월간활성유저", "월별활성사용자"],  ← aliases
        ...
      },
      ...
    ]
  }

인증 불필요. term 필드가 canonical, synonyms 가 대안 표현.
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
_TTL: float = 3600.0  # 1시간 캐시

# {alias/term → canonical_term}
_alias_map: dict[str, str] = {}
# {canonical_term → [canonical_term, synonym1, ...]}
_expand_map: dict[str, list[str]] = {}
_loaded_at: float = 0.0


def _load() -> None:
    """용어집 API에서 전체 사전을 로드하고 내부 캐시를 갱신합니다."""
    global _alias_map, _expand_map, _loaded_at

    try:
        resp = httpx.get(GLOSSARY_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        alias: dict[str, str] = {}
        expand_m: dict[str, list[str]] = {}

        for entry in data.get("terms", []):
            canonical: str = entry["term"]
            synonyms: list[str] = entry.get("synonyms") or []
            all_forms: list[str] = [canonical, *synonyms]

            # 모든 표현 → canonical
            for form in all_forms:
                alias[form] = canonical

            # canonical → 모든 표현 (검색 확장용)
            expand_m[canonical] = all_forms

        _alias_map = alias
        _expand_map = expand_m
        _loaded_at = time.monotonic()
        logger.info(
            "용어집 로드 완료: %d개 term, %d개 alias (URL: %s)",
            len(expand_m),
            len(alias),
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


def preload() -> None:
    """
    인제스트 파이프라인·MCP 서버 시작 시 명시적으로 미리 로드합니다.
    TTL과 무관하게 강제 갱신합니다.
    """
    _load()
