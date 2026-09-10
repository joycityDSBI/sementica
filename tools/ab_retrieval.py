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
  · 임베딩도 문항당 1회만 계산해 재사용합니다
  · 벡터 검색은 oversample 값마다 실제로 다시 수행합니다 (아래 주의)
  · 이후 병합 → 중복 제거 → 예산 채우기까지 재현
  · 근거 페이지(source_url)가 최종 컨텍스트에 포함되는지 판정

따라서 같은 입력에 대해 결과가 항상 같고, 설정 간 차이만 드러납니다.

⚠️ **oversample 은 결과를 잘라서 흉내낼 수 없습니다.**
   vector_search_pages 에서 limit 은 *페이지* 수이고 oversample 은 후보
   *청크* 풀만 넓힙니다. 예전 구현은 캐시된 페이지 목록을 상위 N개만 남기는
   식으로 재현하려 했는데, 그건 "페이지를 몇 개 넘길까"를 바꾼 것이라
   oversample 을 전혀 측정하지 못했습니다. 그래서 지금은 oversample 값마다
   Qdrant 검색을 다시 돌립니다 (임베딩은 재사용하므로 비용은 낮습니다).

⚠️ **예산 계산은 evaluate.py 보다 낙관적입니다.**
   evaluate.py 는 그래프·타임라인 텍스트를 먼저 빼고 남은 예산을 문서에
   씁니다. 여기서는 문서 채널만 보므로, 타임라인이 긴 문항에서는 실제보다
   많이 들어가는 것으로 나옵니다. 설정 간 *비교*에는 영향이 없습니다.

⚠️ **관계 카테고리는 이 지표로 판단하지 마세요.**
   관계 문항의 근거는 그래프 트리플이고 source_url 은 벡터 문서를 가리킵니다.
   실제로 관계 10문항 중 8건이 벡터 recall 실패로 잡히지만 평가에서는 모두
   1.0 입니다 — 그래프가 답을 내기 때문입니다. 그래서 카테고리별로 나눠
   출력하며, 비교는 벡터 의존 카테고리(담당자·정책/규정·문서위치·복합)로
   판단하세요.

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

# 비교할 설정 — (라벨, oversample, coverage_boost, dedupe_threshold)
# dedupe 1.0 = 비활성화
#
# oversample 은 **후보 청크 풀**만 넓힙니다. 반환 페이지 수는 --limit 이
# 정하므로, 값을 키워도 추가 비용은 Qdrant top-k 하나뿐입니다. 어디서
# 포화되는지 보려고 12·16 까지 넣었습니다.
CONFIGS = [
    ("기준 (현행)", 4, 0.20, 1.0),
    ("oversample 1", 1, 0.20, 1.0),
    ("oversample 2", 2, 0.20, 1.0),
    ("oversample 8", 8, 0.20, 1.0),
    ("oversample 12", 12, 0.20, 1.0),
    ("oversample 16", 16, 0.20, 1.0),
    ("boost 0.0", 4, 0.00, 1.0),
    ("boost 0.10", 4, 0.10, 1.0),
    ("dedupe 0.85", 4, 0.20, 0.85),
    ("dedupe 0.85 + boost 0.10", 4, 0.10, 0.85),
]


def main() -> int:
    ap = argparse.ArgumentParser(description="검색 파라미터 A/B")
    ap.add_argument("--dept", default="strategic")
    ap.add_argument("--golden", required=True)
    ap.add_argument(
        "--limit",
        type=int,
        default=0,
        help="서브쿼리당 페이지 수 (0 이면 운영값 utils.retrieval.DEFAULT_PAGE_LIMIT)",
    )
    ap.add_argument(
        "--budget",
        type=int,
        default=0,
        help="컨텍스트 예산 문자 수 (0 이면 evaluate.CONTEXT_MAX_CHARS 기본값 60000)",
    )
    args = ap.parse_args()

    from dept_config import load_dept

    collection = load_dept(args.dept)["qdrant_collection"]

    from anthropic import AnthropicVertex
    from google import genai
    from qdrant_client import QdrantClient

    from utils.retrieval import (
        DECOMPOSE_MODEL_VERTEX,
        DEFAULT_PAGE_LIMIT,
        dedupe_documents,
        merge_semantic_results,
        search_queries,
        vector_search_pages,
    )

    embed = genai.Client(project=GCP_PROJECT, location=LOCATION, vertexai=True)
    claude = AnthropicVertex(project_id=GCP_PROJECT, region=ANTHROPIC_REGION)
    qc = QdrantClient(url=QDRANT_URL)

    # 페이지 수·분해 모델은 운영값을 그대로 씁니다. 여기서 다른 값을 쓰면
    # 운영과 다른 서브쿼리·다른 문서 집합을 튜닝하게 됩니다.
    page_limit = args.limit or DEFAULT_PAGE_LIMIT
    budget = args.budget or 60000

    def _complete(prompt: str) -> str:
        msg = claude.messages.create(
            model=DECOMPOSE_MODEL_VERTEX,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text

    def _embed_text(text: str) -> list:
        r = embed.models.embed_content(model=EMBED_MODEL, contents=[text[:2000]])
        return list(r.embeddings[0].values)

    golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    questions = [q for q in golden.get("questions", golden) if q.get("source_url")]
    print(
        f"  컬렉션: {collection} | 문항 {len(questions)}개 | "
        f"페이지/서브쿼리 {page_limit} | 예산 {budget}자\n"
    )

    # ── 1. 분해·임베딩은 문항당 1회만 (모든 설정이 동일 입력을 공유) ──────
    print("  쿼리 분해 + 임베딩 중...")
    cache: list = []
    for i, q in enumerate(questions, 1):
        subs, _ = search_queries(q["question"], _complete)
        vecs = [_embed_text(sq) for sq in subs]
        cache.append({"q": q, "subs": subs, "vecs": vecs, "by_over": {}})
        print(f"    [{i}/{len(questions)}] {q['id']}", end="\r", flush=True)
    print(" " * 40, end="\r")

    # ── 2. oversample 값마다 실제 벡터 검색 (같은 임베딩 재사용) ──────────
    overs = sorted({c[1] for c in CONFIGS})
    for over in overs:
        print(f"  벡터 검색 (oversample {over})...", end="\r", flush=True)
        for item in cache:
            item["by_over"][over] = [
                vector_search_pages(qc, collection, v, page_limit, oversample=over)
                for v in item["vecs"]
            ]
    print(" " * 48, end="\r")

    # ── 3. 설정별 recall 측정 ──────────────────────────────────────────────
    # 관계 카테고리는 그래프가 답하므로 벡터 recall 로 판단할 수 없습니다.
    VECTOR_CATS = ("담당자", "정책/규정", "문서위치", "복합")
    MAX_CONTEXT_DOCS = 25  # evaluate.py 와 동일
    n_vec = sum(1 for it in cache if it["q"].get("category") in VECTOR_CATS)
    n_rel = len(cache) - n_vec
    print(f"\n  설정별 근거 포함률 — 벡터 의존 {n_vec}문항 / 관계 {n_rel}문항\n")
    print(f"  {'설정':<26} {'벡터recall':>10} {'전체':>7} {'평균순위':>12} {'평균투입':>8}")
    print("  " + "-" * 68)

    results: list = []
    for label, over, boost, dedup in CONFIGS:
        hits = 0
        vec_hits = 0
        ranks: list = []
        used_counts: list = []
        misses: list = []

        for item in cache:
            gold = item["q"]["source_url"]
            is_vec = item["q"].get("category") in VECTOR_CATS
            per_sub = item["by_over"][over]

            merged = merge_semantic_results(per_sub, boost=boost)
            if dedup < 1.0:
                merged = dedupe_documents(merged, threshold=dedup)

            # 예산 채우기 — evaluate.py 의 문서 채널만 재현 (docstring 참고)
            total, used = 0, 0
            found = False
            for j, d in enumerate(merged[:MAX_CONTEXT_DOCS]):
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
                if is_vec:
                    vec_hits += 1
            elif is_vec:
                # 관계 문항의 벡터 누락은 정상이므로 손실 목록에서 제외
                misses.append((item["q"]["id"], rank))

        recall = hits / len(cache) if cache else 0.0
        vec_recall = vec_hits / n_vec if n_vec else 0.0
        avg_rank = sum(ranks) / len(ranks) if ranks else 0
        avg_used = sum(used_counts) / len(used_counts) if used_counts else 0
        results.append((label, vec_recall, misses))
        # 평균순위 옆의 (n) 은 근거를 찾은 문항 수입니다. n 이 작을수록 평균이
        # 좋아 보이므로 (못 찾은 문항이 평균에서 빠지므로) 반드시 같이 봐야 합니다.
        rank_cell = f"{avg_rank:.1f} ({len(ranks)})"
        print(f"  {label:<26} {vec_recall:>9.1%} {recall:>7.1%} {rank_cell:>12} {avg_used:>8.1f}")

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
        print(f"\n  기준 설정에서 벡터 근거가 누락된 문항 ({len(base_misses)}건)")
        for qid, rank in base_misses:
            print(f"    {qid}: 순위 {rank if rank else '검색 실패 — 임베딩·청킹 문제'}")
    else:
        print("\n  기준 설정에서 벡터 의존 문항은 모두 근거 포함")
    print(
        f"\n  ※ 관계 {n_rel}문항은 그래프가 답하므로 이 지표에서 제외했습니다."
        "\n    (source_url 은 벡터 문서를 가리켜 그래프 근거를 측정하지 못함)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
