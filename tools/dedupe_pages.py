#!/usr/bin/env python3
"""
중복 수집 파일 정리 — page_id 하나당 .md 한 개만 남깁니다.
─────────────────────────────────────────────────────────────────────────────
notion_fetch 의 파일명이 예전에는 `{수집 회차 순번}_{제목}.md` 였습니다.
fetch 를 다시 돌릴 때마다 같은 페이지에 다른 번호가 붙어 새 파일이 쌓였고,
실측으로 파일 1258개가 실제로는 299개 페이지였습니다(4.2배).

인제스트는 파일 단위로 돌므로 같은 페이지를 네 번씩 LLM 에 넣었고, 저장 시점에
서로 덮어써서(Qdrant 포인트 ID·notion_pages·이벤트 모두 page_id/URL 기준)
로그의 "1225개 저장"과 저장소의 268개가 어긋났습니다. 데이터가 손실된 것은
아니지만, 시간과 비용의 3/4 을 중복 작업에 쓰고 있었습니다.

page_id 별로 **가장 최근 파일 하나만 남기고** 나머지를 지웁니다.
기본은 미리보기이며, 실제 삭제는 --apply 를 붙여야 합니다.

실행:
    python tools/dedupe_pages.py --dept strategic
    python tools/dedupe_pages.py --dept strategic --apply
"""

import argparse
import collections
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "pipeline"))


def main() -> int:
    ap = argparse.ArgumentParser(description="중복 수집 파일 정리")
    ap.add_argument("--dept", default="strategic")
    ap.add_argument("--apply", action="store_true", help="실제로 삭제 (기본: 미리보기)")
    args = ap.parse_args()

    from ingest import parse_md

    from dept_config import load_dept

    data_dir = load_dept(args.dept)["data_dir"] / "notion_pages"
    files = sorted(data_dir.glob("*.md"))
    if not files:
        print(f"❌ .md 파일이 없습니다: {data_dir}")
        return 1

    by_id: dict = collections.defaultdict(list)
    no_id: list = []
    for f in files:
        pid = parse_md(f)["meta"].get("page_id", "")
        (by_id[pid] if pid else no_id).append(f)

    dupes = {pid: fs for pid, fs in by_id.items() if len(fs) > 1}
    to_delete: list = []
    for fs in dupes.values():
        # 가장 최근에 받은 것을 남깁니다 (내용이 가장 최신일 가능성이 높음)
        fs_sorted = sorted(fs, key=lambda p: p.stat().st_mtime, reverse=True)
        to_delete.extend(fs_sorted[1:])

    print(f"  디렉터리: {data_dir}")
    print(f"  파일 {len(files)}개 / 고유 page_id {len(by_id)}개")
    if no_id:
        print(f"  ⚠️  page_id 없는 파일 {len(no_id)}개 — 손대지 않습니다")
        for f in no_id[:3]:
            print(f"       {f.name}")
    print(f"  중복 page_id {len(dupes)}개 / 삭제 대상 {len(to_delete)}개")

    if not to_delete:
        print("  ✅ 중복 없음")
        return 0

    for pid, fs in list(dupes.items())[:3]:
        keep = max(fs, key=lambda p: p.stat().st_mtime)
        print(f"\n    page_id {pid[:16]}… ({len(fs)}개)")
        print(f"      유지: {keep.name}")
        for f in sorted(fs, key=lambda p: p.stat().st_mtime, reverse=True)[1:3]:
            print(f"      삭제: {f.name}")
    if len(dupes) > 3:
        print(f"\n    … 외 {len(dupes) - 3}개 page_id")

    if not args.apply:
        print("\n  미리보기입니다. 실제로 지우려면 --apply 를 붙이세요.")
        print(f"  적용 후 파일 수: {len(files) - len(to_delete)}개")
        return 0

    removed = 0
    for f in to_delete:
        try:
            f.unlink()
            removed += 1
        except OSError as e:
            print(f"  ⚠️  삭제 실패 {f.name}: {e}")
    print(f"\n  ✅ {removed}개 삭제 — 남은 파일 {len(files) - removed}개")
    print("  이제 재인제스트하면 페이지당 1회만 처리합니다:")
    print(f"    python src/pipeline/ingest.py --dept {args.dept} --reset --workers 10")
    return 0


if __name__ == "__main__":
    sys.exit(main())
