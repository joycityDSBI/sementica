#!/usr/bin/env python3
"""
has_html_attachment 백필 스크립트
────────────────────────────────────────────────────────────────────────────────
수집된 .md 파일을 스캔해 [첨부 HTML: 마커가 있는 페이지를
PostgreSQL notion_pages 테이블의 has_html_attachment=TRUE로 업데이트합니다.

사용법:
  python tools/backfill_html_flag.py --dept strategic
  python tools/backfill_html_flag.py --dept strategic --dry-run

실행 순서 (서버):
  1. git pull                                    # 최신 코드 적용
  2. python src/pipeline/notion_fetch.py ...     # 페이지 재수집 (HTML 추출)
  3. python tools/backfill_html_flag.py ...      # DB 백필 (본 스크립트)
"""

import argparse
import os
import re
import sys
from pathlib import Path

# 프로젝트 루트를 sys.path에 추가
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "ops"))

# ── .env 로드 ─────────────────────────────────────────────────────────────────
_env_path = ROOT / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# ── frontmatter page_id 추출 ──────────────────────────────────────────────────
_RE_PAGE_ID = re.compile(r"^page_id:\s*(.+)$", re.MULTILINE)
_HTML_MARKER = "[첨부 HTML:"


def _extract_page_id(md_text: str) -> str | None:
    """YAML frontmatter에서 page_id를 추출합니다."""
    m = _RE_PAGE_ID.search(md_text)
    if not m:
        return None
    raw = m.group(1).strip().strip('"').strip("'")
    # 32자 UUID(대시 제거) 또는 36자 UUID(대시 포함) 모두 허용
    pid = raw.replace("-", "")
    return pid[:32] if len(pid) >= 32 else None


def scan_data_dir(data_dir: Path) -> dict[str, bool]:
    """
    data_dir 아래 모든 .md 파일을 스캔합니다.

    Returns:
        {page_id: has_html_attachment}  — 모든 page_id 포함 (False도 포함)
    """
    result: dict[str, bool] = {}
    md_files = list(data_dir.rglob("*.md"))
    print(f"  📂 {data_dir}  →  {len(md_files)}개 .md 파일 발견")

    for md_path in md_files:
        try:
            text = md_path.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            print(f"  ⚠️  읽기 실패 {md_path.name}: {e}")
            continue

        pid = _extract_page_id(text)
        if not pid:
            continue

        has_html = _HTML_MARKER in text
        result[pid] = has_html

    return result


def backfill(dept: str, data_dir: Path, dry_run: bool = False) -> None:
    """has_html_attachment 값을 DB에 반영합니다."""
    page_flags = scan_data_dir(data_dir)

    html_pages = [pid for pid, v in page_flags.items() if v]
    total_pages = len(page_flags)

    print(f"\n  총 페이지: {total_pages}개  /  HTML 첨부 있음: {len(html_pages)}개")

    if not html_pages:
        print("  ℹ️  [첨부 HTML:] 마커가 발견된 .md 파일이 없습니다.")
        print("     → 페이지를 먼저 재수집해야 합니다:")
        print(f"       python src/pipeline/notion_fetch.py --dept {dept}")
        return

    if dry_run:
        print("\n  [DRY-RUN] 아래 page_id에 has_html_attachment=TRUE 설정 예정:")
        for pid in html_pages:
            print(f"    {pid}")
        return

    # ── PostgreSQL 업데이트 ──────────────────────────────────────────────────
    POSTGRES_URL = os.environ.get("POSTGRES_URL", "")
    if not POSTGRES_URL:
        print("  ❌ POSTGRES_URL 환경변수가 없습니다. .env 파일을 확인하세요.")
        return

    try:
        import psycopg2
    except ImportError:
        print("  ❌ psycopg2가 없습니다: pip install psycopg2-binary")
        return

    try:
        conn = psycopg2.connect(POSTGRES_URL)
    except Exception as e:
        print(f"  ❌ DB 연결 실패: {e}")
        return

    updated = 0
    reset = 0
    errors = 0

    try:
        with conn, conn.cursor() as cur:
            # HTML 있는 페이지 → TRUE
            cur.executemany(
                "UPDATE notion_pages SET has_html_attachment=TRUE "
                "WHERE page_id=%s AND dept=%s AND has_html_attachment=FALSE",
                [(pid, dept) for pid in html_pages],
            )
            updated = cur.rowcount

            # HTML 없는 페이지 → FALSE (재수집 후 사라진 경우 보정)
            no_html = [pid for pid, v in page_flags.items() if not v]
            if no_html:
                cur.executemany(
                    "UPDATE notion_pages SET has_html_attachment=FALSE "
                    "WHERE page_id=%s AND dept=%s AND has_html_attachment=TRUE",
                    [(pid, dept) for pid in no_html],
                )
                reset = cur.rowcount

        print("\n  ✅ 업데이트 완료")
        print(f"     has_html_attachment TRUE  설정: {updated}개")
        if reset:
            print(f"     has_html_attachment FALSE 재설정: {reset}개 (HTML 없는 페이지)")

    except Exception as e:
        print(f"  ❌ DB 업데이트 실패: {e}")
        errors += 1
    finally:
        conn.close()

    if errors == 0:
        print("\n  대시보드를 새로고침하면 📎 아이콘이 표시됩니다.")


# ── 엔트리포인트 ──────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="has_html_attachment DB 백필")
    parser.add_argument("--dept", default="strategic", help="본부 키 (departments.yaml)")
    parser.add_argument("--dry-run", action="store_true", help="변경 없이 대상 목록만 출력")
    args = parser.parse_args()

    # 본부별 data_dir 결정
    try:
        import yaml

        cfg_path = ROOT / "config" / "departments.yaml"
        with cfg_path.open(encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        data_dir_str = cfg["departments"][args.dept].get("data_dir", f"data/{args.dept}")
        data_dir = ROOT / data_dir_str
    except Exception:
        data_dir = ROOT / "data" / args.dept

    print(f"🔍 백필 시작  dept={args.dept}  data_dir={data_dir}")

    if not data_dir.exists():
        print(f"  ❌ 데이터 디렉토리가 없습니다: {data_dir}")
        sys.exit(1)

    backfill(args.dept, data_dir, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
