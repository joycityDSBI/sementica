#!/usr/bin/env python3
"""
트리플 추출 재현성 측정 — 같은 문서를 두 번 추출해 결과를 비교합니다.
─────────────────────────────────────────────────────────────────────────────
온도 1.0 으로 샘플링하던 시절, 재인제스트마다 그래프가 달라졌습니다.
실측 예: "퍼포먼스팀 →[검토]→ GBTW 9월 마케팅 믹스표" 가 [작성] 로 바뀌고,
"ADNW/빅미디어 규모-효율 진단" 엔티티는 아예 사라졌습니다. 그래프는 영구
저장물이므로 이런 흔들림이 그대로 남고, 관계 질문의 답이 회차마다 달라집니다.

전체 재인제스트는 몇 시간이 걸리므로, 여기서는 **소수 페이지를 두 번 추출해**
차이를 직접 셉니다. 몇 분이면 끝나고 DB 를 건드리지 않습니다.

지표:
  일치율   두 회차에 모두 나온 트리플 / 두 회차 합집합 (Jaccard)
  관계명만 다름   같은 (주어, 목적어) 인데 관계명이 바뀐 건수
                 — GBTW 사례가 이 유형입니다

실행:
    python tools/check_extraction_stability.py --dept strategic --pages 10
    python tools/check_extraction_stability.py --pages 5 --temperature 1.0  # 비교용
"""

import argparse
import os
import random
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

GCP_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
ANTHROPIC_REGION = os.environ.get("ANTHROPIC_VERTEX_REGION", "global")


def _triple_set(items: list) -> set:
    """(주어, 관계, 목적어) 집합 — 비교용 정규화."""
    return {
        (
            (t["subject"]["name"] or "").strip(),
            (t["predicate"]["name"] or "").strip(),
            (t["object"]["name"] or "").strip(),
        )
        for t in items
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="트리플 추출 재현성 측정")
    ap.add_argument("--dept", default="strategic")
    ap.add_argument("--pages", type=int, default=10, help="검사할 페이지 수")
    ap.add_argument("--temperature", type=float, default=-1.0, help="기본: 파이프라인 설정값")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    import ingest as ing
    from anthropic import AnthropicVertex

    from dept_config import load_dept

    temp = ing.EXTRACT_TEMPERATURE if args.temperature < 0 else args.temperature
    data_dir = load_dept(args.dept)["data_dir"] / "notion_pages"
    files = sorted(data_dir.glob("*.md"))
    if not files:
        print(f"❌ .md 파일이 없습니다: {data_dir}")
        return 1

    random.seed(args.seed)
    random.shuffle(files)

    client = AnthropicVertex(project_id=GCP_PROJECT, region=ANTHROPIC_REGION)

    def extract(text: str) -> list:
        resp = client.messages.create(
            model=ing.HAIKU_MODEL,
            max_tokens=2048,
            temperature=temp,
            messages=[{"role": "user", "content": ing.EXTRACT_PROMPT.format(text=text[:3000])}],
        )
        import json

        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = (parts[1] if len(parts) > 1 else raw).removeprefix("json")
        # evidence_quote 없는 트리플은 파이프라인에서도 버리므로 동일하게 제외
        return [
            {
                "subject": ing._norm_node(t.get("subject", "")),
                "predicate": ing._norm_pred(t.get("predicate", "")),
                "object": ing._norm_node(t.get("object", "")),
            }
            for t in json.loads(raw.strip())
            if isinstance(t, dict) and (t.get("evidence_quote") or "").strip()
        ]

    print(f"  온도 {temp} | 페이지 {args.pages}개를 각각 2회 추출\n")
    tot_same = tot_union = tot_renamed = 0
    checked = 0

    for path in files:
        if checked >= args.pages:
            break
        page = ing.parse_md(path)
        body = page["body"]
        if len(body.split()) < 50:
            continue
        try:
            a, b = _triple_set(extract(body)), _triple_set(extract(body))
        except Exception as e:
            print(f"  ⚠️  {path.name[:40]}: {type(e).__name__}: {e}")
            continue

        checked += 1
        same, union = a & b, a | b
        only_a, only_b = a - b, b - a
        # 같은 (주어, 목적어) 인데 관계명만 바뀐 경우
        pairs_a = {(s, o): p for s, p, o in only_a}
        renamed = sum(1 for s, p, o in only_b if (s, o) in pairs_a and pairs_a[(s, o)] != p)

        tot_same += len(same)
        tot_union += len(union)
        tot_renamed += renamed
        rate = len(same) / len(union) if union else 1.0
        flag = "✅" if rate >= 0.95 else ("⚠️ " if rate >= 0.8 else "❌")
        print(
            f"  {flag} {path.name[:44]:<44} {len(a):>3}/{len(b):>3}개 "
            f"일치 {rate:>5.0%}" + (f"  관계명변경 {renamed}" if renamed else "")
        )
        for s, p, o in sorted(only_a)[:2]:
            print(f"       1회차만: {s} →[{p}]→ {o}")
        for s, p, o in sorted(only_b)[:2]:
            print(f"       2회차만: {s} →[{p}]→ {o}")

    if not checked:
        print("  검사할 페이지가 없습니다.")
        return 1

    overall = tot_same / tot_union if tot_union else 1.0
    print(f"\n  전체 일치율 {overall:.1%}  (페이지 {checked}개, 합집합 {tot_union}개 트리플)")
    if tot_renamed:
        print(f"  관계명만 바뀐 트리플 {tot_renamed}개 — GBTW 사례와 같은 유형")
    if overall >= 0.95:
        print("  ✅ 재현성 확보 — 재인제스트해도 그래프가 거의 같습니다")
    elif overall >= 0.8:
        print("  ⚠️  부분적으로 흔들립니다 — 관계 질문의 답이 회차마다 달라질 수 있습니다")
    else:
        print("  ❌ 재현성 없음 — 온도만으로는 해결되지 않습니다")
    return 0


if __name__ == "__main__":
    sys.exit(main())
