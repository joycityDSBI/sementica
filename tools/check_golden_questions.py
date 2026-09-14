#!/usr/bin/env python3
"""
골든셋 문항 품질 점검 — 답할 수 없는 문항이 섞이면 평가값이 낮게 나옵니다
─────────────────────────────────────────────────────────────────────────────
검색 시스템은 **질문만** 받습니다. 그런데 골든셋 생성기가 문서를 보면서 질문을
만들다 보니, 문서를 아는 사람에게만 말이 되는 문항이 섞입니다:

    "이 제보 문서에서 AI가 판단을 확신하지 못한 이유는?"   ← 어느 제보?
    "제시된 테이블에서 세 번째 열 기준 가장 낮은 행은?"    ← 어느 테이블?

LLM 판정자는 이걸 못 거릅니다 — **원문을 보면서 판단하기 때문에** "이 제보
문서" 가 모호하지 않습니다. 판정자가 평가 대상보다 많이 알면 그 관문은
작동하지 않습니다.

실측(golden_v2 90문항): holdout 2건·dev 1건이 이 유형으로 0점을 받았습니다.
시스템 결함이 아니라 문항 결함이며, 그만큼 평가값이 실제보다 낮게 나옵니다.

이 도구는 기존 골든셋을 점검합니다. 생성기(gen_golden_set.py)에는 같은 판정이
이미 들어가 있어 새로 만드는 문항은 걸러집니다.

실행:
    python tools/check_golden_questions.py data/eval/golden_v2_dev.json
    python tools/check_golden_questions.py data/eval/*.json
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "eval"))


def _load_checker():
    """gen_golden_set 에서 판정 함수만 꺼냅니다.

    그 모듈은 import 시 Vertex 클라이언트를 만들기 때문에 통째로 import 할 수
    없습니다. 그렇다고 여기서 규칙을 다시 쓰면 두 벌이 되어 어긋납니다 —
    이번 세션에서 이미 겪은 실수라 AST 로 해당 정의만 실행합니다.
    """
    import ast
    import re

    src = (ROOT / "src" / "eval" / "gen_golden_set.py").read_text(encoding="utf-8")
    want = {"_DEICTIC", "_TARGET_NOUN", "_CONTEXT_DEPENDENT"}
    ns: dict = {"re": re}
    for node in ast.parse(src).body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id in want for t in node.targets)
        ) or (isinstance(node, ast.FunctionDef) and node.name == "is_context_dependent"):
            exec(compile(ast.Module([node], []), "<gen>", "exec"), ns)
    fn = ns.get("is_context_dependent")
    if not fn:
        raise RuntimeError("gen_golden_set 에서 is_context_dependent 를 찾지 못했습니다")
    return fn


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__.strip().split("실행:")[-1])
        return 1

    check = _load_checker()
    total = flagged = 0
    for arg in sys.argv[1:]:
        path = Path(arg)
        if not path.exists():
            print(f"❌ 없음: {path}")
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        qs = data.get("questions", data)
        bad = [q for q in qs if check(q.get("question", ""))]
        total += len(qs)
        flagged += len(bad)
        mark = "⚠️ " if bad else "✅ "
        print(f"{mark}{path.name}: {len(qs)}문항 중 문맥 의존 {len(bad)}건")
        for q in bad:
            print(f"     {q.get('id', '?')} | {q.get('question', '')[:66]}")

    if total:
        print(f"\n  합계 {total}문항 중 {flagged}건 ({100 * flagged / total:.1f}%)")
    if flagged:
        print("\n  이 문항들은 **시스템이 답할 수 없습니다** — 평가값을 실제보다 낮춥니다.")
        print("  기존 골든셋에서 지우면 이전 회차와 총점 비교가 깨지므로, 다음 골든셋을")
        print("  만들 때 자연히 빠지도록 두는 편이 낫습니다 (생성기에 판정이 들어가 있습니다).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
