"""
운영 로그 — PostgreSQL 기록 모듈

POSTGRES_URL 환경변수가 설정되지 않았거나 psycopg2가 없으면
모든 함수가 조용히 no-op으로 동작합니다 (서버/동기화 중단 없음).

환경변수:
  POSTGRES_URL=postgresql://user:pass@host:5432/dbname

테이블 초기화:
  psql $POSTGRES_URL -f schema/ops_log.sql
"""

import os
import time
from datetime import datetime
from functools import wraps
from pathlib import Path

# .env 로드 (직접 실행 시)
_env_path = Path(__file__).parent.parent.parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

POSTGRES_URL = os.environ.get("POSTGRES_URL", "")

try:
    import psycopg2

    _HAS_PG = True
except ImportError:
    _HAS_PG = False


def _get_conn():
    """PostgreSQL 연결 반환. 설정 없거나 실패 시 None."""
    if not _HAS_PG or not POSTGRES_URL:
        return None
    try:
        return psycopg2.connect(POSTGRES_URL)
    except Exception as e:
        print(f"  [DB] 연결 실패 (로그 건너뜀): {e}")
        return None


# ─── MCP 요청 로그 ────────────────────────────────────────────────────────────


def log_mcp_request(
    dept: str,
    tool: str,
    query: str,
    result_count: int = 0,
    duration_ms: int = 0,
    error: str | None = None,
    vector_count: int | None = None,
    graph_count: int | None = None,
    timeline_count: int | None = None,
    sub_queries: int | None = None,
    truncated: bool = False,
) -> None:
    """MCP 도구 호출 1건을 mcp_request_log에 기록.

    경로별 건수를 함께 남깁니다. result_count 만으로는 벡터가 답했는지
    그래프가 답했는지 타임라인이 답했는지 알 수 없는데, 실측으로 같은 종류의
    질문이 타임라인이 붙으면 1.0 안 붙으면 0.0 인 사례가 있었습니다.
    """
    conn = _get_conn()
    if conn is None:
        return
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                    INSERT INTO mcp_request_log
                        (dept, tool, query, result_count, duration_ms, error,
                         vector_count, graph_count, timeline_count, sub_queries, truncated)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                (
                    dept,
                    tool,
                    query[:2000],
                    result_count,
                    duration_ms,
                    error,
                    vector_count,
                    graph_count,
                    timeline_count,
                    sub_queries,
                    truncated,
                ),
            )
    except Exception as e:
        print(f"  [DB] mcp_request_log 기록 실패: {e}")
    finally:
        conn.close()


def mcp_tool_logged(dept_getter, tool_name: str):
    """
    MCP 도구 함수에 붙이는 데코레이터.
    실행 시간 측정 + 성공/실패 자동 기록.

    사용 예:
        @mcp_tool_logged(lambda: DEPT_NAME, "semantic_search")
        def semantic_search(query: str, limit: int = 5):
            ...
    """

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            start = time.time()
            error = None
            result = None
            try:
                result = fn(*args, **kwargs)
                return result
            except Exception as e:
                error = str(e)
                raise
            finally:
                duration_ms = int((time.time() - start) * 1000)
                # 쿼리 추출 (첫 번째 인수 또는 'query' 키워드)
                query = ""
                if args:
                    query = str(args[0])
                elif "query" in kwargs:
                    query = str(kwargs["query"])
                elif "entity" in kwargs:
                    query = str(kwargs["entity"])
                # 결과 수 추출
                count = 0
                if isinstance(result, list):
                    count = len(result)
                elif isinstance(result, dict):
                    count = len(result.get("semantic_results", []) or result.get("outgoing", []))
                dept = dept_getter() if callable(dept_getter) else dept_getter
                log_mcp_request(
                    dept=dept,
                    tool=tool_name,
                    query=query,
                    result_count=count,
                    duration_ms=duration_ms,
                    error=error,
                )

        return wrapper

    return decorator


# ─── 동기화 작업 로그 ─────────────────────────────────────────────────────────


def log_sync_result(
    dept: str,
    search_keyword: str,
    since_time,  # datetime or ISO string
    modified_found: int,
    processed: int,
    skipped: int,
    errors: int,
    new_chunks: int,
    new_triplets: int,
    duration_sec: int,
    status: str,  # "success" | "partial" | "failed" | "dry_run"
    error_detail: str | None = None,
) -> None:
    """동기화 작업 1회 결과를 sync_log에 기록."""
    conn = _get_conn()
    if conn is None:
        return
    # since_time 정규화
    if isinstance(since_time, str):
        try:
            since_time = datetime.fromisoformat(since_time.removesuffix("Z") + "+00:00")
        except Exception:
            since_time = None
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                    INSERT INTO sync_log
                        (dept, search_keyword, since_time, modified_found,
                         processed, skipped, errors, new_chunks, new_triplets,
                         duration_sec, status, error_detail)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                (
                    dept,
                    search_keyword or None,
                    since_time,
                    modified_found,
                    processed,
                    skipped,
                    errors,
                    new_chunks,
                    new_triplets,
                    duration_sec,
                    status,
                    error_detail,
                ),
            )
        print(f"  [DB] sync_log 기록 완료 (status={status})")
    except Exception as e:
        print(f"  [DB] sync_log 기록 실패: {e}")
    finally:
        conn.close()


# ─── 인제스트 실행 로그 ───────────────────────────────────────────────────────


def log_ingest_result(
    dept: str,
    mode: str,
    files_found: int = 0,
    pages_stored: int = 0,
    pages_skipped: int = 0,
    pages_error: int = 0,
    chunks: int = 0,
    triplets: int = 0,
    nodes: int = 0,
    edges: int = 0,
    events: int = 0,
    workers: int = 0,
    duration_sec: int = 0,
    llm_temperature: float | None = None,
    llm_temp_channel: str | None = None,
    status: str = "success",
    error_detail: str | None = None,
) -> None:
    """인제스트 1회 결과를 ingest_log 에 기록합니다.

    files_found 와 pages_stored 를 함께 남기는 것이 핵심입니다 — 중복 파일이
    쌓여 있으면 둘이 4배까지 벌어지는데, 기록이 없어 그 사실을 발견하는 데
    하루가 걸렸습니다.
    """
    conn = _get_conn()
    if conn is None:
        return
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                    INSERT INTO ingest_log
                        (dept, mode, files_found, pages_stored, pages_skipped, pages_error,
                         chunks, triplets, nodes, edges, events, workers, duration_sec,
                         llm_temperature, llm_temp_channel, status, error_detail)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                (
                    dept,
                    (mode or "")[:20],
                    files_found,
                    pages_stored,
                    pages_skipped,
                    pages_error,
                    chunks,
                    triplets,
                    nodes,
                    edges,
                    events,
                    workers,
                    duration_sec,
                    llm_temperature,
                    (llm_temp_channel or "")[:16] or None,
                    (status or "success")[:20],
                    error_detail,
                ),
            )
        print(f"  [DB] ingest_log 기록 완료 (status={status})")
    except Exception as e:
        print(f"  [DB] ingest_log 기록 실패: {e}")
    finally:
        conn.close()


# ─── 저장소 실측 스냅샷 ───────────────────────────────────────────────────────

_SNAPSHOT_COLS = (
    "qdrant_chunks",
    "qdrant_pages",
    "qdrant_no_page_id",
    "graph_nodes",
    "graph_edges",
    "graph_events",
    "events_no_manager",
    "events_no_scope",
    "followed_by",
    "followed_by_skips",
    "registry_rows",
    "registry_error",
    "note",
)


def log_store_snapshot(dept: str, **counts) -> None:
    """저장소 실측치를 store_snapshot 에 기록합니다 (tools/stats.py --save).

    실행 로그가 "무엇을 하려 했는가"라면 이것은 "지금 무엇이 들어 있는가"입니다.
    둘이 어긋나는 것을 사람이 눈으로 발견하는 데 하루가 걸렸습니다.
    """
    conn = _get_conn()
    if conn is None:
        return
    try:
        placeholders = ", ".join(["%s"] * len(_SNAPSHOT_COLS))
        with conn, conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO store_snapshot (dept, {', '.join(_SNAPSHOT_COLS)}) "
                f"VALUES (%s, {placeholders})",
                (dept, *[counts.get(c) for c in _SNAPSHOT_COLS]),
            )
        print("  [DB] store_snapshot 기록 완료")
    except Exception as e:
        print(f"  [DB] store_snapshot 기록 실패: {e}")
    finally:
        conn.close()


# ─── 골든셋 평가 실행 이력 ────────────────────────────────────────────────────


def log_eval_run(
    dept: str,
    golden_set: str,
    collection: str = "",
    total: int = 0,
    scored: int = 0,
    harness_failed: int = 0,
    passed: int = 0,
    avg_score: float | None = None,
    category_scores: dict | None = None,
    difficulty_scores: dict | None = None,
    detail: list | None = None,
) -> None:
    """evaluate.py 결과 1회를 eval_run_log 에 기록합니다.

    golden_set 을 함께 남기는 것이 중요합니다 — 골든셋이 바뀌면 회차 간 총점
    비교가 무의미해지는데, 파일에만 있으면 나중에 무엇과 무엇을 비교하는지
    알 수 없습니다.
    """
    import json as _json

    conn = _get_conn()
    if conn is None:
        return
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                    INSERT INTO eval_run_log
                        (dept, golden_set, collection, total, scored, harness_failed,
                         passed, avg_score, category_scores, difficulty_scores, detail)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                (
                    dept,
                    golden_set,
                    (collection or "")[:100],
                    total,
                    scored,
                    harness_failed,
                    passed,
                    avg_score,
                    _json.dumps(category_scores or {}, ensure_ascii=False),
                    _json.dumps(difficulty_scores or {}, ensure_ascii=False),
                    _json.dumps(detail or [], ensure_ascii=False),
                ),
            )
        print("  [DB] eval_run_log 기록 완료")
    except Exception as e:
        print(f"  [DB] eval_run_log 기록 실패: {e}")
    finally:
        conn.close()


# ─── Notion 페이지 레지스트리 ─────────────────────────────────────────────────


def get_pages_edit_times(dept: str) -> "dict | None":
    """
    notion_pages 테이블에서 부서별 페이지 수정 시각을 반환합니다.
    증분 동기화 시 Notion API 결과와 per-page 비교에 사용합니다.

    Returns:
        {page_id: "2026-09-01T03:16:00+00:00", ...}
          - 테이블이 비어있으면 {} (연결됨 + 데이터 없음)
        None
          - PostgreSQL 미연결 또는 쿼리 실패 → 호출자가 fallback 처리
    """
    conn = _get_conn()
    if conn is None:
        return None  # 미연결: 호출자가 sync_state.json 등 fallback 사용
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT page_id, last_edited_time FROM notion_pages WHERE dept = %s",
                (dept,),
            )
            result = {}
            for page_id, last_edited_time in cur.fetchall():
                if last_edited_time is not None:
                    result[page_id] = last_edited_time.isoformat()
                else:
                    result[page_id] = ""
            return result  # {} 이면 "연결됨 + 신규 부서" 의미
    except Exception as e:
        print(f"  [DB] get_pages_edit_times 실패: {e}")
        return None
    finally:
        conn.close()


def upsert_notion_page(
    page_id: str,
    dept: str,
    notion_url: str = "",
    title: str = "",
    last_edited_time=None,  # datetime | ISO 문자열 | None
    word_count: int = 0,
    # 카운트 3종은 기본값이 **None(=기존 값 유지)** 입니다. 0 이 기본이던 시절에는
    # 해시 일치로 건너뛴 페이지가 카운트를 넘기지 않아 매 동기화마다 0 으로
    # 덮어써졌고, 그 결과 "chunk_count=0 인데 status='ok'" 인 행이 잔뜩 생겨
    # 벡터가 실제로 유실된 페이지와 구분할 수 없었습니다.
    chunk_count: int | None = None,
    triplet_count: int | None = None,
    event_count: int | None = None,
    is_db_item: bool = False,
    has_html_attachment: bool = False,  # HTML 첨부 파일 포함 여부  (v3)
    status: str = "ok",  # "ok" | "skipped" | "error"
    error_msg: str | None = None,
    route: str = "core",  # "core" | "defer" | "excluded"  (v2)
    content_hash: str | None = None,  # SHA-256 앞 16자  (v2)
) -> None:
    """
    Notion 페이지 1건을 notion_pages 테이블에 UPSERT합니다.
    (page_id, dept) 조합이 이미 있으면 갱신, 없으면 삽입.
    PostgreSQL 연결 없으면 no-op.

    v2 추가 필드:
      route        — 페이지 분류 경로 (core / defer / excluded)
      content_hash — 본문 SHA-256 앞 16자 (중복 처리 건너뜀 판단)
    """
    conn = _get_conn()
    if conn is None:
        return
    # last_edited_time 정규화 — 빈 문자열 포함 모든 비정상값은 None으로
    if isinstance(last_edited_time, str):
        if last_edited_time.strip():
            try:
                last_edited_time = datetime.fromisoformat(
                    last_edited_time.removesuffix("Z") + "+00:00"
                )
            except Exception:
                last_edited_time = None
        else:
            last_edited_time = None  # 빈 문자열 → NULL
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                    INSERT INTO notion_pages
                        (page_id, dept, notion_url, title, last_edited_time,
                         last_ingested_at, word_count, chunk_count, triplet_count,
                         event_count, is_db_item, has_html_attachment, status, error_msg,
                         route, content_hash)
                    VALUES (%s, %s, %s, %s, %s, NOW(), %s,
                            COALESCE(%s, 0), COALESCE(%s, 0), COALESCE(%s, 0),
                            %s, %s, %s, %s, %s, %s)
                    ON CONFLICT ON CONSTRAINT uq_notion_pages_page_dept
                    DO UPDATE SET
                        notion_url           = EXCLUDED.notion_url,
                        title                = EXCLUDED.title,
                        last_edited_time     = EXCLUDED.last_edited_time,
                        last_ingested_at     = NOW(),
                        word_count           = EXCLUDED.word_count,
                        -- 인수가 NULL 이면 기존 값을 유지합니다. EXCLUDED 를 쓰면
                        -- 위 INSERT 의 COALESCE(...,0) 때문에 0 으로 보여
                        -- "안 넘긴 것"과 "0 건"을 구분할 수 없으므로 따로 바인딩합니다.
                        chunk_count          = COALESCE(%s, notion_pages.chunk_count),
                        triplet_count        = COALESCE(%s, notion_pages.triplet_count),
                        event_count          = COALESCE(%s, notion_pages.event_count),
                        is_db_item           = EXCLUDED.is_db_item,
                        has_html_attachment  = EXCLUDED.has_html_attachment,
                        status               = EXCLUDED.status,
                        error_msg            = EXCLUDED.error_msg,
                        route                = EXCLUDED.route,
                        content_hash         = COALESCE(EXCLUDED.content_hash, notion_pages.content_hash)
                    """,
                (
                    page_id[:32] if page_id else "",
                    dept[:50] if dept else "",
                    notion_url or "",
                    (title or "")[:500],
                    last_edited_time,
                    word_count,
                    chunk_count,
                    triplet_count,
                    event_count,
                    is_db_item,
                    has_html_attachment,
                    (status or "ok")[:20],
                    error_msg,
                    (route or "core")[:20],
                    content_hash[:16] if content_hash else None,
                    # DO UPDATE 의 카운트 3종 (위 주석 참고)
                    chunk_count,
                    triplet_count,
                    event_count,
                ),
            )
    except Exception as e:
        print(f"  [DB] notion_pages upsert 실패: {e}")
    finally:
        conn.close()


def get_page_hashes(dept: str) -> "dict | None":
    """
    notion_pages 테이블에서 부서별 content_hash를 반환합니다.
    sync.py가 내용 무변경 페이지의 LLM 재처리를 건너뛸 때 사용합니다.

    Returns:
        {page_id: content_hash, ...}
          - content_hash가 NULL인 행은 포함하지 않음
          - 테이블이 비어있으면 {} (연결됨, 데이터 없음)
        None
          - PostgreSQL 미연결 또는 쿼리 실패 → 호출자가 skip 없이 전체 처리
    """
    conn = _get_conn()
    if conn is None:
        return None
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT page_id, content_hash FROM notion_pages "
                "WHERE dept = %s AND content_hash IS NOT NULL",
                (dept,),
            )
            return {row[0]: row[1] for row in cur.fetchall()}
    except Exception as e:
        print(f"  [DB] get_page_hashes 실패: {e}")
        return None
    finally:
        conn.close()
