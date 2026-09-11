#!/usr/bin/env python3
"""
중복 가능성이 있는 엔티티 노드 찾기 — 정규화 설계를 위한 실측
─────────────────────────────────────────────────────────────────────────────
LLM 추출은 같은 대상을 회차마다 다르게 부릅니다(실측 재현율 45.9%). 그 결과
같은 것이 여러 노드로 쪼개지고, 그래프 질문의 답이 흔들립니다. 관찰된 예:

    준혁            ← 박준혁 (DB 속성에는 전체 이름이 있는데 본문 추출은 축약)
    In-Joy / 모바일 프로젝트   ← 같은 트리플의 목적어가 회차마다 바뀜
    마케팅실(:Team) / 마케팅실(:Role)  ← 같은 이름이 다른 라벨로 중복

**이 도구는 아무것도 바꾸지 않습니다.** 유형별 건수와 표본만 보여줍니다.
정규화를 어디까지 할지(규칙 기반 / 용어집 확장 / 유사도 / LLM)는 이 숫자를
보고 정해야 합니다.

유형:
  ① 라벨만 다름     이름이 같고 라벨이 다른 노드 — 가장 확실한 중복
  ② 표기만 다름     공백·대소문자·문장부호만 다른 이름
  ③ 포함 관계       한 이름이 다른 이름에 통째로 들어감 (준혁 ⊂ 박준혁)
                    ※ "팀" ⊂ "운영팀" 같은 오탐이 섞이므로 그대로 쓰면 안 됩니다
  ④ 높은 유사도     문자 3-gram Jaccard 가 임계값 이상

각 유형마다 **엣지 수**를 함께 보여줍니다. 엣지가 0인 노드는 병합해도
검색 결과가 달라지지 않으므로 우선순위가 낮습니다.

실행:
    python tools/find_duplicate_entities.py --dept strategic
    python tools/find_duplicate_entities.py --dept strategic --min-len 3 --sim 0.7
"""

import argparse
import os
import re
import sys
from collections import defaultdict
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


def _norm(s: str) -> str:
    """표기 차이를 지운 비교용 형태 — 공백·문장부호 제거 + 소문자."""
    return re.sub(r"[\s\W_]+", "", (s or "").lower())


def _grams(s: str, n: int = 3) -> set:
    t = _norm(s)
    return {t[i : i + n] for i in range(len(t) - n + 1)} if len(t) >= n else {t}


def _sim(a: str, b: str) -> float:
    ga, gb = _grams(a), _grams(b)
    return len(ga & gb) / len(ga | gb) if ga | gb else 0.0


def main() -> int:
    ap = argparse.ArgumentParser(description="중복 가능 엔티티 실측")
    ap.add_argument("--dept", default="strategic")
    ap.add_argument("--min-len", type=int, default=2, help="포함 관계 판정 최소 길이")
    ap.add_argument("--sim", type=float, default=0.7, help="유사도 임계값")
    ap.add_argument("--show", type=int, default=8, help="유형별 표본 수")
    args = ap.parse_args()

    import falkordb

    from dept_config import load_dept

    g = falkordb.FalkorDB(host=FALKORDB_HOST, port=FALKORDB_PORT).select_graph(
        load_dept(args.dept)["falkordb_graph"]
    )

    # 노드 + 엣지 수 (:Event 는 제외 — 이름이 아니라 event_id 로 식별됩니다)
    rows = (
        g.query(
            "MATCH (n) WHERE n.name IS NOT NULL AND n.name <> '' AND NOT n:Event "
            "OPTIONAL MATCH (n)-[r:REL]-() "
            "RETURN labels(n)[0], n.name, count(r)"
        ).result_set
        or []
    )
    nodes = [(lbl or "?", name, deg) for lbl, name, deg in rows]
    print(f"  대상 노드 {len(nodes)}개 (:Event 제외)")
    lonely = sum(1 for _l, _n, d in nodes if not d)
    print(f"  그중 엣지 0개: {lonely}개 — 병합해도 검색에 영향 없음\n")

    def _fmt(lbl, name, deg):
        return f"{name}({lbl}, 엣지 {deg})"

    # ── ① 라벨만 다름 ────────────────────────────────────────────────────
    by_name = defaultdict(list)
    for lbl, name, deg in nodes:
        by_name[name].append((lbl, deg))
    same_name = {n: v for n, v in by_name.items() if len({lbl for lbl, _ in v}) > 1}
    print(f"■ ① 라벨만 다름: {len(same_name)}건")
    for name, v in sorted(same_name.items(), key=lambda x: -sum(d for _, d in x[1]))[: args.show]:
        print(f"    {name}  →  " + ", ".join(f"{lbl}(엣지 {d})" for lbl, d in v))

    # ── ② 표기만 다름 ────────────────────────────────────────────────────
    by_norm = defaultdict(list)
    for lbl, name, deg in nodes:
        by_norm[_norm(name)].append((lbl, name, deg))
    spelling = {k: v for k, v in by_norm.items() if len({n for _l, n, _d in v}) > 1 and len(k) >= 2}
    print(f"\n■ ② 표기만 다름(공백·대소문자·부호): {len(spelling)}건")
    for _k, v in sorted(spelling.items(), key=lambda x: -sum(d for *_x, d in x[1]))[: args.show]:
        print("    " + "  ≡  ".join(_fmt(lbl, n, d) for lbl, n, d in v))

    # ── ③ 포함 관계 ──────────────────────────────────────────────────────
    names = sorted({(lbl, name, deg) for lbl, name, deg in nodes}, key=lambda x: len(x[1]))
    contained: list = []
    for i, (lbl_a, a, da) in enumerate(names):
        na = _norm(a)
        if len(na) < args.min_len:
            continue
        for lbl_b, b, db in names[i + 1 :]:
            nb = _norm(b)
            if na != nb and na in nb:
                contained.append(((lbl_a, a, da), (lbl_b, b, db)))
    print(f"\n■ ③ 포함 관계: {len(contained)}건  (오탐 많음 — 그대로 병합 금지)")
    for (la, a, da), (lb, b, db) in sorted(contained, key=lambda p: -(p[0][2] + p[1][2]))[
        : args.show
    ]:
        print(f"    {_fmt(la, a, da)}  ⊂  {_fmt(lb, b, db)}")

    # ── ④ 높은 유사도 ────────────────────────────────────────────────────
    pairs: list = []
    for i, (la, a, da) in enumerate(names):
        for lb, b, db in names[i + 1 :]:
            if _norm(a) == _norm(b) or _norm(a) in _norm(b) or _norm(b) in _norm(a):
                continue  # ①②③ 에서 이미 다룸
            s = _sim(a, b)
            if s >= args.sim:
                pairs.append((s, (la, a, da), (lb, b, db)))
    pairs.sort(key=lambda x: -x[0])
    print(f"\n■ ④ 유사도 {args.sim} 이상: {len(pairs)}건")
    for s, (la, a, da), (lb, b, db) in pairs[: args.show]:
        print(f"    {s:.2f}  {_fmt(la, a, da)}  ~  {_fmt(lb, b, db)}")

    print(
        f"\n  요약 — 라벨중복 {len(same_name)} / 표기차이 {len(spelling)} / "
        f"포함 {len(contained)} / 유사 {len(pairs)}"
    )
    print("  ①②는 규칙만으로 안전하게 병합 가능합니다.")
    print("  ③④는 오탐이 섞이므로 용어집 등록이나 사람 확인이 필요합니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
