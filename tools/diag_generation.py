#!/usr/bin/env python3
"""
생성 단계 진단기
─────────────────────────────────────────────────────────────────────────────
검색·전달이 정상인데 LLM이 "컨텍스트에 없습니다"라고 답하는 문항을 대상으로,
원인이 컨텍스트 길이인지 프롬프트인지 가릅니다.

diag_golden_miss.py 가 "전달됨 — 생성/채점 단계 문제"로 분류한 문항에 사용합니다.

세 조건을 같은 질문에 적용해 비교합니다:

  A. 현재      — 실제 파이프라인 컨텍스트 + 현재 프롬프트
  B. 근거만    — 정답 근거 페이지 하나만 + 현재 프롬프트
                 (A 실패 / B 성공 → 컨텍스트 길이 문제)
  C. 프롬프트  — 실제 컨텍스트 + 탐색을 유도하는 프롬프트
                 (A 실패 / C 성공 → 프롬프트 문제)

실행:
    python tools/diag_generation.py --dept strategic \\
        --golden data/eval/golden_set_20260910.json --ids Q07,Q18,Q20
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "pipeline"))

from utils.llm import create_message  # noqa: E402

sys.path.insert(0, str(ROOT / "src" / "eval"))

_env = ROOT / ".env"
if _env.exists():
    for raw in _env.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

GCP_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
ANTHROPIC_REGION = os.environ.get("ANTHROPIC_VERTEX_REGION", "global")
CLAUDE_MODEL = "claude-sonnet-4-6@default"

# 현재 evaluate.py 가 쓰는 프롬프트
PROMPT_CURRENT = """아래 컨텍스트를 바탕으로 질문에 답하세요. 컨텍스트에 없는 내용은 답하지 마세요.

컨텍스트:
{context}

질문: {question}

답변 (간결하게):"""

# 탐색을 유도하는 프롬프트 후보
PROMPT_SEEK = """아래 컨텍스트에서 질문의 답을 찾아 답변하세요.

컨텍스트:
{context}

질문: {question}

지침:
- 컨텍스트는 여러 문서로 구성되어 있습니다. 끝까지 확인하세요.
- 답의 근거가 여러 문서에 나뉘어 있을 수 있습니다.
- 표현이 질문과 다를 수 있습니다. 의미가 같으면 근거로 사용하세요.
- 근거를 찾지 못한 경우에만 "자료에서 확인되지 않음"이라고 답하세요.

답변 (간결하게):"""


def judge(claude, question: str, answer: str, response: str) -> tuple[float, str]:
    """evaluate.py 와 동일한 기준으로 채점."""
    prompt = f"""당신은 검색 시스템 평가자입니다.

질문: {question}
정답: {answer}
검색 결과에서 생성된 응답: {response[:2500]}

위 응답이 정답을 얼마나 잘 포함하고 있는지 채점하세요.
- 1.0: 정답의 핵심 정보를 완전히 포함
- 0.5: 부분적으로 포함
- 0.0: 관련 없거나 틀림

JSON으로만: {{"score": 0.0, "reason": "한 줄"}}"""
    try:
        msg = create_message(
            claude,
            model=CLAUDE_MODEL,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            d = json.loads(m.group())
            return float(d.get("score", 0.0)), str(d.get("reason", ""))
    except Exception as exc:
        return 0.0, f"채점 실패: {exc}"
    return 0.0, "파싱 실패"


def generate(claude, prompt_tpl: str, context: str, question: str) -> str:
    try:
        msg = create_message(
            claude,
            model=CLAUDE_MODEL,
            max_tokens=800,
            messages=[
                {"role": "user", "content": prompt_tpl.format(context=context, question=question)}
            ],
        )
        return msg.content[0].text.strip()
    except Exception as exc:
        return f"생성 실패: {exc}"


def main() -> int:
    ap = argparse.ArgumentParser(description="생성 단계 진단")
    ap.add_argument("--dept", default="strategic")
    ap.add_argument("--golden", required=True)
    ap.add_argument("--ids", default="", help="대상 문항 ID (쉼표 구분). 없으면 전체")
    args = ap.parse_args()

    # evaluate.py 의 검색 파이프라인을 그대로 사용
    os.environ.setdefault("EVAL_DEPT", args.dept)
    sys.argv = ["evaluate.py", "--dept", args.dept]
    import evaluate as ev

    embed_client, qdrant, graph, claude_ev = ev.init_clients()
    from anthropic import AnthropicVertex

    claude = AnthropicVertex(project_id=GCP_PROJECT, region=ANTHROPIC_REGION)

    golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    questions = {q["id"]: q for q in golden.get("questions", golden)}
    targets = (
        [i.strip() for i in args.ids.split(",") if i.strip() in questions]
        if args.ids
        else list(questions)
    )

    from utils.retrieval import COVERAGE_BOOST, PAGE_MAX_CHARS

    print(f"  대상 {len(targets)}문항 | PAGE_MAX_CHARS={PAGE_MAX_CHARS} ")
    print(
        f"  CONTEXT_MAX_CHARS={ev.CONTEXT_MAX_CHARS} "
        f"MAX_CONTEXT_DOCS={ev.MAX_CONTEXT_DOCS} COVERAGE_BOOST={COVERAGE_BOOST}\n"
    )

    summary: list = []
    for qid in targets:
        q = questions[qid]
        question, answer, url = q["question"], q["answer"], q.get("source_url", "")
        print("=" * 74)
        print(f"  {qid}  {question[:64]}")
        print(f"  정답: {answer[:64]}")

        sr = ev.hybrid_search(embed_client, qdrant, graph, question, claude=claude_ev)
        ctx = sr["combined_context"]
        docs = sr["semantic"]

        # 근거 페이지가 컨텍스트에 실제로 들어갔는지
        # (키는 utils.retrieval 통합 후 source_url/content 로 통일됨)
        # 예산이 찰 때까지 채우므로 실제 사용된 문서 수는 질문마다 다릅니다.
        used = sr.get("used_docs", len(docs))
        in_ctx = any(d.get("source_url") == url for d in docs[:used])
        rank = next((i + 1 for i, d in enumerate(docs) if d.get("source_url") == url), None)
        print(f"  컨텍스트 {len(ctx)}자 | 검색 {len(docs)}건 | 컨텍스트 투입 {used}건 ", end="")
        print(f"| 근거 순위 {rank} | 포함: {'예' if in_ctx else '아니오'}")
        if sr.get("decomposed"):
            print(f"  서브쿼리: {sr.get('sub_queries')}")

        # 상위 문서의 순위·점수·coverage — 짧은 정답 문서가 밀렸는지 확인용.
        # coverage 부스트는 여러 서브쿼리에 걸린 문서를 올리므로, 한 서브쿼리에만
        # 걸리는 짧고 구체적인 문서가 불리해질 수 있습니다.
        print("  순위  점수    cov  길이   문서")
        for i, d in enumerate(docs[: used + 3], 1):
            mark = " ←근거" if d.get("source_url") == url else ""
            cut = "  " if i <= used else " ✂"
            print(
                f"  {cut}{i:2}  {d.get('score', 0):.4f}  {d.get('coverage', 1):>2}  "
                f"{len(d.get('content', '')):>5}  {d.get('title', '')[:30]}{mark}"
            )

        # A. 현재 조건
        a_resp = generate(claude, PROMPT_CURRENT, ctx[: ev.CONTEXT_MAX_CHARS], question)
        a_score, a_why = judge(claude, question, answer, a_resp)
        print(f"\n  A 현재     : {a_score:.1f}  {a_resp[:88]}")

        # B. 근거 페이지만
        only = next((d for d in docs if d.get("source_url") == url), None)
        if only:
            b_ctx = f"[{only['title']}]\n{only['content']}"
            b_resp = generate(claude, PROMPT_CURRENT, b_ctx, question)
            b_score, _ = judge(claude, question, answer, b_resp)
            print(f"  B 근거만   : {b_score:.1f}  ({len(b_ctx)}자)  {b_resp[:70]}")
        else:
            b_score = -1.0
            print("  B 근거만   : (근거 페이지가 검색 결과에 없어 생략)")

        # C. 탐색 유도 프롬프트
        c_resp = generate(claude, PROMPT_SEEK, ctx[: ev.CONTEXT_MAX_CHARS], question)
        c_score, _ = judge(claude, question, answer, c_resp)
        print(f"  C 프롬프트 : {c_score:.1f}  {c_resp[:88]}")

        if a_score >= 0.7:
            verdict = "재현 안 됨 (평가 시점과 다름)"
        elif rank is None:
            verdict = "검색 미스 — 근거 페이지가 결과에 없음"
        elif not in_ctx:
            verdict = f"랭킹 — 근거가 {rank}위라 투입 {used}건 밖으로 밀림"
        elif b_score >= 0.7 and c_score < 0.7:
            verdict = "컨텍스트 — 다른 문서에 정답이 묻힘"
        elif c_score >= 0.7:
            verdict = "프롬프트 — 과도하게 보수적"
        elif b_score < 0.7:
            verdict = "근거 페이지만으로도 실패 — 근거 판정 재확인 필요"
        else:
            verdict = "불명확"
        print(f"  → {verdict}")
        print(f"     A 사유: {a_why[:70]}\n")
        summary.append((qid, a_score, b_score, c_score, verdict))

    print("=" * 74)
    print("  요약        A(현재)  B(근거만)  C(프롬프트)  판정")
    for qid, a, b, c, v in summary:
        bs = "  -  " if b < 0 else f" {b:.1f} "
        print(f"    {qid:5}      {a:.1f}      {bs}      {c:.1f}      {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
