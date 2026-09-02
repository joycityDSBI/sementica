"""
증분 동기화 스크립트 (Incremental Sync)
Notion에서 마지막 동기화 이후 수정된 페이지만 Qdrant + FalkorDB에 업데이트합니다.

동작 원리:
  1. data/{dept}/sync_state.json 에서 last_sync_time 읽기
  2. Notion API에서 last_edited_time 내림차순으로 페이지 조회
  3. last_sync_time 이후 수정된 페이지만 처리:
     a. Qdrant: source_url 필터로 기존 벡터 전체 삭제 후 재삽입
     b. FalkorDB: source_url 필터로 기존 엣지 전체 삭제 후 재삽입
  4. sync_state.json 갱신

사용법:
  python src/pipeline/sync.py --dept strategic          # 증분 동기화
  python src/pipeline/sync.py --dept strategic --full   # 강제 전체 재동기화
  python src/pipeline/sync.py --dept strategic --dry-run # 확인만

Cron 등록 (매일 새벽 2시):
  0 2 * * * cd ~/sementica && .venv/bin/python src/pipeline/sync.py --dept strategic >> data/logs/sync.log 2>&1
"""

import argparse
import contextlib
import json
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

# ─── .env 로드 ────────────────────────────────────────────────────────────────
_env_path = Path(__file__).parent.parent.parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(Path(__file__).parent))

try:
    import httpx
except ImportError:
    raise SystemExit("httpx가 필요합니다: pip install httpx") from None

from dept_config import load_dept  # noqa: E402
from notion_fetch import (  # noqa: E402
    RATE_LIMIT_DELAY,
    extract_db_properties,
    fetch_blocks_recursive,
    notion_headers,
    page_title,
)
from semantica_helper import (  # noqa: E402
    extract_with_fallback,
    is_decision_triplet,
    merge_node,
    record_decision_node,
    upsert_event_node,
)

# DB 로거 (POSTGRES_URL 없으면 no-op)
sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
try:
    from db_logger import log_sync_result
    from db_logger import upsert_notion_page as _upsert_notion_page
    from db_logger import get_pages_edit_times as _get_pages_edit_times
except Exception:
    def log_sync_result(*a, **kw): pass
    def _upsert_notion_page(*a, **kw): pass
    def _get_pages_edit_times(*a, **kw): return None  # import 실패 → PG 미연결로 간주

# ─── 설정 ─────────────────────────────────────────────────────────────────────
GCP_PROJECT      = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
LOCATION         = os.environ.get("VERTEX_AI_LOCATION", "us-east5")    # 임베딩 리전
ANTHROPIC_REGION = os.environ.get("ANTHROPIC_VERTEX_REGION", "global")  # Claude LLM 리전
EMBED_MODEL_NAME = "text-multilingual-embedding-002"
EMBED_BATCH_SIZE = 50     # Vertex AI 배치 크기
HAIKU_MODEL      = "claude-haiku-4-5@20251001"   # 트리플·이벤트 추출용 (Vertex AI 형식)
QDRANT_URL       = os.environ.get("QDRANT_URL", "http://localhost:6333")
FALKORDB_HOST    = os.environ.get("FALKORDB_HOST", "localhost")
FALKORDB_PORT    = int(os.environ.get("FALKORDB_PORT", "6379"))
CHUNK_SIZE       = 800
CHUNK_OVERLAP    = 200

# ─── 이벤트 추출 프롬프트 (ingest.py와 동일) ──────────────────────────────────
EVENT_EXTRACT_PROMPT = """\
다음 텍스트에서 게임/서비스의 이벤트·업데이트를 추출하세요.
날짜가 명시된 항목만 추출합니다. 날짜 형식: YYYY-MM-DD 또는 YY-MM-DD.

이벤트 유형 (event_type):
  client_update   — 클라이언트 패치·업데이트
  server_update   — 서버 점검·배포
  user_event      — 신규·복귀·기간한정 유저 이벤트
  season          — 시즌 개막·종료
  content_release — 신규 콘텐츠 오픈
  maintenance     — 정기 점검
  incident        — 장애 발생·복구
  kpi_milestone   — DAU·매출·ROAS 마일스톤 달성
  ua_budget       — UA 매체 예산 변경 (증액·감액·중단)
  ua_creative     — UA 소재(크리에이티브) 교체·추가·중단
  ua_channel      — UA 매체·채널 추가·제거·전략 변경
  ua_targeting    — UA 타겟·오디언스 세그먼트 변경
  ua_abtest       — UA A/B 테스트 시작·종료·결과 적용

텍스트:
{text}

이벤트가 있으면 JSON 배열, 없으면 [] 로만 응답하세요:
[
  {{
    "game":        "게임명",
    "event_type":  "client_update",
    "date":        "YYYY-MM-DD",
    "title":       "이벤트 제목",
    "description": "상세 설명 (없으면 빈 문자열)",
    "target":      "신규유저,복귀유저 (해당 없으면 빈 문자열)",
    "manager":     "담당자 또는 팀 이름 (모르면 빈 문자열)"
  }}
]"""

# ─── DB 속성 키 별칭 ─────────────────────────────────────────────────────────
_DATE_KEYS    = {"이벤트날짜", "날짜", "일자", "date", "event_date", "시작일", "시작날짜"}
_GAME_KEYS    = {"게임명", "게임", "game", "product", "서비스명", "서비스"}
_TYPE_KEYS    = {"이벤트유형", "유형", "event_type", "type", "종류"}
_MANAGER_KEYS = {"담당자", "담당팀", "manager", "owner", "담당"}


def _event_from_db_props(db_props: dict, source_url: str, title: str) -> dict | None:
    """
    Notion DB 속성 딕셔너리에서 Event dict를 직접 생성합니다.
    날짜와 게임명이 모두 있을 때만 반환, 없으면 None.
    """
    if not db_props:
        return None

    date_val = next((db_props[k] for k in _DATE_KEYS if k in db_props), None)
    game_val = next((db_props[k] for k in _GAME_KEYS if k in db_props), None)
    if not date_val or not game_val:
        return None

    type_val = next((db_props[k] for k in _TYPE_KEYS if k in db_props), "")
    mgr_raw = next((db_props[k] for k in _MANAGER_KEYS if k in db_props), "")
    mgr_val = ", ".join(mgr_raw) if isinstance(mgr_raw, list) else str(mgr_raw) if mgr_raw else ""

    return {
        "game":        str(game_val),
        "date":        str(date_val)[:10],
        "title":       title,
        "event_type":  str(type_val),
        "manager":     mgr_val,
        "source_url":  source_url,
        "description": "",
        "target":      "",
    }


# ─── 트리플 추출 프롬프트 ──────────────────────────────────────────────────────
EXTRACT_PROMPT = """\
다음 텍스트에서 엔티티-관계-엔티티 트리플을 추출하세요.

━━ 1. 엔티티 type (아래 8가지만 사용, 그 외 타입 금지) ━━━━━━━━━━━━━━
  Person   — 실명이 있는 사람. 예: 홍길동, 김철수 팀장
             ※ "담당자", "관리자" 처럼 역할어만 있으면 Role로 분류
  Team     — 팀·본부·실·센터·부문 등 조직 단위.
             예: 전략사업본부, DI팀, 마케팅실
             ※ "조이시티"처럼 회사 전체는 Team 아님 → 생략
  System   — IT 시스템, 플랫폼, DB, 툴, API.
             예: BigQuery, Slack, 인사시스템, MMP
  Process  — 반복 수행되는 업무 절차·프로세스.
             예: 정산 프로세스, 데이터 적재 파이프라인
  Policy   — 정책, 규정, 기준, 지침.
             예: 개인정보처리방침, 결재 기준
  Document — 보고서, 문서, 양식, 기획서.
             예: 주간보고서, UA 전략 문서
  Role     — 직책·역할어 (이름 없이 역할만).
             예: 팀장, 담당자, 승인권자, PO
  Decision — 명시적으로 결정·확정된 사항.
             예: 예산 승인, 런칭 결정

━━ 2. 엔티티 이름 표기 규칙 (중복 방지 핵심) ━━━━━━━━━━━━━━━━━━━━━
  ① 문서에 나온 표기를 그대로 사용 (번역·변형 금지)
     올바름: "빅쿼리" (문서에 이렇게 표기)  금지: "BigQuery"로 바꾸지 말 것
  ② 조직명은 약칭보다 공식 명칭 우선
     올바름: "전략사업본부"  금지: "전략본부", "전략사업부"로 변형
  ③ 사람 이름은 성+이름 전체 사용
     올바름: "홍길동"  금지: "홍씨", "길동"
  ④ 시스템명은 고유명사 그대로 (대소문자, 영문 유지)
     올바름: "BigQuery", "Slack"  금지: "빅쿼리"로 번역
  ⑤ 동일 개념이 여러 표기로 등장하면 → 첫 등장 표기 기준으로 통일

━━ 3. 관계명 (아래 목록에서만 선택) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  조직 관계: 소속, 관리, 보고, 협업
  업무 관계: 담당, 운영, 요청, 승인, 검토
  시스템 관계: 연동, 적재, 활용, 생성, 분석, 참조
  문서 관계: 작성, 포함, 정의
  ※ 목록에 없는 관계는 가장 가까운 것으로 대체. 임의 신조어 금지.

━━ 4. 추출하지 않는 것 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ✗ 날짜·시간 (2026-01-01, 오전 9시)
  ✗ 숫자·통계 (100만원, 30%, 3회)
  ✗ 컬럼명·필드명 (user_id, created_at)
  ✗ 일반 동사·형용사 (진행, 완료, 중요)
  ✗ 회사 전체 이름 ("조이시티" 단독 엔티티)
  ✗ 의미 없는 단어 (것, 내용, 사항, 경우)

━━ 5. 추출 예시 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  입력: "DI팀 유현상이 마케팅실 요청으로 BigQuery에 일별 유저 데이터를 적재한다."
  출력:
  [
    {{"subject": {{"name": "유현상", "type": "Person"}},
      "predicate": {{"name": "소속"}},
      "object":   {{"name": "DI팀", "type": "Team"}}}},
    {{"subject": {{"name": "마케팅실", "type": "Team"}},
      "predicate": {{"name": "요청"}},
      "object":   {{"name": "유현상", "type": "Person"}}}},
    {{"subject": {{"name": "유현상", "type": "Person"}},
      "predicate": {{"name": "적재"}},
      "object":   {{"name": "BigQuery", "type": "System"}}}}
  ]

━━ 6. 텍스트 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{text}

JSON 배열로만 응답 (설명·마크다운 없이):
[
  {{
    "subject":   {{"name": "이름", "type": "타입"}},
    "predicate": {{"name": "관계명"}},
    "object":    {{"name": "이름", "type": "타입"}}
  }}
]
트리플이 없으면 [] 반환."""


# ─── 청킹 ────────────────────────────────────────────────────────────────────
def _make_chunks(text: str) -> list[str]:
    chunks, start = [], 0
    while start < len(text):
        chunk = text[start:start + CHUNK_SIZE].strip()
        if chunk:
            chunks.append(chunk)
        if start + CHUNK_SIZE >= len(text):
            break
        start = start + CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


# ─── Notion 페이지 존재 여부 확인 ────────────────────────────────────────────
def check_page_exists(client, token: str, page_id_nodash: str) -> bool:
    """
    Notion 페이지가 여전히 접근 가능한지 확인합니다.
    - 404 → 삭제됨
    - archived=true → 휴지통으로 이동 (삭제로 간주)
    - 네트워크 오류 → True 반환 (안전 우선, 실수로 삭제 방지)
    """
    # 대시 없는 32자 page_id → Notion API 형식 UUID로 변환
    pid = page_id_nodash.replace("-", "")   # 혹시 대시 섞인 경우 정규화
    notion_id = f"{pid[:8]}-{pid[8:12]}-{pid[12:16]}-{pid[16:20]}-{pid[20:32]}"
    try:
        resp = client.get(
            f"https://api.notion.com/v1/pages/{notion_id}",
            headers=notion_headers(token),
        )
        time.sleep(RATE_LIMIT_DELAY)
        if resp.status_code == 404:
            return False
        if resp.status_code == 200:
            return not resp.json().get("archived", False)
        return True   # 기타 상태 (403 권한 없음 등)는 존재하는 것으로 간주
    except Exception:
        return True   # 네트워크 오류 시 삭제로 처리하지 않음


def _get_stored_pages(dept: str) -> list[dict]:
    """notion_pages 테이블에서 인제스천된 페이지 목록 반환."""
    pg_url = os.environ.get("POSTGRES_URL", "")
    if not pg_url:
        return []
    try:
        import psycopg2
        conn = psycopg2.connect(pg_url)
        with conn, conn.cursor() as cur:
            cur.execute(
                """SELECT page_id, notion_url, title
                       FROM notion_pages
                       WHERE dept = %s AND status IN ('ok', 'skipped')
                       ORDER BY last_ingested_at DESC""",
                (dept,),
            )
            return [{"page_id": r[0], "notion_url": r[1], "title": r[2] or r[0]}
                    for r in cur.fetchall()]
    except Exception as e:
        print(f"  ⚠️  notion_pages 조회 실패: {e}")
        return []
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def _mark_page_deleted(page_id: str, dept: str) -> None:
    """notion_pages에서 해당 페이지를 deleted 상태로 마킹."""
    pg_url = os.environ.get("POSTGRES_URL", "")
    if not pg_url:
        return
    try:
        import psycopg2
        conn = psycopg2.connect(pg_url)
        with conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE notion_pages SET status='deleted' WHERE page_id=%s AND dept=%s",
                (page_id, dept),
            )
    except Exception as e:
        print(f"  ⚠️  삭제 마킹 실패: {e}")
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def delete_page_events(graph, source_url: str) -> int:
    """:Event 노드 중 source_url 일치하는 것 삭제. 삭제된 수 반환."""
    try:
        res = graph.query(
            "MATCH (e:Event {source_url: $url}) DETACH DELETE e RETURN count(e) AS cnt",
            {"url": source_url},
        )
        return res.result_set[0][0] if res.result_set else 0
    except Exception as e:
        print(f"    ⚠️  Event 노드 삭제 실패: {e}")
        return 0


def reconcile_deleted_pages(
    notion_client, token: str, dept: str,
    qc, graph, collection_name: str,
    dry_run: bool = False,
) -> dict:
    """
    notion_pages 테이블 기준으로 Notion에서 삭제된 페이지를 감지하고
    Qdrant·FalkorDB·PostgreSQL에서 해당 데이터를 정리합니다.

    사용: sync.py --dept strategic --reconcile
    권장 주기: 주 1회 (cron 일요일 새벽 3시)
    """
    print("\n" + "=" * 60)
    print(f"🔍 [RECONCILE] Notion 삭제 페이지 감지 — {dept}")
    print("=" * 60)

    stored = _get_stored_pages(dept)
    if not stored:
        print("  [i] notion_pages 테이블에 데이터 없음.")
        print("      ingest.py 실행 후 재시도하세요.")
        return {"checked": 0, "deleted": 0}

    print(f"  📋 저장된 페이지 {len(stored)}개 확인 시작...")
    if dry_run:
        print("  [DRY-RUN] 실제 삭제는 수행하지 않습니다.")

    deleted_pages = []
    for i, page in enumerate(stored, 1):
        page_id    = page["page_id"]
        source_url = page["notion_url"]
        title      = page["title"]

        exists = check_page_exists(notion_client, token, page_id)

        if not exists:
            print(f"\n  🗑️  [{i:03d}/{len(stored):03d}] 삭제 감지: {title[:55]}")
            print(f"       URL: {source_url}")

            if not dry_run:
                v_del  = delete_page_vectors(qc, collection_name, source_url)
                e_del  = delete_page_edges(graph, source_url)
                ev_del = delete_page_events(graph, source_url)
                _mark_page_deleted(page_id, dept)
                print(f"       → 벡터 {v_del}개 / 엣지 {e_del}개 / 이벤트 {ev_del}개 삭제 완료")
            else:
                print("       [DRY-RUN] 삭제 예정")

            deleted_pages.append({"page_id": page_id, "title": title, "url": source_url})
        else:
            if i % 50 == 0:
                print(f"  ✅ [{i:03d}/{len(stored):03d}] 확인 중...")

    print(f"\n  결과: {len(stored)}개 확인 → {len(deleted_pages)}개 삭제됨")
    if deleted_pages:
        print("  삭제된 페이지:")
        for p in deleted_pages:
            print(f"    - {p['title']}")
    return {"checked": len(stored), "deleted": len(deleted_pages)}


# ─── Qdrant 벡터 삭제 (source_url 필터) ──────────────────────────────────────
def delete_page_vectors(qc, collection_name: str, source_url: str) -> int:
    """source_url이 일치하는 벡터 전체 삭제. 삭제된 수 반환."""
    from qdrant_client.models import FieldCondition, Filter, MatchValue
    try:
        result = qc.delete(
            collection_name=collection_name,
            points_selector=Filter(
                must=[FieldCondition(key="source_url", match=MatchValue(value=source_url))]
            ),
        )
        return getattr(result, 'deleted', 0) or 0
    except Exception as e:
        print(f"    ⚠️  벡터 삭제 실패: {e}")
        return 0


# ─── FalkorDB 엣지 삭제 (source_url 필터) ────────────────────────────────────
def delete_page_edges(graph, source_url: str) -> int:
    """
    source_url이 일치하는 자동 생성 엣지만 삭제. 수동 입력 엣지(is_manual=true)는 보존.
    삭제된 수 반환.
    """
    try:
        res = graph.query(
            "MATCH ()-[r:REL {source_url: $url}]->() "
            "WHERE r.is_manual IS NULL OR r.is_manual = false "
            "DELETE r RETURN count(r) AS cnt",
            {"url": source_url}
        )
        return res.result_set[0][0] if res.result_set else 0
    except Exception as e:
        print(f"    ⚠️  엣지 삭제 실패: {e}")
        return 0


# ─── 트리플 추출 ─────────────────────────────────────────────────────────────
def _norm_node(val) -> dict:
    if isinstance(val, dict):
        return {"name": str(val.get("name", "")), "type": str(val.get("type", "Unknown"))}
    return {"name": str(val), "type": "Unknown"}


def _norm_pred(val) -> dict:
    if isinstance(val, dict):
        pred = {"name": str(val.get("name", ""))}
        for k in ("condition", "duration"):
            if k in val:
                pred[k] = str(val[k])
        if "order" in val:
            with contextlib.suppress(ValueError, TypeError):
                pred["order"] = int(val["order"])
        return pred
    return {"name": str(val)}


def extract_events_from_text(llm_client, text: str) -> list[dict]:
    """Claude로 텍스트에서 날짜 기반 시계열 이벤트를 추출합니다."""
    try:
        resp = llm_client.messages.create(
            model=HAIKU_MODEL,   # Sonnet → Haiku (3~5배 빠름)
            max_tokens=1024,
            messages=[{"role": "user", "content": EVENT_EXTRACT_PROMPT.format(text=text[:3000])}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            raw = raw.removeprefix("json")
        raw = raw.strip()
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [
                e for e in parsed
                if isinstance(e, dict) and e.get("game") and e.get("date")
            ]
        return []
    except Exception:
        return []


def extract_triplets(llm_client, text: str) -> list:
    raw = ""
    try:
        resp = llm_client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=2048,
            messages=[{"role": "user", "content": EXTRACT_PROMPT.format(text=text[:3000])}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1][4:] if len(parts) > 1 else raw
        parsed = json.loads(raw.strip())
        result = [
            {
                "subject":   _norm_node(t.get("subject", "")),
                "predicate": _norm_pred(t.get("predicate", "")),
                "object":    _norm_node(t.get("object", "")),
            }
            for t in parsed if isinstance(t, dict)
        ]
        if not result:
            print(f"    [LLM] 트리플 없음 (LLM 응답: {raw[:120]!r})")
        return result
    except json.JSONDecodeError as e:
        print(f"    ⚠️  LLM 응답 JSON 파싱 실패: {e} | 응답: {raw[:200]!r}")
        return []
    except Exception as e:
        print(f"    ⚠️  LLM 트리플 추출 실패 (API 오류): {type(e).__name__}: {e}")
        return []


# ─── 페이지 동기화 (핵심 함수) ───────────────────────────────────────────────
def sync_page(
    notion_client, token: str, page_meta: dict,
    qc, embed_client, llm_client, graph,
    collection_name: str, dry_run: bool = False, dept: str = "",
) -> dict:
    """
    단일 페이지를 Notion에서 가져와 Qdrant + FalkorDB 업데이트
    """
    page_id    = page_meta["id"].replace("-", "")
    title      = page_title(page_meta)
    source_url = page_meta.get("url", "")

    result = {
        "page_id":    page_id,
        "title":      title,
        "source_url": source_url,
        "deleted_vectors": 0,
        "deleted_edges":   0,
        "new_chunks":      0,
        "new_triplets":    0,
        "new_events":      0,
    }

    print(f"  🔄 {title[:60]}")
    print(f"     {source_url}")

    if dry_run:
        print("     [DRY-RUN] 건너뜀")
        return result

    # 1. Notion에서 본문 가져오기
    try:
        body = fetch_blocks_recursive(notion_client, token, page_id)
    except Exception as e:
        print(f"     ❌ 본문 조회 실패: {e}")
        result["error"] = str(e)
        return result

    if len(body.split()) < 30:
        print(f"     ⚠️  텍스트 부족 ({len(body.split())} 단어) — 건너뜀")
        result["skipped"] = True
        _upsert_notion_page(
            page_id=page_id, dept=dept,
            notion_url=source_url, title=title,
            last_edited_time=page_meta.get("last_edited_time"),
            word_count=len(body.split()),
            is_db_item=bool(extract_db_properties(page_meta)),
            status="skipped",
        )
        return result

    # 2. 기존 벡터 삭제
    deleted_v = delete_page_vectors(qc, collection_name, source_url)
    result["deleted_vectors"] = deleted_v
    print(f"     벡터 삭제: {deleted_v}개")

    # 3. 기존 엣지 삭제
    deleted_e = delete_page_edges(graph, source_url)
    result["deleted_edges"] = deleted_e
    print(f"     엣지 삭제: {deleted_e}개")

    # 4. 청킹 + 배치 임베딩 + Qdrant 일괄 저장
    chunks = _make_chunks(body)
    new_chunks = 0
    if chunks:
        try:
            # 4-1. 배치 임베딩 (EMBED_BATCH_SIZE 단위, API 호출 최소화)
            all_vecs = []
            for bi in range(0, len(chunks), EMBED_BATCH_SIZE):
                batch = chunks[bi:bi + EMBED_BATCH_SIZE]
                res   = embed_client.models.embed_content(
                    model=EMBED_MODEL_NAME, contents=batch
                )
                all_vecs.extend([list(e.values) for e in res.embeddings])

            # 4-2. 전체 청크 한 번에 Qdrant upsert
            points = []
            for i, (chunk, vec) in enumerate(zip(chunks, all_vecs, strict=True)):
                points.append({
                    "id":     str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source_url}#chunk{i}")),
                    "vector": vec,
                    "payload": {
                        "title":       title,
                        "source_url":  source_url,
                        "page_id":     page_id,
                        "text":        chunk,
                        "chunk_index": i,
                        "chunk_total": len(chunks),
                    },
                })
            qc.upsert(collection_name=collection_name, points=points)
            new_chunks = len(chunks)
        except Exception as e:
            print(f"     ⚠️  배치 임베딩/저장 실패: {e}")

    result["new_chunks"] = new_chunks
    print(f"     벡터 저장: {new_chunks}개 청크")

    # 5+6. LLM 추출 — 트리플·이벤트 동시 실행 (순차 대비 ~40% 단축)
    # DB 속성 먼저 확인 (API 없음, 즉시) → LLM 이벤트 추출 필요 여부 결정
    db_props   = extract_db_properties(page_meta)
    ev_from_db = _event_from_db_props(db_props, source_url, title) if db_props else None

    with ThreadPoolExecutor(max_workers=2) as _pool:
        ft = _pool.submit(
            extract_with_fallback,
            lambda t: extract_triplets(llm_client, t),
            body,
        )
        # DB 속성에 이벤트가 없을 때만 LLM 이벤트 추출 병행
        fe = (
            _pool.submit(extract_events_from_text, llm_client, body)
            if ev_from_db is None
            else None
        )
        triplets, triplet_src = ft.result()
        llm_events = fe.result() if fe is not None else []

    # 5. 트리플 → FalkorDB 저장
    print(f"     트리플: {len(triplets)}개 추출 [{triplet_src}]")
    node_cache = {}

    def get_or_create_node(entity: dict) -> int:
        key = (entity["name"], entity["type"])
        if key in node_cache:
            return node_cache[key]
        # merge_node: 그래프 전체에서 MERGE → 크로스-문서 중복 제거
        nid = merge_node(graph, entity["name"], entity["type"], source_url)
        if nid >= 0:
            node_cache[key] = nid
        return nid

    edges_created = 0
    for t in triplets:
        sid = get_or_create_node(t["subject"])
        oid = get_or_create_node(t["object"])
        if sid < 0 or oid < 0:
            continue
        pred = t["predicate"]
        props = {"rel_name": pred["name"], "source_url": source_url}
        for k in ("condition", "order", "duration"):
            if k in pred:
                props[k] = pred[k]
        try:
            # create_relationship() SDK 메서드는 버전에 따라 동작이 다름 →
            # Cypher 직접 실행으로 대체 (node id 기반, 안정적)
            set_clauses = ", ".join(f"r.{k} = ${k}" for k in props)
            graph.query(
                f"MATCH (s), (o) WHERE id(s) = $sid AND id(o) = $oid "
                f"CREATE (s)-[r:REL]->(o) SET {set_clauses}",
                {"sid": sid, "oid": oid, **props},
            )
            edges_created += 1

            # 의사결정 트리플이면 :Decision 노드로도 기록
            if is_decision_triplet(t):
                record_decision_node(graph, t, source_url)
        except Exception as e:
            print(f"    ⚠️  엣지 생성 실패 ({t['subject']['name']} → {t['object']['name']}): {e}")

    result["new_triplets"] = edges_created
    print(f"     그래프: {len(node_cache)}개 노드 / {edges_created}개 엣지")

    # 6. 이벤트 → FalkorDB :Event 노드 저장
    ev_stored = 0
    if ev_from_db:
        # 6a. DB 속성에서 직접 생성 (LLM 없음, 100% 정확)
        nid = upsert_event_node(graph, ev_from_db)
        if nid >= 0:
            ev_stored += 1
        print(
            f"     이벤트(DB속성): {ev_from_db['game']} / {ev_from_db['date']}"
            f" / {ev_from_db['event_type'] or '유형미지정'}"
            f" → {'저장' if nid >= 0 else '실패'}"
        )
    elif llm_events:
        # 6b. LLM 추출 결과 저장 (트리플과 동시 실행 완료됨)
        for ev in llm_events:
            ev["source_url"] = source_url
            nid = upsert_event_node(graph, ev)
            if nid >= 0:
                ev_stored += 1
        print(f"     이벤트(LLM): {ev_stored}/{len(llm_events)}개 :Event 노드 저장")
    else:
        print("     이벤트: 없음")

    result["new_events"] = ev_stored

    # ── notion_pages 레지스트리 업서트 (PostgreSQL) ───────────────────────────
    _upsert_notion_page(
        page_id=page_id,
        dept=dept,
        notion_url=source_url,
        title=title,
        last_edited_time=page_meta.get("last_edited_time"),
        word_count=len(body.split()),
        chunk_count=result.get("new_chunks", 0),
        triplet_count=result.get("new_triplets", 0),
        event_count=ev_stored,
        is_db_item=bool(extract_db_properties(page_meta)),
        status="ok",
    )

    return result


# ─── 동기화 상태 관리 ─────────────────────────────────────────────────────────
def load_sync_state(state_path: Path) -> dict:
    if state_path.exists():
        text = state_path.read_text(encoding="utf-8").strip()
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                print("  ⚠️  sync_state.json 파싱 실패 — 초기 상태로 재설정")
    # 파일 없거나 비어있거나 손상된 경우: 1970-01-01 (전체 동기화)
    return {"last_sync_time": "1970-01-01T00:00:00.000Z", "total_synced": 0}


def save_sync_state(state_path: Path, state: dict):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ─── Notion 수정 페이지 조회 ──────────────────────────────────────────────────
def fetch_modified_pages(client, token: str, since_iso: str) -> list:
    """last_edited_time > since_iso 인 전체 페이지 반환 (최신순 정렬)"""
    modified = []
    cursor   = None
    batch    = 1
    while True:
        body = {
            "filter": {"value": "page", "property": "object"},
            "sort":   {"direction": "descending", "timestamp": "last_edited_time"},
            "page_size": 100,
        }
        if cursor:
            body["start_cursor"] = cursor
        resp = client.post(
            "https://api.notion.com/v1/search",
            headers=notion_headers(token),
            json=body,
        )
        resp.raise_for_status()
        time.sleep(RATE_LIMIT_DELAY)
        data    = resp.json()
        results = data.get("results", [])

        stop = False
        added_this_batch = 0
        for page in results:
            last_edited = page.get("last_edited_time", "")
            if last_edited <= since_iso:
                stop = True
                break
            modified.append(page)
            added_this_batch += 1

        print(f"    배치 {batch:02d}: {len(results)}개 조회 → {added_this_batch}개 수정됨 (누적 {len(modified)}개)"
              + (" ← 기준 시각 도달, 중단" if stop else ""))
        batch += 1

        if stop or not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    return modified


def fetch_modified_pages_by_keyword(client, token: str, since_iso: str, keyword: str) -> list:
    """Notion 검색 API로 keyword 포함 페이지만 조회 후 since_iso 이후 수정된 것만 반환"""
    modified = []
    cursor   = None
    batch    = 1
    while True:
        body = {
            "query":  keyword,
            "filter": {"value": "page", "property": "object"},
            "sort":   {"direction": "descending", "timestamp": "last_edited_time"},
            "page_size": 100,
        }
        if cursor:
            body["start_cursor"] = cursor
        resp = client.post(
            "https://api.notion.com/v1/search",
            headers=notion_headers(token),
            json=body,
        )
        resp.raise_for_status()
        time.sleep(RATE_LIMIT_DELAY)
        data    = resp.json()
        results = data.get("results", [])

        stop = False
        added_this_batch = 0
        for page in results:
            last_edited = page.get("last_edited_time", "")
            if last_edited <= since_iso:
                stop = True
                break
            modified.append(page)
            added_this_batch += 1

        print(f"    배치 {batch:02d}: {len(results)}개 조회 → {added_this_batch}개 수정됨 (누적 {len(modified)}개)"
              + (" ← 기준 시각 도달, 중단" if stop else ""))
        batch += 1

        if stop or not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    return modified


# ─── 메인 ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Semantica 증분 동기화")
    parser.add_argument("--dept",    required=True, help="본부 이름 (config/departments.yaml)")
    parser.add_argument("--search",  default="",   help="수집 대상 키워드 필터 (예: '프로세스'). 미지정 시 전체 페이지")
    parser.add_argument("--full",    action="store_true", help="전체 재동기화 (last_sync_time 무시)")
    parser.add_argument("--limit",   type=int, default=0,
                        help="처리할 최대 페이지 수 (기본: 0 = 무제한). 예: --limit 50")
    parser.add_argument("--dry-run",   action="store_true", help="변경 내용 확인만 (저장 안 함)")
    parser.add_argument("--reconcile", action="store_true",
                        help="Notion 삭제 페이지 감지 후 Qdrant·FalkorDB에서 데이터 정리 (주 1회 권장)")
    args = parser.parse_args()

    # ── 본부 설정 로드 ──────────────────────────────────────────────────────
    dept_cfg        = load_dept(args.dept)
    token           = dept_cfg["notion_token"]
    collection_name = dept_cfg["qdrant_collection"]
    graph_name      = dept_cfg["falkordb_graph"]
    data_dir        = dept_cfg["data_dir"]
    state_path      = data_dir / "sync_state.json"
    log_dir         = ROOT / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    start_time = time.time()
    now_iso    = datetime.now(UTC).isoformat()
    state      = load_sync_state(state_path)

    # ── since_iso 및 PostgreSQL 사전 확인 ──────────────────────────────────
    # stored_edit_times:
    #   None  → PostgreSQL 미연결 또는 오류 → sync_state.json 기준 유지
    #   {}    → 연결됨 + 신규 부서(데이터 없음) → 전체 조회, 모두 신규 처리
    #   {...} → 연결됨 + 기존 데이터 있음 → 전체 조회 후 per-page 비교
    stored_edit_times = None
    if args.full:
        since_iso = "1970-01-01T00:00:00.000Z"
    else:
        stored_edit_times = _get_pages_edit_times(args.dept)
        if stored_edit_times is not None:
            # PostgreSQL 연결됨 → 전체 페이지 조회, DB가 필터링 담당
            # (신규 연동된 오래된 페이지도 table에 없으므로 자동 수집)
            since_iso = "1970-01-01T00:00:00.000Z"
        else:
            # PostgreSQL 미연결 → sync_state.json 기준 (기존 동작)
            since_iso = state["last_sync_time"]

    print("=" * 60)
    print(f"🔄 증분 동기화 — {dept_cfg['name']} ({args.dept})")
    print(f"   컬렉션: {collection_name}  그래프: {graph_name}")
    if args.full:
        print(f"   기준:   전체 재동기화 (--full)")
    elif stored_edit_times is not None:
        print(f"   기준:   PostgreSQL per-page 비교 ({len(stored_edit_times)}개 기존 기록)")
    else:
        print(f"   기준:   {since_iso[:19]} 이후 수정된 페이지 (sync_state.json)")
    print(f"   시작:   {now_iso[:19]}")
    if args.dry_run:
        print("   [DRY-RUN 모드]")
    if args.limit:
        print(f"   [LIMIT {args.limit}페이지]")
    print("=" * 60)

    # ── 클라이언트 초기화 ──────────────────────────────────────────────────
    print("\n[1/4] 클라이언트 초기화")
    import falkordb as fdb
    from anthropic import AnthropicVertex
    from google import genai
    from qdrant_client import QdrantClient

    embed_client = genai.Client(project=GCP_PROJECT, location=LOCATION, vertexai=True)
    qc           = QdrantClient(url=QDRANT_URL)
    db           = fdb.FalkorDB(host=FALKORDB_HOST, port=FALKORDB_PORT)
    graph        = db.select_graph(graph_name)
    llm_client   = AnthropicVertex(project_id=GCP_PROJECT, region=ANTHROPIC_REGION)
    print("  ✅ 완료")

    # ── Reconcile 모드 (삭제 페이지 감지) ────────────────────────────────
    if args.reconcile:
        with httpx.Client(timeout=60) as notion_client:
            reconcile_deleted_pages(
                notion_client, token, args.dept,
                qc, graph, collection_name,
                dry_run=args.dry_run,
            )
        print("\n  ✅ Reconcile 완료")
        return

    # ── 수정 페이지 조회 ───────────────────────────────────────────────────
    keyword = args.search.strip()
    print("\n[2/4] Notion에서 수정 페이지 조회 중...")
    if keyword:
        print(f"   검색 키워드: '{keyword}' (Notion 검색 API 사용)")
    with httpx.Client(timeout=60) as notion_client:
        if keyword:
            # 키워드로 먼저 좁힌 뒤 시간 필터 적용 (전체 조회 불필요)
            modified_pages = fetch_modified_pages_by_keyword(notion_client, token, since_iso, keyword)
        else:
            modified_pages = fetch_modified_pages(notion_client, token, since_iso)
        print(f"  📋 Notion API 반환: {len(modified_pages)}개")

        # ── Per-page 필터링: PostgreSQL notion_pages.last_edited_time 기준 ──
        # stored_edit_times 는 Notion API 호출 전에 이미 조회됨 (since_iso 결정용).
        # None  → PostgreSQL 미연결 → Notion API 결과 전체 처리 (기존 동작 유지)
        # {}    → 연결됨 + 신규 부서 → 모두 신규 처리
        # {...} → 연결됨 → table에 없는 신규 페이지 + 수정된 페이지만 처리
        if not args.full and stored_edit_times is not None:
            before = len(modified_pages)
            truly_modified = []
            already_synced = 0
            new_pages      = 0
            for page in modified_pages:
                pid          = page.get("id", "").replace("-", "")
                notion_time  = page.get("last_edited_time", "")
                stored_time  = stored_edit_times.get(pid, "")
                if not stored_time:
                    # table에 없는 신규 페이지 → 무조건 처리
                    truly_modified.append(page)
                    new_pages += 1
                elif notion_time > stored_time:
                    # 수정된 페이지
                    truly_modified.append(page)
                else:
                    already_synced += 1
            modified_pages = truly_modified
            print(
                f"  🔍 Per-page 비교: {before}개 → "
                f"신규 {new_pages}개 + 수정 {len(modified_pages) - new_pages}개 처리, "
                f"{already_synced}개 스킵"
            )
        elif not args.full:
            print("  ℹ️  PostgreSQL 미연결 → Notion API 결과 전체 처리")

        # --limit 적용
        if args.limit and len(modified_pages) > args.limit:
            print(f"  ✂️  --limit {args.limit} 적용 → {args.limit}개만 처리 (나머지 {len(modified_pages) - args.limit}개 제외)")
            modified_pages = modified_pages[:args.limit]

        if not modified_pages:
            print("\n  ✅ 새로 수정된 페이지 없음 — 동기화 완료")
            state["last_sync_time"] = now_iso
            state["last_check"]     = now_iso
            if not args.dry_run:
                save_sync_state(state_path, state)
            return

        # ── 동기화 실행 ─────────────────────────────────────────────────
        print("\n[3/4] 페이지 동기화 시작")
        results = []
        for i, page in enumerate(modified_pages, 1):
            print(f"\n  [{i:03d}/{len(modified_pages):03d}]")
            r = sync_page(
                notion_client, token, page,
                qc, embed_client, llm_client, graph,
                collection_name, dry_run=args.dry_run, dept=args.dept,
            )
            results.append(r)
            time.sleep(0.5)

    # ── 결과 요약 ─────────────────────────────────────────────────────────
    success  = [r for r in results if not r.get("error") and not r.get("skipped")]
    errors   = [r for r in results if r.get("error")]
    skipped  = [r for r in results if r.get("skipped")]
    total_v  = sum(r.get("new_chunks",   0) for r in success)
    total_e  = sum(r.get("new_triplets", 0) for r in success)
    total_ev = sum(r.get("new_events",   0) for r in success)

    print("\n[4/4] 동기화 결과")
    print("=" * 60)
    print(f"  처리: {len(success)}개 / 건너뜀: {len(skipped)}개 / 오류: {len(errors)}개")
    print(f"  신규 벡터 청크: {total_v}개")
    print(f"  신규 트리플:    {total_e}개")
    print(f"  신규 이벤트:    {total_ev}개 :Event 노드")

    # ── 상태 저장 ─────────────────────────────────────────────────────────
    if not args.dry_run:
        state["last_sync_time"]  = now_iso
        state["last_sync_count"] = len(success)
        state["total_synced"]    = state.get("total_synced", 0) + len(success)
        save_sync_state(state_path, state)
        print(f"\n  💾 sync_state.json 갱신: {state_path}")

    # ── 로그 저장 ─────────────────────────────────────────────────────────
    log = {
        "dept": args.dept, "since": since_iso, "now": now_iso,
        "modified": len(modified_pages), "success": len(success),
        "skipped": len(skipped), "errors": len(errors),
        "new_chunks": total_v, "new_triplets": total_e,
        "results": results,
    }
    log_path = log_dir / f"sync_{args.dept}_{now_iso[:10].replace('-', '')}.json"
    log_path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  📄 로그: {log_path}")

    # ── DB 로그 저장 ──────────────────────────────────────────────────────
    duration_sec = int(time.time() - start_time)
    if errors and len(success) == 0:
        db_status = "failed"
    elif errors:
        db_status = "partial"
    elif args.dry_run:
        db_status = "dry_run"
    else:
        db_status = "success"

    error_sample = errors[0].get("error") if errors else None
    log_sync_result(
        dept=args.dept,
        search_keyword=keyword or None,
        since_time=since_iso,
        modified_found=len(modified_pages),
        processed=len(success),
        skipped=len(skipped),
        errors=len(errors),
        new_chunks=total_v,
        new_triplets=total_e,
        duration_sec=duration_sec,
        status=db_status,
        error_detail=error_sample,
    )

    print(f"\n  {'✅ 동기화 완료' if not errors else f'⚠️  {len(errors)}개 오류 발생'}")


if __name__ == "__main__":
    main()
