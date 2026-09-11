#!/usr/bin/env python3
"""
temperature 전달 통로 확인 — 인제스트 전에 1회 호출로 검증합니다.
─────────────────────────────────────────────────────────────────────────────
anthropic 0.99.0 에는 temperature 명명 인자가 있고 1.2.0 에는 없습니다.
1.2.0 에는 대신 output_config / extra_body 가 있는데, 시그니처에 있다고 해서
API 가 받아준다는 보장은 없습니다. 확인 없이 재인제스트를 돌렸다가 모든 LLM
호출이 TypeError 로 죽어 그래프가 비고 몇 시간을 날린 적이 있습니다.

이 도구는 후보 통로마다 **최소 토큰으로 한 번씩** 실제 호출해 무엇이 통하는지
알려줍니다. 몇 초면 끝나고 DB 는 건드리지 않습니다.

실행:
    python tools/probe_llm.py
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

_env = ROOT / ".env"
if _env.exists():
    for raw in _env.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

GCP_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
ANTHROPIC_REGION = os.environ.get("ANTHROPIC_VERTEX_REGION", "global")
MODEL = os.environ.get("PROBE_MODEL", "claude-haiku-4-5@20251001")

# extra_body 가 1.x 의 공식 경로입니다 (SDK 마이그레이션 문서).
# output_config 는 시그니처에 있지만 샘플링 설정 자리가 아니므로 시험하지 않습니다.
CANDIDATES = [
    ("temperature=", {"temperature": 0}),
    ("extra_body=", {"extra_body": {"temperature": 0}}),
]


def main() -> int:
    import anthropic
    from anthropic import AnthropicVertex

    print(f"  anthropic {getattr(anthropic, '__version__', '?')} | 모델 {MODEL}\n")

    client = AnthropicVertex(project_id=GCP_PROJECT, region=ANTHROPIC_REGION)
    base = {"model": MODEL, "max_tokens": 4, "messages": [{"role": "user", "content": "1"}]}

    winner = None
    for label, extra in CANDIDATES:
        try:
            client.messages.create(**base, **extra)
        except Exception as e:
            head = str(e).replace("\n", " ")[:90]
            print(f"  ❌ {label:<16} {type(e).__name__}: {head}")
            continue
        print(f"  ✅ {label:<16} 통과")
        if winner is None:
            winner = label

    print()
    if not winner:
        print("  → 어떤 통로도 통하지 않습니다. temperature 없이(기본 1.0) 동작하며,")
        print("     같은 문서에서도 추출 결과가 회차마다 달라집니다.")
        print("     인제스트는 가능하지만 그래프 재현성은 포기해야 합니다.")
        return 0

    print(f"  → '{winner}' 통로가 오류 없이 통과했습니다.\n")

    # ── 값이 실제로 반영되는지 확인 ──────────────────────────────────────
    # 통로가 열렸다는 것과 값이 적용된다는 것은 다릅니다. 인자를 조용히
    # 무시하는 통로를 "성공"으로 받아들이면, 온도 0 인 줄 알면서 1.0 으로
    # 도는 최악이 됩니다 — 실측으로 추출 재현성이 45.9% 였습니다.
    # 엔트로피가 높은 프롬프트를 같은 조건으로 두 번 호출해 비교합니다.
    print("  값이 실제로 반영되는지 확인 중 (같은 프롬프트 2회씩)...")
    entropy = {
        "model": MODEL,
        "max_tokens": 60,
        "messages": [
            {"role": "user", "content": "무작위 한국어 단어 10개를 쉼표로 나열하세요. 설명 없이."}
        ],
    }
    _, extra = next(c for c in CANDIDATES if c[0] == winner)

    def _twice(kw: dict) -> tuple[str, str]:
        out = []
        for _ in range(2):
            r = client.messages.create(**entropy, **kw)
            out.append(r.content[0].text.strip())
        return out[0], out[1]

    a0, b0 = _twice(extra)
    a1, b1 = _twice({} if winner == "temperature=" else {"extra_body": {"temperature": 1.0}})

    same0 = a0 == b0
    same1 = a1 == b1
    print(f"    온도 0   2회 동일: {'예' if same0 else '아니오'}")
    print(f"    온도 1.0 2회 동일: {'예' if same1 else '아니오'}")
    print()

    if same0 and not same1:
        print("  ✅ temperature 가 실제로 적용됩니다 (0 에서 고정, 1.0 에서 변동).")
    elif same0 and same1:
        print("  ⚠️  두 온도 모두 동일 — 프롬프트가 너무 쉬워 판별되지 않았을 수 있습니다.")
        print("     추출 재현성은 tools/check_extraction_stability.py 로 확인하세요.")
    else:
        print("  ❌ 온도 0 인데도 결과가 달라집니다 — 값이 전달되지 않거나 무시됩니다.")
        print(f"     1회차: {a0[:60]}")
        print(f"     2회차: {b0[:60]}")
        print("     그래프 재현성을 확보할 수 없습니다. 추출 안정화는 온도가 아닌")
        print("     다른 수단(프롬프트 제약·엔티티 명명 규칙)으로 접근해야 합니다.")

    from utils.llm import create_message, strategy

    try:
        create_message(client, temperature=0, **base)
        print(f"\n  → utils.llm 선택: {strategy()}")
    except Exception as e:
        print(f"  ⚠️  래퍼 호출 실패: {type(e).__name__}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
