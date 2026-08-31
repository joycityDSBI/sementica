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
from datetime import datetime, timezone
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
    error: str = None,
) -> None:
    """MCP 도구 호출 1건을 mcp_request_log에 기록."""
    conn = _get_conn()
    if conn is None:
        return
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO mcp_request_log
                        (dept, tool, query, result_count, duration_ms, error)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (dept, tool, query[:2000], result_count, duration_ms, error),
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
    since_time,          # datetime or ISO string
    modified_found: int,
    processed: int,
    skipped: int,
    errors: int,
    new_chunks: int,
    new_triplets: int,
    duration_sec: int,
    status: str,         # "success" | "partial" | "failed" | "dry_run"
    error_detail: str = None,
) -> None:
    """동기화 작업 1회 결과를 sync_log에 기록."""
    conn = _get_conn()
    if conn is None:
        return
    # since_time 정규화
    if isinstance(since_time, str):
        try:
            since_time = datetime.fromisoformat(since_time.replace("Z", "+00:00"))
        except Exception:
            since_time = None
    try:
        with conn:
            with conn.cursor() as cur:
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
