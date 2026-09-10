#!/usr/bin/env python3
"""
검색 파라미터 A/B 측정기
─────────────────────────────────────────────────────────────────────────────
골든셋 문항의 **근거 문서가 컨텍스트에 실제로 들어가는 비율**(recall)을
설정별로 비교합니다.

평가 점수(evaluate.py)로 파라미터를 판단하면 안 됩니다 — LLM 생성이
비결정적이라 코드가 같아도 문항 점수가 ±0.5 씩 흔들립니다. 실제로 어떤
회차에서는 근거가 컨텍스트에 그대로 있는데도 응답 구성만 달라져 1.0 → 0.5
가 된 적이 있습니다.

이 도구는 LLM 생성·채점을 거치지 않고 검색 단계만 봅니다:
  · 쿼리 분해는 문항당 1회만 수행해 모든 설정이 **동일한 서브쿼리**를 씁니다
  · 이후 벡터 검색 → 병합 → 중복 제거 → 예산 채우기까지 재현
  · 근거 페이지(source_url)가 최종 컨텍스트에 포함되는지 판정

따라서 같은 입력에 대해 결과가 항상 같고, 설정 간 차이만 드러납니다.

실행:
    python tools/ab_retrieval.py --dept strategic \\
        --golden data/eval/golden_set_20260910.json
"""

import argparse
import json
import os
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
LOCATION = os.environ.get("VERTEX_AI_LOCATION", "us-east5")
ANTHROPIC_REGION = os.environ.get("ANTHROPIC_VERTEX_REGION", "global")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
EMBED_MODEL = "text-multilingual-embedding-002"
CLAUDE_MODEL = "claude-sonnet-4-6@default"

# 비교할 설정 — (라벨, oversample, coverage_boost, dedupe_threshold)
# dedupe 1.0 = 비활성화
CONFIGS = [
    ("기준 (현행)", 4, 0.20, 1.0),
    ("oversample 1", 1, 0.20, 1.0),
    ("oversample 2", 2, 0.20, 1.0),
    ("oversample 8", 8, 0.20, 1.0),
    ("boost 0.0", 4, 0.00, 1.0),
    ("boost 0.10", 4, 0.10, 1.0),
    ("dedupe 0.85", 4, 0.20, 0.85),
    ("dedupe 0.85 + boost 0.10", 4, 0.10, 0.85),
]


def main() -> int:
    ap = argparse.ArgumentParser(description="검색 파라미터 A/B")
    ap.add_argument("--dept", default="strategic")
    ap.add_argument("--golden", required=True)
    ap.add_argument("--limit", type=int, default=10, help="서브쿼리당 페이지 수")
    ap.add_argument("--budget", type=int, default=0, help="컨텍스트 예산 (0=evaluate.py 값)")
    args = ap.parse_args()

    from dept_config import load_dept

    collection = load_dept(args.dept)["qdrant_collection"]

    from anthropic import AnthropicVertex
    from google import genai
    from qdrant_client import QdrantClient

    from utils.retrieval import (
        dedupe_documents,
        merge_semantic_results,
        search_queries,
        vector_search_pages,
    )

    embed = genai.Client(project=GCP_PROJECT, location=LOCATION, vertexai=True)
    claude = AnthropicVertex(project_id=GCP_PROJECT, region=ANTHROPIC_REGION)
    qc = QdrantClient(url=QDRANT_URL)

    budget = args.budget or 60000

    def _complete(prompt: str) -> str:
        msg = claude.messages.create(
            model=CLAUDE_MODEL, max_tokens=400, messages=[{"role": "user", "content": prompt}]
        )
        return msg.content[0].text

    def _embed_text(text: str) -> list:
        r = embed.models.embed_content(model=EMBED_MODEL, contents=[text[:2000]])
        return list(r.embeddings[0].values)

    golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    questions = [q for q in golden.get("questions", golden) if q.get("source_url")]
    print(f"  컬렉션: {collection} | 문항 {len(questions)}개 | 예산 {budget}자\n")

    # ── 1. 분해·임베딩·검색은 문항당 1회만 (모든 설정이 동일 입력을 공유) ──
    print("  쿼리 분해 + 벡터 검색 캐싱 중...")
    cache: list = []
    max_over = max(c[1] for c in CONFIGS)
    for i, q in enumerate(questions, 1):
        subs, _ = search_queries(q["question"], _complete)
        # 최대 oversample 로 한 번 검색해두고, 낮은 설정은 상위만 잘라 재현합니다
        per_sub = [
            vector_search_pages(qc, collection, _embed_text(sq), args.limit, oversample=max_over)
            for sq in subs
        ]
        cache.append({"q": q, "subs": subs, "per_sub": per_sub})
        print(f"    [{i}/{len(questions)}] {q['id']}", end="\r", flush=True)
    print(" " * 40, end="\r")

    # ── 2. 설정별 recall 측정 ──────────────────────────────────────────────
    print("\n  설정별 근거 포함률 (recall)\n")
    print(f"  {'설정':<26} {'recall':>8} {'평균순위':>8} {'평균투입':>8} {'평균문서':>8}")
    print("  " + "-" * 62)

    results: list = []
    for label, over, boost, dedup in CONFIGS:
        hits = 0
        ranks: list = []
        used_counts: list = []
        doc_counts: list = []
        misses: list = []

        for item in cache:
            gold = item["q"]["source_url"]
            # oversample 축소 재현: 상위 (limit*over/max_over) 페이지만 사용
            keep = max(1, round(args.limit * over / max_over))
            per_sub = [s[:keep] for s in item["per_sub"]]

            merged = merge_semantic_results(per_sub, boost=boost)
            if dedup < 1.0:
                merged = dedupe_documents(merged, threshold=dedup)
            doc_counts.append(len(merged))

            # 예산 채우기 (evaluate.py 와 동일 규칙)
            total, used = 0, 0
            found = False
            for j, d in enumerate(merged[:25]):
                block = len(d.get("content", "")) + len(d.get("title", "")) + 20
                if j and total + block > budget:
                    break
                total += block
                used += 1
                if d.get("source_url") == gold:
                    found = True
            used_counts.append(used)

            rank = next((k + 1 for k, d in enumerate(merged) if d.get("source_url") == gold), None)
            if rank:
                ranks.append(rank)
            if found:
                hits += 1
            else:
                misses.append((item["q"]["id"], rank))

        recall = hits / len(cache) if cache else 0.0
        avg_rank = sum(ranks) / len(ranks) if ranks else 0
        avg_used = sum(used_counts) / len(used_counts) if used_counts else 0
        avg_docs = sum(doc_counts) / len(doc_counts) if doc_counts else 0
        results.append((label, recall, misses))
        print(f"  {label:<26} {recall:>7.1%} {avg_rank:>8.1f} {avg_used:>8.1f} {avg_docs:>8.1f}")

    # ── 3. 기준 대비 차이 ─────────────────────────────────────────────────
    base_label, base_recall, base_misses = results[0]
    base_miss_ids = {m[0] for m in base_misses}
    print(f"\n  기준({base_label}) 대비\n")
    for label, recall, misses in results[1:]:
        miss_ids = {m[0] for m in misses}
        gained = base_miss_ids - miss_ids
        lost = miss_ids - base_miss_ids
        delta = recall - base_recall
        sign = "+" if delta > 0 else ""
        note = []
        if gained:
            note.append(f"회복 {sorted(gained)}")
        if lost:
            note.append(f"손실 {sorted(lost)}")
        print(f"  {label:<26} {sign}{delta:>6.1%}  {' / '.join(note) or '동일'}")

    if base_misses:
        print(f"\n  기준 설정에서 누락된 문항: {[m[0] for m in base_misses]}")
        for qid, rank in base_misses:
            print(f"    {qid}: 순위 {rank if rank else '검색 실패'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
