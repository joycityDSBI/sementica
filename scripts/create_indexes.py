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
from src.pipeline.dept_config import list_depts, load_dept

# 인덱스 목록은 semantica_helper 가 정본입니다. ingest 도 --reset 직후 같은
# 목록으로 인덱스를 만드므로, 여기에 따로 두면 두 곳이 어긋납니다.
from src.pipeline.semantica_helper import INDEX_SPECS, ensure_indexes


def _create_indexes_for_dept(graph_name: str, client: FalkorDB) -> None:
    """단일 그래프에 모든 인덱스를 생성합니다."""
    graph = client.select_graph(graph_name)
    print(f"\n[{graph_name}] 인덱스 생성 시작… ({len(INDEX_SPECS)}개)")
    stats = ensure_indexes(graph, verbose=True)
    print(
        f"[{graph_name}] 완료: 생성 {stats['created']}, "
        f"기존 {stats['existing']}, 실패 {stats['failed']}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="FalkorDB 인덱스 생성")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="모든 부서 그래프에 인덱스 생성")
    group.add_argument(
        "--dept",
        action="append",
        metavar="DEPT",
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

    # 부서 키(strategic)와 그래프명(strategic_kg)은 다릅니다. 예전에는 부서 키를
    # 그대로 select_graph 에 넘겨, 인덱스가 새로 만들어진 빈 그래프에 붙고
    # 운영 그래프는 계속 풀스캔이었습니다 — 화면에는 전부 성공으로 보였습니다.
    targets: list[tuple[str, str]] = []
    for dept in depts:
        try:
            graph_name = load_dept(dept)["falkordb_graph"]
        except Exception as exc:
            print(f"❌ 부서 '{dept}' 설정을 읽을 수 없습니다: {exc}")
            sys.exit(1)
        targets.append((dept, graph_name))

    print("대상 그래프: " + ", ".join(f"{d} → {g}" for d, g in targets))

    for _dept, graph_name in targets:
        _create_indexes_for_dept(graph_name, client)

    print("\n모든 인덱스 생성 완료.")


if __name__ == "__main__":
    main()
