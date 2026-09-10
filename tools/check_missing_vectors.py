#!/usr/bin/env python3
"""
벡터가 실제로 유실된 페이지 찾기
─────────────────────────────────────────────────────────────────────────────
notion_pages 레지스트리에 "정상 인제스트됨(status='ok', route≠excluded)"으로
기록되어 있는데 Qdrant 에는 청크가 하나도 없는 페이지를 찾습니다.

왜 레지스트리의 chunk_count 만으로는 안 되는가:
  · 해시 일치로 건너뛴 페이지는 카운트를 넘기지 않아, 예전 코드에서는
    매 동기화마다 chunk_count 가 0 으로 덮어써졌습니다. 그래서 "chunk_count=0
    이고 status='ok'" 인 행 대부분은 정상 페이지입니다.
  · 반대로 sync 가 벡터를 지운 뒤 재생성에 실패하고 해시까지 기록한 페이지는
    실제로 비어 있는데도 chunk_count 에 옛 값이 남아 있을 수 있습니다.
Qdrant 를 직접 확인하는 것만이 정확합니다.

  --fix 를 주면 유실된 페이지의 content_hash 를 비웁니다. 그러면 다음
  sync 가 해시 불일치로 판단해 해당 페이지만 다시 처리합니다.

실행:
    python tools/check_missing_vectors.py --dept strategic
    python tools/check_missing_vectors.py --dept strategic --fix
"""

import argparse
import os
import sys
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

POSTGRES_URL = os.environ.get("POSTGRES_URL", "")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")


def _page_ids_in_qdrant(qc, collection: str) -> set:
    """컬렉션에 청크가 하나라도 있는 page_id 집합."""
    seen: set = set()
    offset = None
    while True:
        points, offset = qc.scroll(
            collection_name=collection,
            limit=1000,
            offset=offset,
            with_payload=["page_id"],
            with_vectors=False,
        )
        for p in points:
            pid = (p.payload or {}).get("page_id")
            if pid:
                seen.add(pid)
        if offset is None:
            break
    return seen


def main() -> int:
    ap = argparse.ArgumentParser(description="벡터 유실 페이지 점검")
    ap.add_argument("--dept", default="strategic")
    ap.add_argument("--fix", action="store_true", help="유실 페이지의 content_hash 를 비웁니다")
    args = ap.parse_args()

    if not POSTGRES_URL:
        print("❌ POSTGRES_URL 이 없습니다.")
        return 1

    import psycopg2
    from qdrant_client import QdrantClient

    from dept_config import load_dept

    collection = load_dept(args.dept)["qdrant_collection"]

    conn = psycopg2.connect(POSTGRES_URL)
    with conn, conn.cursor() as cur:
        cur.execute(
            """SELECT page_id, title, route, word_count, chunk_count, status, content_hash
               FROM notion_pages
               WHERE dept = %s AND status = 'ok' AND route <> 'excluded'
               ORDER BY title""",
            (args.dept,),
        )
        rows = cur.fetchall()

    print(f"  컬렉션: {collection} | 레지스트리 대상 {len(rows)}건")

    qc = QdrantClient(url=QDRANT_URL)
    have = _page_ids_in_qdrant(qc, collection)
    print(f"  Qdrant 에 청크가 있는 페이지: {len(have)}건\n")

    missing = [r for r in rows if r[0] not in have]
    stale_count = [r for r in rows if r[0] in have and (r[4] or 0) == 0]

    if stale_count:
        print(f"  ℹ️  chunk_count 가 0 이지만 실제로는 벡터가 있는 페이지: {len(stale_count)}건")
        print("     (해시 스킵이 카운트를 0 으로 덮어쓰던 예전 버그의 흔적 — 무해합니다)\n")

    if not missing:
        print("  ✅ 벡터가 유실된 페이지 없음")
        conn.close()
        return 0

    print(f"  ❌ 벡터 유실 {len(missing)}건")
    for page_id, title, route, wc, cc, _st, chash in missing:
        print(
            f"    {page_id}  [{route}] {(title or '')[:44]:<44} 단어 {wc:>5} 기록청크 {cc or 0:>3}"
        )
        if not chash:
            print("        └ content_hash 없음 — 다음 sync 가 이미 재처리합니다")

    if not args.fix:
        print("\n  --fix 를 주면 content_hash 를 비워 다음 sync 에서 재처리합니다.")
        conn.close()
        return 1

    ids = [r[0] for r in missing]
    with conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE notion_pages SET content_hash = '' WHERE dept = %s AND page_id = ANY(%s)",
            (args.dept, ids),
        )
        n = cur.rowcount
    conn.close()
    print(f"\n  ✅ {n}건의 content_hash 를 비웠습니다. 이제 동기화를 실행하세요:")
    print(f"     python src/pipeline/sync.py --dept {args.dept}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
