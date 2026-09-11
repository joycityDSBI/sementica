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
    if winner:
        print(f"  → '{winner}' 로 temperature 가 전달됩니다. 재현성 확보 상태입니다.")
    else:
        print("  → 어떤 통로도 통하지 않습니다. temperature 없이(기본 1.0) 동작하며,")
        print("     같은 문서에서도 추출 결과가 회차마다 달라집니다.")
        print("     인제스트는 가능하지만 그래프 재현성은 포기해야 합니다.")

    # 래퍼가 같은 판단을 하는지 확인
    from utils.llm import create_message, strategy

    try:
        create_message(client, temperature=0, **base)
        print(f"  → utils.llm 선택: {strategy()}")
    except Exception as e:
        print(f"  ⚠️  래퍼 호출 실패: {type(e).__name__}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
