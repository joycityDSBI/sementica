#!/usr/bin/env python3
"""
저장소 실측 통계 — Qdrant · FalkorDB · notion_pages 를 한 번에 확인합니다.
─────────────────────────────────────────────────────────────────────────────
인제스트 로그의 "N개 저장 완료" 는 **시도한 수**입니다. 실제로 검색 가능한
상태인지는 저장소를 직접 세어봐야 알 수 있습니다. 특히:

  · page_id 가 없는 청크는 vector_search_pages 가 통째로 건너뜁니다
    (페이지 단위로 묶는 키가 page_id 이고, 빈 값은 제외됩니다).
    따라서 "청크는 있는데 검색은 안 되는" 문서가 생길 수 있습니다.
  · source_url 이 없으면 sync 가 그 페이지의 벡터·엣지를 찾지 못해
    갱신도 삭제도 할 수 없습니다.
  · manager/scope 가 비면 timeline_search 의 키워드·주체 경로가 조용히
    빈 결과를 냅니다.

실행:
    python tools/stats.py --dept strategic
"""

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "pipeline"))

_env = ROOT / ".env"
if _env.exists():
    for raw in _env.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
FALKORDB_HOST = os.environ.get("FALKORDB_HOST", "localhost")
FALKORDB_PORT = int(os.environ.get("FALKORDB_PORT", "6379"))
POSTGRES_URL = os.environ.get("POSTGRES_URL", "")


def _pct(n: int, total: int) -> str:
    return f"{n / total * 100:.1f}%" if total else "—"


def qdrant_stats(collection: str) -> dict:
    from qdrant_client import QdrantClient

    qc = QdrantClient(url=QDRANT_URL)
    chunks = 0
    page_ids: set = set()
    urls: set = set()
    no_pid = 0
    no_url = 0
    offset = None
    while True:
        points, offset = qc.scroll(
            collection_name=collection,
            limit=1000,
            offset=offset,
            with_payload=["page_id", "source_url"],
            with_vectors=False,
        )
        for p in points:
            pl = p.payload or {}
            chunks += 1
            pid = pl.get("page_id") or ""
            url = pl.get("source_url") or ""
            if pid:
                page_ids.add(pid)
            else:
                no_pid += 1
            if url:
                urls.add(url)
            else:
                no_url += 1
        if offset is None:
            break

    print(f"\n■ Qdrant ({collection})")
    print(f"  청크 총계             {chunks}")
    print(f"  page_id 있는 페이지   {len(page_ids)}")
    print(f"  source_url 있는 문서  {len(urls)}")
    if no_pid:
        print(
            f"  ⚠️  page_id 없는 청크  {no_pid} ({_pct(no_pid, chunks)})"
            "  ← 이 청크들은 검색 결과에 절대 나오지 않습니다"
        )
    if no_url:
        print(f"  ⚠️  source_url 없는 청크 {no_url} ({_pct(no_url, chunks)})  ← sync 가 추적 불가")
    if not no_pid and not no_url:
        print("  ✅ 모든 청크가 page_id·source_url 을 가지고 있습니다")
    return {
        "qdrant_chunks": chunks,
        "qdrant_pages": len(page_ids),
        "qdrant_no_page_id": no_pid,
    }


def graph_stats(graph_name: str) -> dict:
    import falkordb

    g = falkordb.FalkorDB(host=FALKORDB_HOST, port=FALKORDB_PORT).select_graph(graph_name)

    def one(q: str) -> int:
        try:
            r = g.query(q)
            return r.result_set[0][0] if r.result_set else 0
        except Exception as e:
            print(f"    (쿼리 실패: {e})")
            return -1

    total_ev = one("MATCH (e:Event) RETURN count(e)")
    print(f"\n■ FalkorDB ({graph_name})")

    out = {
        "graph_nodes": one("MATCH (n) RETURN count(n)"),
        "graph_edges": one("MATCH ()-[r:REL]->() RETURN count(r)"),
        "graph_events": total_ev,
    }
    print(f"  노드                  {out['graph_nodes']}")
    print(f"  REL 엣지              {out['graph_edges']}")
    print(f"  :Event                {total_ev}")
    for label, key, q in [
        (
            "manager 없음",
            "events_no_manager",
            "MATCH (e:Event) WHERE e.manager IS NULL OR e.manager = '' RETURN count(e)",
        ),
        (
            "scope 없음",
            "events_no_scope",
            "MATCH (e:Event) WHERE e.scope IS NULL OR e.scope = '' RETURN count(e)",
        ),
        (
            "scope_type unknown",
            None,
            "MATCH (e:Event) WHERE e.scope_type = 'unknown' RETURN count(e)",
        ),
        (
            "date_ts 없음/0",
            None,
            "MATCH (e:Event) WHERE e.date_ts IS NULL OR e.date_ts = 0 RETURN count(e)",
        ),
    ]:
        n = one(q)
        if key:
            out[key] = n
        mark = "  ⚠️" if n > 0 and total_ev and n / total_ev > 0.2 else ""
        print(f"    {label:<20} {n:>5} ({_pct(n, total_ev)}){mark}")

    out["followed_by"] = one("MATCH ()-[r:FOLLOWED_BY]->() RETURN count(r)")
    print(f"  FOLLOWED_BY           {out['followed_by']}")
    skip = one(
        "MATCH (a:Event)-[:FOLLOWED_BY]->(c:Event) "
        "MATCH (b:Event) WHERE b.scope = a.scope "
        "AND b.date_ts > a.date_ts AND b.date_ts < c.date_ts "
        "RETURN count(DISTINCT a)"
    )
    if skip > 0:
        print(f"    ⚠️  건너뛰기 엣지     {skip}  ← 사이에 다른 이벤트가 있는 연결")
    elif skip == 0:
        print("    ✅ 건너뛰기 엣지 없음")
    out["followed_by_skips"] = skip
    return out


def registry_stats(dept: str) -> dict:
    if not POSTGRES_URL:
        print("\n■ notion_pages — POSTGRES_URL 없음, 건너뜀")
        return {}
    import psycopg2

    conn = psycopg2.connect(POSTGRES_URL)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT status, route, count(*) FROM notion_pages "
                "WHERE dept = %s GROUP BY status, route ORDER BY count(*) DESC",
                (dept,),
            )
            rows = cur.fetchall()
            cur.execute(
                "SELECT count(*) FROM notion_pages WHERE dept = %s AND "
                "(content_hash IS NULL OR content_hash = '')",
                (dept,),
            )
            no_hash = cur.fetchone()[0]
    finally:
        conn.close()

    total = sum(r[2] for r in rows)
    print(f"\n■ notion_pages ({dept})  총 {total}건")
    by_status: Counter = Counter()
    for status, route, n in rows:
        print(f"    {status or '(없음)':<10} / {route or '(없음)':<10} {n:>5}")
        by_status[status] += n
    if no_hash:
        print(f"    해시 없음(다음 sync 에서 재처리) {no_hash}")
    if by_status.get("error"):
        print(f"    ⚠️  error 상태 {by_status['error']}건 — 다음 sync 가 재시도합니다")
    return {"registry_rows": total, "registry_error": by_status.get("error", 0)}


def main() -> int:
    ap = argparse.ArgumentParser(description="저장소 실측 통계")
    ap.add_argument("--dept", default="strategic")
    ap.add_argument(
        "--save",
        action="store_true",
        help="store_snapshot 테이블에 기록 (cron 에 걸면 추세가 남습니다)",
    )
    ap.add_argument("--note", default="", help="--save 시 함께 남길 메모")
    args = ap.parse_args()

    from dept_config import load_dept

    cfg = load_dept(args.dept)
    print(f"본부: {cfg['name']} ({args.dept})")

    counts: dict = {}
    for fn, arg in (
        (qdrant_stats, cfg["qdrant_collection"]),
        (graph_stats, cfg["falkordb_graph"]),
        (registry_stats, args.dept),
    ):
        try:
            counts.update(fn(arg) or {})
        except Exception as e:
            print(f"\n  ❌ {fn.__name__} 실패: {type(e).__name__}: {e}")

    if args.save:
        # 실행 로그가 "무엇을 하려 했는가"라면 이 표는 "지금 무엇이 들어 있는가"
        # 입니다. 둘이 4배 어긋난 것을 사람이 눈으로 발견하는 데 하루가 걸렸습니다.
        sys.path.insert(0, str(ROOT / "src" / "ops"))
        try:
            from db_logger import log_store_snapshot

            log_store_snapshot(args.dept, note=args.note or None, **counts)
        except Exception as e:
            print(f"  ⚠️  스냅샷 기록 실패: {type(e).__name__}: {e}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
