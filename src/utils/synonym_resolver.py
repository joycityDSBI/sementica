"""
JoyCity 비즈니스 용어집 기반 동의어 해결기
─────────────────────────────────────────────────────────────────────────────
https://catalog.joycityplay.com/api/glossary/all 에서 전체 용어집을 로드하여
엔티티 이름을 canonical form(term)으로 정규화하거나 동의어 전체로 확장합니다.

  norm_key(name)           → 표기(공백·대소문자·부호)를 지운 비교용 키.
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
import re
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
# {정규화 키 → canonical_term}  정확 일치가 실패했을 때의 폴백 (_norm_key 참고)
_alias_norm: dict[str, str] = {}
# {canonical_term → [canonical_term, synonym1, ...]}
_expand_map: dict[str, list[str]] = {}
# {canonical_term → category}  예: {"POTC": "game", "데이터사이언스실": "organization"}
_category_of: dict[str, str] = {}
# {category → {alias/term → canonical_term}}  카테고리 한정 조회용
_by_category: dict[str, dict[str, str]] = {}
# {category → {정규화 키 → canonical_term}}
_by_category_norm: dict[str, dict[str, str]] = {}
# [(정규화 키, [canonical, ...]), ...]  표기를 지우면 모호해지는 항목 — 진단용
_conflicts: list = []
_loaded_at: float = 0.0


def norm_key(s: str) -> str:
    """표기 차이를 지운 조회용 키 — 공백·문장부호 제거 + 소문자.

    용어집 조회가 정확 일치만 하면, 등록돼 있는데도 못 찾는 표현이 대부분입니다.
    실측(스냅샷 109개 용어 / 350개 표현): 350개 중 241개가 공백·대소문자·부호를
    포함해 정확 일치가 깨질 수 있는 형태였고, 실제로

        resolve("평균 동접") → "ACU"      (등록된 표기)
        resolve("평균동접")  → "평균동접"   ← 띄어쓰기 하나에 실패
        resolve("android")   → "android"  ← "Android"/"ANDROID" 는 등록돼 있음

    문서 본문의 표기를 저자가 용어집과 똑같이 쓸 이유는 없으므로, 정확 일치가
    실패했을 때의 폴백으로 이 키를 씁니다.
    """
    return re.sub(r"[\s\W_]+", "", (s or "").lower())


def _build_indexes(entries: list) -> tuple[dict, dict, dict, dict, dict, dict, list]:
    """용어 목록 → 조회 인덱스 일체.

    두 가지 모호함을 **추측하지 않고** 처리합니다.

    ① 어떤 용어가 다른 용어의 동의어로도 등록된 경우.
       실측: "가입 경과일" 은 그 자체로 용어인데 "코호트" 의 동의어이기도 해서,
       사전을 만드는 순서에 따라 resolve("가입 경과일") 이 "코호트" 가 됐습니다.
       → **용어 자신이 항상 이깁니다.** 순서와 무관하게 결과가 같습니다.

    ② 표기를 지우면 서로 다른 용어를 가리키게 되는 경우.
       실측: "신규가입자"→RU, "신규 가입자"→DRU. 띄어쓰기 하나로 다른 엔티티가
       됩니다. 둘 중 하나를 고르면 절반은 틀립니다.
       → **정규화 폴백에서 제외**하고 conflicts 로 보고합니다. 정확 일치는
         그대로 두므로 기존 동작이 나빠지지는 않습니다. 이건 용어집 데이터의
         모순이라 코드가 아니라 용어집에서 풀어야 합니다.

    Returns:
        (alias, alias_norm, expand, cat_of, by_cat, by_cat_norm, conflicts)
    """
    alias: dict[str, str] = {}
    expand_m: dict[str, list[str]] = {}
    cat_of: dict[str, str] = {}
    by_cat: dict[str, dict[str, str]] = {}

    valid = [e for e in entries if isinstance(e, dict) and e.get("term")]
    term_names = {e["term"] for e in valid}

    # 정규화 키마다 어떤 canonical 들이 걸리는지 — 2개 이상이면 모호합니다.
    norm_claims: dict[str, set] = {}
    norm_cat_claims: dict[tuple, set] = {}

    for entry in valid:
        canonical: str = entry["term"]
        synonyms: list[str] = entry.get("synonyms") or []
        all_forms: list[str] = [canonical, *synonyms]
        category = str(entry.get("category") or "").strip()

        for form in all_forms:
            # ① 용어 자신을 남의 동의어로 덮어쓰지 않습니다.
            if form != canonical and form in term_names:
                continue
            alias[form] = canonical
            norm_claims.setdefault(norm_key(form), set()).add(canonical)
            if category:
                by_cat.setdefault(category, {})[form] = canonical
                norm_cat_claims.setdefault((category, norm_key(form)), set()).add(canonical)

        expand_m[canonical] = all_forms
        if category:
            cat_of[canonical] = category

    # ② 한 정규화 키를 여러 용어가 주장하면 폴백에서 뺍니다.
    alias_norm = {k: next(iter(v)) for k, v in norm_claims.items() if len(v) == 1 and k}
    by_cat_norm: dict[str, dict[str, str]] = {}
    for (cat, k), v in norm_cat_claims.items():
        if len(v) == 1 and k:
            by_cat_norm.setdefault(cat, {})[k] = next(iter(v))

    conflicts = sorted((k, sorted(v)) for k, v in norm_claims.items() if len(v) > 1 and k)
    return alias, alias_norm, expand_m, cat_of, by_cat, by_cat_norm, conflicts


def _load_from_snapshot() -> bool:
    """오프라인 스냅샷 파일에서 사전을 로드합니다. 성공하면 True.

    용어집 API에 접근할 수 없는 환경에서도 동의어 정규화와 게임/조직 판정이
    동작하도록 하는 폴백입니다. 파일이 없거나 비어 있으면 False 를 반환하고
    호출부가 기존 실패 처리를 이어갑니다.
    """
    global _alias_map, _alias_norm, _expand_map, _category_of
    global _by_category, _by_category_norm, _conflicts, _loaded_at

    try:
        if not _SNAPSHOT_PATH.exists():
            return False
        data = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("용어집 스냅샷 읽기 실패 (%s): %s", _SNAPSHOT_PATH, exc)
        return False

    alias, alias_n, expand_m, cat_of, by_cat, by_cat_n, conf = _build_indexes(data.get("terms", []))
    if not alias:
        return False

    # _alias_map 은 "로드됨" 판정 기준이므로 맨 마지막에 대입합니다 (_load 와 동일).
    _alias_norm = alias_n
    _expand_map = expand_m
    _category_of = cat_of
    _by_category = by_cat
    _by_category_norm = by_cat_n
    _conflicts = conf
    # TTL 을 적용해 이후 API 재시도 기회를 남깁니다.
    _loaded_at = time.monotonic()
    _alias_map = alias
    logger.info(
        "용어집 스냅샷 로드: %d개 term, 카테고리 %s (생성 %s)",
        len(expand_m),
        {c: len(set(v.values())) for c, v in by_cat.items()} or "없음",
        (data.get("_meta") or {}).get("fetched_at", "?"),
    )
    _warn_conflicts()
    return True


def _load() -> None:
    """용어집 API에서 전체 사전을 로드하고 내부 캐시를 갱신합니다."""
    global _alias_map, _alias_norm, _expand_map, _category_of
    global _by_category, _by_category_norm, _conflicts, _loaded_at
    global _fail_count, _last_attempt

    # 성공·실패와 무관하게 시도 시각을 기록해 재시도 폭주를 막습니다.
    _last_attempt = time.monotonic()

    try:
        resp = httpx.get(GLOSSARY_URL, timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        entries: list = list(data.get("terms", []))

        # /all 응답에 category 가 없는 구버전 API 대응 —
        # 주체 분류에 필요한 카테고리만 개별 조회해 보완합니다.
        if not any(str((e or {}).get("category") or "").strip() for e in entries):
            for cat in _FALLBACK_CATEGORIES:
                try:
                    r = httpx.get(
                        GLOSSARY_CATEGORY_URL, params={"category": cat}, timeout=_HTTP_TIMEOUT
                    )
                    r.raise_for_status()
                    entries.extend(r.json().get("terms", []))
                except Exception as cat_exc:
                    logger.warning("용어집 카테고리 조회 실패 (%s): %s", cat, cat_exc)

        # 인덱스는 **모든 응답을 모은 뒤 한 번에** 만듭니다. 예전에는 응답마다
        # 같은 사전에 덧칠했는데, 그러면 나중 응답의 동의어가 앞 응답의 용어를
        # 덮어써 조회 순서에 따라 결과가 달라집니다 (_build_indexes ① 참고).
        alias, alias_n, expand_m, cat_of, by_cat, by_cat_n, conf = _build_indexes(entries)

        # _alias_map 을 **마지막에** 바꿉니다. _ensure() 가 "_alias_map 이
        # 차 있으면 로드됨"으로 판단하므로, 이걸 먼저 대입하면 다른 스레드가
        # 새 alias 와 아직 갱신되지 않은 category 인덱스를 함께 보게 되어
        # category_of()/resolve_in() 이 빈 값을 돌려줍니다.
        _alias_norm = alias_n
        _expand_map = expand_m
        _category_of = cat_of
        _by_category = by_cat
        _by_category_norm = by_cat_n
        _conflicts = conf
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
        _warn_conflicts()

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


def _warn_conflicts() -> None:
    """표기를 지우면 모호해지는 항목을 한 번 경고합니다.

    이건 코드가 고칠 수 없는 **용어집 데이터의 모순**입니다. 예를 들어
    "신규가입자"→RU, "신규 가입자"→DRU 처럼 띄어쓰기 하나로 다른 엔티티가
    되는 경우, 어느 쪽으로 정규화해도 절반은 틀립니다. 그래서 폴백에서
    제외하고, 용어집 담당자가 볼 수 있게 남깁니다.
    """
    if not _conflicts:
        return
    logger.warning(
        "용어집에 표기 충돌 %d건 — 정규화 폴백에서 제외합니다 (용어집에서 정리 필요): %s",
        len(_conflicts),
        "; ".join(f"{k} → {'/'.join(v)}" for k, v in _conflicts[:5]),
    )


# ── 공개 API ──────────────────────────────────────────────────────────────────


def resolve(name: str) -> str:
    """
    alias 또는 canonical → canonical(term).

    정확 일치를 먼저 보고, 실패하면 표기(공백·대소문자·부호)를 지운 키로
    한 번 더 찾습니다. 문서 저자가 용어집과 똑같이 띄어 쓸 이유는 없는데,
    정확 일치만 하면 등록된 용어조차 놓칩니다 — 실측으로 등록 표현 350개 중
    241개가 이 문제에 노출돼 있었습니다 (_norm_key 참고).

    용어집에 없으면 name 원본을 그대로 반환합니다.
    merge_node() 직전에 호출하여 FalkorDB 노드 이름을 정규화합니다.

    예:
        resolve("드래곤슈퍼")  → "DS"
        resolve("월간활성유저") → "MAU"
        resolve("평균 동접")   → "ACU"
        resolve("평균동접")    → "ACU"     ← 띄어쓰기가 달라도 동일
        resolve("미등록단어")   → "미등록단어"
    """
    _ensure()
    hit = _alias_map.get(name)
    if hit is not None:
        return hit
    return _alias_norm.get(norm_key(name), name)


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
    return list(_expand_map.get(resolve(name), [name]))


def category_of(name: str) -> str:
    """
    용어의 카테고리를 반환합니다. 미등록이면 빈 문자열.

    카테고리 예: "game", "organization", "KPI", "marketing", "business"

    예:
        category_of("POTC")           → "game"
        category_of("캐리비안의 해적") → "game"   (동의어도 동일 판정)
        category_of("미등록단어")      → ""
    """
    return _category_of.get(resolve(name), "")


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
    hit = _by_category.get(category, {}).get(name)
    if hit is not None:
        return hit
    # resolve() 와 같은 이유로 표기를 지운 키도 봅니다.
    return _by_category_norm.get(category, {}).get(norm_key(name), "")


def terms_in(category: str) -> list[str]:
    """지정한 카테고리의 canonical term 목록을 반환합니다.

    예: terms_in("game") → ["POTC", "RESU", ...]
    """
    _ensure()
    return sorted(set(_by_category.get(category, {}).values()))


def conflicts() -> list:
    """표기를 지우면 모호해지는 항목 — [(정규화 키, [canonical, ...]), ...].

    용어집 데이터의 모순이므로 코드가 아니라 용어집에서 고쳐야 합니다.
    예: ("신규가입자", ["DRU", "RU"]) — "신규가입자"는 RU, "신규 가입자"는 DRU.
    """
    _ensure()
    return list(_conflicts)


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
