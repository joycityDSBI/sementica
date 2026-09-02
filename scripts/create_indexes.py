"""
FalkorDB 인덱스 생성 스크립트
==============================
각 레이블·속성에 대한 인덱스가 없으면 성능이 크게 저하됩니다.
이 스크립트는 모든 필수 인덱스를 멱등적으로 생성합니다.
(이미 존재하는 인덱스는 건너뜁니다.)

사용법:
    python scripts/create_indexes.py --all          # 모든 부서 그래프
    python scripts/create_indexes.py --dept strategic
    python scripts/create_indexes.py --dept strategic --dept game
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 프로젝트 루트를 PYTHONPATH에 추가
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from falkordb import FalkorDB  # type: ignore

from src.pipeline.dept_config import list_depts

# ── 생성할 인덱스 목록 (label, property) ──────────────────────────────────────
# FalkorDB: CREATE INDEX FOR (n:Label) ON (n.prop)
INDEX_SPECS: list[tuple[str, str]] = [
    # ── 공통 노드 ────────────────────────────────────────────────────
    ("Person",    "name"),
    ("Team",      "name"),
    ("Process",   "name"),
    ("System",    "name"),
    ("Policy",    "name"),
    ("Document",  "name"),
    ("Role",      "name"),
    # ── Decision 온톨로지 ─────────────────────────────────────────────
    ("Decision",  "subject"),
    ("Decision",  "outcome"),
    ("Decision",  "date"),
    # ── Event·Game 온톨로지 ───────────────────────────────────────────
    ("Event",     "game"),
    ("Event",     "event_type"),
    ("Event",     "date_ts"),          # range 탐색 핵심
    ("Game",      "name"),
]


def _create_indexes_for_dept(graph_name: str, client: FalkorDB) -> None:
    """단일 그래프에 모든 인덱스를 생성합니다."""
    graph = client.select_graph(graph_name)
    print(f"\n[{graph_name}] 인덱스 생성 시작…")

    created = 0
    skipped = 0
    failed  = 0

    for label, prop in INDEX_SPECS:
        cypher = f"CREATE INDEX FOR (n:{label}) ON (n.{prop})"
        try:
            graph.query(cypher)
            print(f"  ✅ {label}.{prop}")
            created += 1
        except Exception as exc:
            msg = str(exc).lower()
            # "already indexed" 계열 메시지는 정상 (멱등)
            if "already indexed" in msg or "already exists" in msg or "equivalent index" in msg:
                print(f"  ⏭  {label}.{prop} (이미 존재)")
                skipped += 1
            else:
                print(f"  ❌ {label}.{prop} — {exc}")
                failed += 1

    print(
        f"[{graph_name}] 완료: 생성 {created}, 기존 {skipped}, 실패 {failed}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="FalkorDB 인덱스 생성")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="모든 부서 그래프에 인덱스 생성")
    group.add_argument(
        "--dept", action="append", metavar="DEPT",
        help="특정 부서(그래프) 지정 (복수 가능: --dept strategic --dept game)",
    )
    parser.add_argument("--host", default="localhost", help="FalkorDB 호스트 (기본: localhost)")
    parser.add_argument("--port", type=int, default=6379, help="FalkorDB 포트 (기본: 6379)")
    args = parser.parse_args()

    client = FalkorDB(host=args.host, port=args.port)

    if args.all:
        depts = list_depts()
        if not depts:
            print("❌ departments.yaml 에 부서가 없습니다.")
            sys.exit(1)
    else:
        depts = args.dept  # type: ignore[assignment]

    print(f"대상 그래프: {depts}")

    for dept in depts:
        _create_indexes_for_dept(dept, client)

    print("\n모든 인덱스 생성 완료.")


if __name__ == "__main__":
    main()
