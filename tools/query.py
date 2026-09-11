#!/usr/bin/env python3
"""
FalkorDB Cypher 실행기 — redis-cli 없이 그래프를 조회합니다.
─────────────────────────────────────────────────────────────────────────────
    python tools/query.py --dept strategic "MATCH (e:Event) RETURN count(e)"
    python tools/query.py --dept strategic --rel 퍼포먼스팀      # 엔티티 관계 요약
    python tools/query.py --dept strategic --rel GBTW --to       # 들어오는 관계만

--rel 은 자주 쓰는 "이 엔티티에 무슨 트리플이 붙어 있나" 조회의 단축형입니다.
이름은 부분 일치(CONTAINS)로 찾습니다.
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

FALKORDB_HOST = os.environ.get("FALKORDB_HOST", "localhost")
FALKORDB_PORT = int(os.environ.get("FALKORDB_PORT", "6379"))

_REL_OUT = (
    "MATCH (a)-[r:REL]->(b) WHERE a.name CONTAINS $n "
    "RETURN a.name, r.rel_name, b.name, r.source_url LIMIT $lim"
)
_REL_IN = (
    "MATCH (a)-[r:REL]->(b) WHERE b.name CONTAINS $n "
    "RETURN a.name, r.rel_name, b.name, r.source_url LIMIT $lim"
)


def main() -> int:
    ap = argparse.ArgumentParser(description="FalkorDB Cypher 실행")
    ap.add_argument("cypher", nargs="?", default="", help="실행할 Cypher")
    ap.add_argument("--dept", default="strategic")
    ap.add_argument("--rel", default="", help="이 이름이 들어간 엔티티의 관계를 조회")
    ap.add_argument("--to", action="store_true", help="--rel 을 들어오는 방향으로")
    ap.add_argument("--limit", type=int, default=50)
    args = ap.parse_args()

    if not args.cypher and not args.rel:
        ap.error("cypher 또는 --rel 중 하나가 필요합니다")

    import falkordb
    from dept_config import load_dept

    graph_name = load_dept(args.dept)["falkordb_graph"]
    g = falkordb.FalkorDB(host=FALKORDB_HOST, port=FALKORDB_PORT).select_graph(graph_name)

    if args.rel:
        cypher = _REL_IN if args.to else _REL_OUT
        params = {"n": args.rel, "lim": args.limit}
        direction = "들어오는" if args.to else "나가는"
        print(f"[{graph_name}] '{args.rel}' {direction} 관계")
    else:
        cypher, params = args.cypher, {}
        print(f"[{graph_name}] {cypher}")

    try:
        r = g.query(cypher, params) if params else g.query(cypher)
    except Exception as e:
        print(f"❌ {type(e).__name__}: {e}")
        return 1

    rows = r.result_set or []
    if not rows:
        print("  (결과 없음)")
        return 0

    header = getattr(r, "header", None)
    if header:
        # header 는 [(type, name), ...] 형태
        names = [h[1].decode() if isinstance(h[1], bytes) else str(h[1]) for h in header]
        print("  " + " | ".join(names))
        print("  " + "-" * 60)
    for row in rows:
        print("  " + " | ".join("" if v is None else str(v) for v in row))
    print(f"\n  {len(rows)}행")
    return 0


if __name__ == "__main__":
    sys.exit(main())
