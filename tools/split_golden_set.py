#!/usr/bin/env python3
"""
골든셋을 dev / holdout 으로 분할 — 점수가 일반화되는지 재기 위해
─────────────────────────────────────────────────────────────────────────────
지금까지의 점수는 전부 **같은 40문항**에서 나왔습니다. 그 문항들을 보면서
파라미터를 고르고 결함을 고쳤으니, 0.900 이 시스템의 실력인지 그 40문항에
맞춰진 숫자인지 구분할 방법이 없습니다.

홀드아웃은 그 구분을 만듭니다. 규칙은 하나입니다 — **holdout 문항은 보지도,
그것으로 무엇을 고치지도 않습니다.** 한 번이라도 holdout 을 보고 튜닝하면
그 순간 그것도 dev 가 되고, 남는 게 없습니다.

  dev      평소 작업용. 얼마든지 보고 고쳐도 됩니다.
  holdout  큰 변경 뒤에만 재고, 점수만 기록합니다. 문항별 실패는 보지 않습니다.

두 점수의 **차이**가 과적합의 크기입니다. dev 0.90 / holdout 0.90 이면 실력이고,
dev 0.90 / holdout 0.75 면 0.15 만큼은 그 문항들에만 맞춰져 있던 것입니다.

■ 왜 질문이 아니라 출처 문서 단위로 나누는가

같은 페이지에서 나온 질문 둘을 양쪽으로 갈라놓으면 홀드아웃이 오염됩니다.
CHUNK_OVERSAMPLE 을 8 로 정할 때 실제로 본 것이 "근거 페이지가 컨텍스트에
들어오는가"였습니다. dev 쪽 질문을 보고 그 페이지가 검색되게 만들면, 같은
페이지를 쓰는 holdout 질문은 공짜로 맞습니다 — 재고 싶은 것이 "새 문서에도
통하는가"인데 그걸 못 재게 됩니다.

그래서 문서를 통째로 한쪽에 몰아넣습니다. 문항 수가 정확히 반반이 되지
않는 대신, 홀드아웃이 실제로 미지의 문서가 됩니다.

■ 분할은 결정적입니다

문서 URL 의 해시 순서로 처리하고, 그때그때 문항이 적은 쪽에 넣습니다.
입력 순서나 실행 시점과 무관하게 같은 결과가 나오므로, 나중에 다시 돌려도
같은 문항이 같은 쪽에 남습니다 — 이게 깨지면 홀드아웃이 홀드아웃이 아닙니다.

실행:
    python tools/split_golden_set.py data/eval/golden_set_20260911.json
    python tools/split_golden_set.py <파일> --ratio 0.5 --force
"""

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path


def golden_hash(questions: list) -> str:
    """evaluate.py 가 eval_run_log 에 남기는 것과 **같은** 해시.

    분할 직후에 찍어두면, 나중에 DB 의 어느 행이 어느 세트인지 대조할 수
    있습니다. 계산식이 갈리면 대조가 안 되므로 여기서 바꾸면 안 됩니다.
    """
    return hashlib.sha256(
        json.dumps(
            [(q.get("id"), q.get("question"), q.get("answer")) for q in questions],
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:16]


def split_by_source(questions: list, ratio: float = 0.5) -> tuple[list, list]:
    """출처 문서 단위로 dev / holdout 분할. 결정적입니다.

    출처가 없는 문항은 각자 독립 그룹으로 봅니다 (합칠 근거가 없으므로).

    >>> qs = [{"id": i, "source_url": f"u{i % 4}"} for i in range(8)]
    >>> dev, hold = split_by_source(qs)
    >>> sorted(q["id"] for q in dev), sorted(q["id"] for q in hold)
    ([0, 3, 4, 7], [1, 2, 5, 6])

    한 문서의 문항은 반드시 같은 쪽에 모입니다 — 홀드아웃이 성립하는 조건입니다:

    >>> {q["source_url"] for q in dev} & {q["source_url"] for q in hold}
    set()

    입력 순서가 달라도 같은 분할이 나옵니다:

    >>> d2, h2 = split_by_source(list(reversed(qs)))
    >>> sorted(q["id"] for q in d2) == sorted(q["id"] for q in dev)
    True

    문서 수가 홀수면 정확히 반반이 되지 않습니다. 문서를 쪼개는 것보다
    낫습니다 — 쪼개면 홀드아웃이 오염되지만, 치우침은 비율만 흔듭니다:

    >>> d3, h3 = split_by_source([{"id": i, "source_url": f"u{i % 3}"} for i in range(6)])
    >>> len(d3), len(h3)
    (4, 2)
    """
    groups: dict = defaultdict(list)
    for i, q in enumerate(questions):
        key = q.get("source_url") or f"__no_source__{q.get('id', i)}"
        groups[key].append(q)

    # 해시 순서로 처리 — 입력 순서·사전순 어느 쪽에도 기대지 않습니다.
    # 큰 그룹을 먼저 배치해야 균형이 잘 맞으므로 크기를 1순위로 둡니다.
    order = sorted(groups, key=lambda k: (-len(groups[k]), hashlib.sha256(k.encode()).hexdigest()))

    dev: list = []
    hold: list = []
    dev_c: dict = defaultdict(int)
    hold_c: dict = defaultdict(int)

    def _skew(a: dict, b: dict) -> float:
        """양쪽 구성이 목표 비율에서 얼마나 벗어났는지 — 작을수록 좋습니다."""
        return sum(abs(a[k] * (1 - ratio) - b[k] * ratio) for k in set(a) | set(b))

    for key in order:
        grp = groups[key]
        # 총 문항 수만 맞추면 카테고리가 한쪽으로 쏠립니다. 실제로 그렇게
        # 나뉘면 홀드아웃 점수가 떨어져도 그게 과적합 탓인지 카테고리 구성
        # 탓인지 구분할 수 없습니다 — 재려던 것을 못 재게 됩니다.
        # 그래서 총량과 카테고리별 균형을 함께 보고 덜 치우치는 쪽에 넣습니다.
        add: dict = defaultdict(int)
        for q in grp:
            add["__total__"] += 1
            add[f"cat:{q.get('category', '?')}"] += 1

        to_dev = _skew({k: dev_c[k] + add[k] for k in set(dev_c) | set(add)}, hold_c)
        to_hold = _skew(dev_c, {k: hold_c[k] + add[k] for k in set(hold_c) | set(add)})

        if (to_dev, len(dev)) <= (to_hold, len(hold)):
            dev.extend(grp)
            for k, v in add.items():
                dev_c[k] += v
        else:
            hold.extend(grp)
            for k, v in add.items():
                hold_c[k] += v
    return dev, hold


def _summary(name: str, qs: list, all_q: list) -> None:
    cats: dict = defaultdict(int)
    diffs: dict = defaultdict(int)
    for q in qs:
        cats[q.get("category", "?")] += 1
        diffs[q.get("difficulty", "?")] += 1
    srcs = len({q.get("source_url", "") for q in qs if q.get("source_url")})
    pct = 100 * len(qs) / len(all_q) if all_q else 0
    print(f"\n  {name}: {len(qs)}문항 ({pct:.0f}%) / 출처 문서 {srcs}개")
    print("    카테고리 " + "  ".join(f"{k} {v}" for k, v in sorted(cats.items())))
    print("    난이도   " + "  ".join(f"{k} {v}" for k, v in sorted(diffs.items())))
    print(f"    해시 {golden_hash(qs)}")


def main() -> int:
    ap = argparse.ArgumentParser(description="골든셋 dev/holdout 분할")
    ap.add_argument("path", help="골든셋 JSON (gen_golden_set.py 결과)")
    ap.add_argument("--ratio", type=float, default=0.5, help="dev 비율 (기본 0.5)")
    ap.add_argument("--force", action="store_true", help="기존 출력 파일 덮어쓰기")
    args = ap.parse_args()

    src = Path(args.path)
    if not src.exists():
        print(f"❌ 파일이 없습니다: {src}")
        return 1

    data = json.loads(src.read_text(encoding="utf-8"))
    questions = data.get("questions") if isinstance(data, dict) else data
    if not questions:
        print("❌ questions 가 비어 있습니다")
        return 1

    dev, hold = split_by_source(questions, args.ratio)
    print(f"  원본 {src.name}: {len(questions)}문항 / 해시 {golden_hash(questions)}")
    _summary("dev    ", dev, questions)
    _summary("holdout", hold, questions)

    # 문서가 양쪽에 걸치지 않았는지 — 이게 깨지면 분할의 의미가 없습니다.
    dev_src = {q.get("source_url") for q in dev if q.get("source_url")}
    hold_src = {q.get("source_url") for q in hold if q.get("source_url")}
    overlap = dev_src & hold_src
    if overlap:
        print(f"\n  ❌ 출처 문서 {len(overlap)}개가 양쪽에 걸쳐 있습니다 — 분할 실패")
        for u in sorted(overlap)[:5]:
            print(f"       {u}")
        return 1
    print(f"\n  ✅ 겹치는 출처 문서 없음 (dev {len(dev_src)} / holdout {len(hold_src)})")

    stem = src.stem
    outs = [
        (src.parent / f"{stem}_dev.json", dev),
        (src.parent / f"{stem}_holdout.json", hold),
    ]
    exists = [p for p, _ in outs if p.exists()]
    if exists and not args.force:
        print("\n  ❌ 출력 파일이 이미 있습니다 (--force 로 덮어쓰기):")
        for p in exists:
            print(f"       {p}")
        print("     ⚠️  holdout 을 덮어쓰면 '한 번도 안 본 문항'이라는 성질이 사라집니다.")
        return 1

    for path, qs in outs:
        meta = dict(data.get("meta", {}))
        meta.update(
            {
                "split": "dev" if path.name.endswith("_dev.json") else "holdout",
                "split_from": src.name,
                "split_by": "source_url",
                "total": len(qs),
                "golden_hash": golden_hash(qs),
            }
        )
        path.write_text(
            json.dumps({"meta": meta, "questions": qs}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"  💾 {path}")

    print("\n  사용법:")
    print(f"    평소:      python src/eval/evaluate.py --dept strategic --golden {outs[0][0]}")
    print(f"    큰 변경 뒤: python src/eval/evaluate.py --dept strategic --golden {outs[1][0]}")
    print("\n  holdout 은 **점수만** 보세요. 어느 문항이 틀렸는지 보고 고치기 시작하면")
    print("  그 순간 holdout 이 아니게 되고, 일반화 여부를 잴 수단이 없어집니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
