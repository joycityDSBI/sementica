#!/usr/bin/env python3
"""
비즈니스 용어집 스냅샷 생성기
─────────────────────────────────────────────────────────────────────────────
catalog.joycityplay.com 에서 용어집을 받아 config/glossary_snapshot.json 으로
저장합니다. 용어집 API에 접근할 수 없는 환경(운영 VM 등)에서 synonym_resolver
가 이 파일을 폴백으로 사용합니다.

용어집이 없으면 동의어 정규화와 이벤트 주체 판정(게임/조직)이 모두
비활성화되므로, 접근 가능한 환경에서 주기적으로 갱신해 커밋하세요.

저장 항목은 실제로 사용하는 term·synonyms·category 뿐입니다
(definition·sql_snippets 등은 제외).

실행:
    python tools/fetch_glossary_snapshot.py
    python tools/fetch_glossary_snapshot.py --out config/glossary_snapshot.json
"""

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

try:
    import httpx
except ImportError:
    raise SystemExit("httpx가 필요합니다: pip install httpx") from None

ROOT = Path(__file__).parent.parent
DEFAULT_OUT = ROOT / "config" / "glossary_snapshot.json"
DEFAULT_URL = os.environ.get("GLOSSARY_API_URL", "https://catalog.joycityplay.com/api/glossary/all")


def main() -> int:
    parser = argparse.ArgumentParser(description="용어집 스냅샷 생성")
    parser.add_argument("--url", default=DEFAULT_URL, help="용어집 API URL")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="출력 파일 경로")
    parser.add_argument("--timeout", type=float, default=15.0, help="요청 타임아웃(초)")
    args = parser.parse_args()

    print(f"  요청: {args.url}")
    try:
        resp = httpx.get(args.url, timeout=args.timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        print(f"  ❌ 용어집 조회 실패: {exc}")
        return 1

    raw_terms = data.get("terms") or []
    terms = []
    for entry in raw_terms:
        if not isinstance(entry, dict) or not entry.get("term"):
            continue
        # 사용하는 필드만 보존 — definition·sql_snippets 등은 저장하지 않음
        terms.append(
            {
                "term": entry["term"],
                "synonyms": entry.get("synonyms") or [],
                "category": str(entry.get("category") or ""),
            }
        )

    if not terms:
        print("  ❌ 용어가 하나도 없습니다 — 저장하지 않습니다")
        return 1

    by_cat: dict[str, int] = {}
    for t in terms:
        if t["category"]:
            by_cat[t["category"]] = by_cat.get(t["category"], 0) + 1

    payload = {
        "_meta": {
            "fetched_at": datetime.now(UTC).isoformat(),
            "source": args.url,
            "count": len(terms),
            "categories": by_cat,
        },
        "terms": terms,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"  ✅ 저장: {out_path}")
    print(f"     용어 {len(terms)}개, 카테고리 {by_cat or '없음'}")
    if "game" not in by_cat:
        print("     ⚠️  game 카테고리가 없습니다 — 이벤트 주체 판정이 제한됩니다")
    return 0


if __name__ == "__main__":
    sys.exit(main())
