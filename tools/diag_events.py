#!/usr/bin/env python3
"""
이벤트 노드 실태 점검 — "이벤트가 사라졌다" 를 단계별로 가릅니다
─────────────────────────────────────────────────────────────────────────────
"이벤트가 없다" 는 여러 가지를 뜻할 수 있고, 각각 원인과 대처가 다릅니다:

  ① :Event 노드 자체가 없음        → 인제스트에서 이벤트를 못 만든 것
  ② 노드는 있는데 주체 연결이 없음  → classify_scope 판정 문제 (HAD_EVENT 누락)
  ③ 다른 주체에 붙어 있음          → 주체가 game 에서 org 로 바뀌었거나 그 반대
  ④ scope 는 맞는데 조회가 못 찾음  → 조회 경로 문제

이 넷을 구분하지 않으면 엉뚱한 곳을 고치게 됩니다. 아무것도 바꾸지 않습니다.

실행:
    python tools/diag_events.py --dept strategic
    python tools/diag_events.py --dept strategic --scope RESU
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
POSTGRES_URL = os.environ.get("POSTGRES_URL", "")


def check_ledger(q, dept: str, n_ev: int) -> None:
    """장부(PostgreSQL)와 그래프를 대조합니다.

    그래프만 보면 "원래 이만큼이었다" 와 "만들어졌다가 잃었다" 를 구분할 수
    없습니다. notion_pages 는 인제스트가 **만들었다고 보고한** 수치라, 그래프와
    어긋나면 저장 단계에서 잃은 것입니다.
    """
    print("\n■ ⑥ 장부(PostgreSQL) 대조")
    if not POSTGRES_URL:
        print("    (POSTGRES_URL 없음)")
        return
    try:
        import psycopg2

        conn = psycopg2.connect(POSTGRES_URL)
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(event_count),0), COUNT(*) FILTER (WHERE event_count > 0) "
                "FROM notion_pages WHERE dept = %s",
                (dept,),
            )
            total_ev, pages = cur.fetchone()
            print(f"    notion_pages 기준: 이벤트 {total_ev}개 / 이벤트를 만든 페이지 {pages}개")
            if total_ev and not n_ev:
                print("    ❌ 장부에는 있는데 그래프에 없습니다 — 인제스트 후 그래프가 지워졌거나,")
                print("       다른 그래프에 기록됐을 수 있습니다 (--dept / falkordb_graph 확인)")
            elif n_ev and total_ev and n_ev < total_ev:
                print(f"    ⚠️  그래프 {n_ev} vs 장부 {total_ev} — {total_ev - n_ev}개가 없습니다")
                if n_ev == pages:
                    print("       그래프 이벤트 수가 **페이지 수와 정확히 같습니다**.")
                    print("       event_id 가 source_url 로만 정해져서 한 페이지의 이벤트가")
                    print("       모두 같은 ID 로 MERGE 되어 서로를 덮어쓴 결과입니다.")

            # 한 페이지에서 이벤트가 여러 개 나왔는데 그래프에는 몇 개 남았는지
            cur.execute(
                "SELECT notion_url, event_count FROM notion_pages "
                "WHERE dept = %s AND event_count > 1 ORDER BY event_count DESC LIMIT 5",
                (dept,),
            )
            multi = cur.fetchall()
            if multi:
                print()
                print(f"    이벤트를 2개 이상 만든 페이지 (상위 {len(multi)}개):")
                for url, cnt in multi:
                    got = q(
                        "MATCH (e:Event) WHERE e.source_url = $u RETURN count(e)",
                        {"u": url or ""},
                    )
                    n = got[0][0] if got else 0
                    print(
                        f"      {'  ' if n >= cnt else '❌'} 장부 {cnt}개 → 그래프 {n}개  "
                        f"{(url or '')[-44:]}"
                    )

            cur.execute(
                "SELECT to_char(ts,'MM-DD HH24:MI'), mode, pages_stored, events, "
                "triplets, status FROM ingest_log WHERE dept = %s "
                "ORDER BY ts DESC LIMIT 5",
                (dept,),
            )
            rows = cur.fetchall()
            if rows:
                print("\n    최근 인제스트:")
                for r in rows:
                    print(
                        f"      {r[0]} {r[1]!s:8} 페이지 {r[2]} / 이벤트 {r[3]} / "
                        f"트리플 {r[4]} / {r[5]}"
                    )
        conn.close()
    except Exception as exc:
        print(f"    ⚠️  조회 실패: {type(exc).__name__}: {exc}")


def main() -> int:
    ap = argparse.ArgumentParser(description="이벤트 노드 실태 점검")
    ap.add_argument("--dept", default="strategic")
    ap.add_argument("--scope", default="", help="특정 주체를 자세히 (예: RESU)")
    ap.add_argument("--show", type=int, default=12)
    args = ap.parse_args()

    import falkordb
    from dept_config import load_dept

    g = falkordb.FalkorDB(host=FALKORDB_HOST, port=FALKORDB_PORT).select_graph(
        load_dept(args.dept)["falkordb_graph"]
    )

    def q(cypher: str, params: dict | None = None) -> list:
        try:
            return g.query(cypher, params or {}).result_set or []
        except Exception as exc:
            print(f"    ⚠️  쿼리 실패: {type(exc).__name__}: {exc}")
            return []

    # ── ① 이벤트 노드가 있는가 ──────────────────────────────────────────
    total = q("MATCH (e:Event) RETURN count(e)")
    n_ev = total[0][0] if total else 0
    print(f"■ ① :Event 노드 {n_ev}개")
    if not n_ev:
        print("  ❌ 이벤트가 하나도 없습니다 — 인제스트에서 만들어지지 않았습니다.")
        print("     ingest_log / notion_pages.event_count 를 확인하세요 (아래 ⑤).")

    # ── ② 주체 종류 분포 ────────────────────────────────────────────────
    print("\n■ ② 주체 판정(scope_type) 분포")
    for row in q(
        "MATCH (e:Event) RETURN coalesce(e.scope_type, '(없음)'), count(e) ORDER BY count(e) DESC"
    ):
        print(f"    {row[0]:10} {row[1]}개")

    # ── ③ 주체별 이벤트 수 ──────────────────────────────────────────────
    print(f"\n■ ③ 주체(scope)별 이벤트 — 상위 {args.show}")
    rows = q(
        "MATCH (e:Event) RETURN coalesce(e.scope, '(빈값)'), coalesce(e.scope_type, '?'), count(e) "
        "ORDER BY count(e) DESC"
    )
    for scope, stype, cnt in rows[: args.show]:
        print(f"    {scope[:34]:36} {stype:8} {cnt}개")
    if len(rows) > args.show:
        print(f"    … {len(rows) - args.show}개 주체 더")

    # ── ④ HAD_EVENT 연결 상태 ───────────────────────────────────────────
    print("\n■ ④ 주체 → 이벤트 연결(HAD_EVENT)")
    linked = q("MATCH ()-[r:HAD_EVENT]->(:Event) RETURN count(r)")
    print(f"    HAD_EVENT 엣지 {linked[0][0] if linked else 0}개")
    orphan = q("MATCH (e:Event) WHERE NOT ()-[:HAD_EVENT]->(e) RETURN count(e)")
    n_orphan = orphan[0][0] if orphan else 0
    if n_orphan:
        print(
            f"    ⚠️  주체에 연결되지 않은 이벤트 {n_orphan}개 ({100 * n_orphan / max(n_ev, 1):.0f}%)"
        )
        print("        → 노드는 있는데 (Game)/(Team)-[:HAD_EVENT]->(Event) 가 없습니다.")
        for row in q(
            "MATCH (e:Event) WHERE NOT ()-[:HAD_EVENT]->(e) "
            "RETURN coalesce(e.scope, '(빈값)'), coalesce(e.scope_type, '?'), "
            "coalesce(e.game, '(빈값)'), count(e) ORDER BY count(e) DESC"
        )[:6]:
            print(
                f"        scope={row[0][:22]:24} type={row[1]:8} game={row[2][:16]:18} {row[3]}개"
            )
    else:
        print("    ✅ 모든 이벤트가 주체에 연결되어 있습니다")

    # 라벨별 주체 노드
    print("\n    주체 노드 라벨:")
    for row in q("MATCH (n)-[:HAD_EVENT]->(:Event) RETURN labels(n)[0], count(DISTINCT n)"):
        print(f"      {row[0]}: {row[1]}개")

    # ── ⑤ 특정 주체 상세 ────────────────────────────────────────────────
    if args.scope:
        s = args.scope
        print(f"\n■ ⑤ '{s}' 상세")
        for lbl in ("Game", "Team", "System", "Process", "Role"):
            r = q(f"MATCH (n:{lbl}) WHERE n.name = $s RETURN count(n)", {"s": s})
            if r and r[0][0]:
                print(f"    :{lbl} 노드 있음 ({r[0][0]}개)")
        r = q(
            "MATCH (n)-[:HAD_EVENT]->(e:Event) WHERE n.name = $s RETURN count(e)",
            {"s": s},
        )
        print(f"    {s} -[HAD_EVENT]-> 이벤트: {r[0][0] if r else 0}개")

        r = q("MATCH (e:Event) WHERE e.scope = $s RETURN count(e)", {"s": s})
        print(f"    e.scope = '{s}' 인 이벤트: {r[0][0] if r else 0}개")
        r = q("MATCH (e:Event) WHERE e.game = $s RETURN count(e)", {"s": s})
        print(f"    e.game  = '{s}' 인 이벤트: {r[0][0] if r else 0}개")

        # 이름에 포함되는 다른 주체로 갔는지
        print(f"\n    '{s}' 를 이름에 포함하는 주체의 이벤트:")
        for row in q(
            "MATCH (n)-[:HAD_EVENT]->(e:Event) WHERE n.name CONTAINS $s "
            "RETURN n.name, labels(n)[0], count(e) ORDER BY count(e) DESC",
            {"s": s},
        )[:10]:
            print(f"      {row[0][:34]:36} :{row[1]:8} {row[2]}개")

        print(f"\n    scope 에 '{s}' 가 들어간 이벤트:")
        for row in q(
            "MATCH (e:Event) WHERE e.scope CONTAINS $s "
            "RETURN e.scope, coalesce(e.scope_type,'?'), count(e) ORDER BY count(e) DESC",
            {"s": s},
        )[:10]:
            print(f"      {row[0][:34]:36} {row[1]:8} {row[2]}개")

        print(f"\n    표본 (e.game 또는 e.scope 가 '{s}'):")
        for row in q(
            "MATCH (e:Event) WHERE e.game = $s OR e.scope = $s "
            "RETURN e.event_id, e.date, coalesce(e.title,''), coalesce(e.scope,''), "
            "coalesce(e.scope_type,''), coalesce(e.source_url,'') LIMIT 5",
            {"s": s},
        ):
            print(f"      {row[1]} | {row[2][:40]:42} | scope={row[3]}({row[4]})")
            print(f"        {row[5][:90]}")

    check_ledger(q, args.dept, n_ev)
    return 0


if __name__ == "__main__":
    sys.exit(main())
