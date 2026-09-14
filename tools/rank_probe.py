#!/usr/bin/env python3
"""
"이 문서가 왜 안 잡히는가" — 순위를 직접 재서 대처를 정합니다
─────────────────────────────────────────────────────────────────────────────
검색이 문서를 못 찾았다는 사실만으로는 무엇을 고쳐야 할지 알 수 없습니다.
같은 "못 찾음"이라도 대처가 정반대입니다:

    순위 85위   상위 80개 안에 조금 못 든 것 → 오버샘플을 넓히면 됩니다
    순위 400위  의미적으로 멀리 있는 것       → 넓혀도 소용없습니다.
                                              어휘 검색(BM25) 같은 다른 축이 필요합니다

그래서 **깊게 검색해서 실제 순위를 재고**, 그 위에 무엇이 있는지 봅니다.
평가 파이프라인과 같은 분해·임베딩을 씁니다 (utils.retrieval.search_queries) —
여기서 다른 경로를 쓰면 재는 대상이 실제 검색이 아니게 됩니다.

실행:
    python tools/rank_probe.py --dept strategic \\
        --golden data/eval/golden_v2_dev.json --id Q37
    python tools/rank_probe.py --dept strategic \\
        --question "..." --url "https://notion.so/..."
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

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
GCP_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
LOCATION = os.environ.get("VERTEX_AI_LOCATION", "us-east5")
ANTHROPIC_REGION = os.environ.get("ANTHROPIC_VERTEX_REGION", "global")
EMBED_MODEL = "text-multilingual-embedding-002"


def main() -> int:
    ap = argparse.ArgumentParser(description="문서 검색 순위 측정")
    ap.add_argument("--dept", default="strategic")
    ap.add_argument("--golden", default="", help="골든셋 JSON")
    ap.add_argument("--id", default="", help="문항 ID (--golden 과 함께)")
    ap.add_argument("--question", default="", help="직접 질문 입력")
    ap.add_argument("--url", default="", help="찾아야 할 문서의 source_url")
    ap.add_argument("--depth", type=int, default=500, help="몇 위까지 볼지")
    ap.add_argument("--show", type=int, default=8, help="상위 몇 개를 보여줄지")
    args = ap.parse_args()

    from google import genai
    from qdrant_client import QdrantClient

    from dept_config import load_dept
    from utils.retrieval import CHUNK_OVERSAMPLE, DEFAULT_PAGE_LIMIT, search_queries

    question, target_url = args.question, args.url
    if args.golden:
        data = json.loads(Path(args.golden).read_text(encoding="utf-8"))
        qs = data.get("questions", data)
        hit = next((q for q in qs if str(q.get("id")) == args.id), None)
        if not hit:
            print(f"❌ 문항 {args.id} 을 찾을 수 없습니다")
            return 1
        question = hit["question"]
        target_url = hit.get("source_url", "")
        print(f"  {args.id} | {hit.get('category', '')} / {hit.get('difficulty', '')}")
        print(f"  Q: {question}")
        print(f"  A: {hit.get('answer', '')[:90]}")
    if not question or not target_url:
        ap.error("--golden+--id 또는 --question+--url 이 필요합니다")

    cfg = load_dept(args.dept)
    collection = cfg["qdrant_collection"]
    qc = QdrantClient(url=QDRANT_URL)
    embed = genai.Client(project=GCP_PROJECT, location=LOCATION, vertexai=True)

    def _embed(text: str) -> list:
        r = embed.models.embed_content(model=EMBED_MODEL, contents=[text[:2000]])
        return list(r.embeddings[0].values)

    # 분해는 평가·서비스와 같은 경로로 (모델도 같은 것을 씁니다)
    from anthropic import AnthropicVertex

    from utils.llm import create_message
    from utils.retrieval import DECOMPOSE_MODEL_VERTEX

    client = AnthropicVertex(project_id=GCP_PROJECT, region=ANTHROPIC_REGION)

    def complete_fn(prompt: str) -> str:
        msg = create_message(
            client,
            model=DECOMPOSE_MODEL_VERTEX,
            max_tokens=300,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text

    subs, decomposed = search_queries(question, complete_fn)
    print(f"\n  서브쿼리 {len(subs)}개 (분해={decomposed}):")
    for s in subs:
        print(f"    · {s}")

    # 대상 문서의 청크 목록
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    pts, _ = qc.scroll(
        collection_name=collection,
        scroll_filter=Filter(
            must=[FieldCondition(key="source_url", match=MatchValue(value=target_url))]
        ),
        limit=500,
        with_payload=["chunk_index", "title"],
    )
    if not pts:
        print("\n  ❌ 이 문서의 청크가 Qdrant 에 없습니다 — 검색 문제가 아니라 벡터 유실입니다")
        print(f"     {target_url}")
        return 1
    title = (pts[0].payload or {}).get("title", "")
    print(f"\n  대상 문서: {title[:60]}")
    print(f"    청크 {len(pts)}개 / {target_url[-52:]}")

    window = max(DEFAULT_PAGE_LIMIT * CHUNK_OVERSAMPLE, DEFAULT_PAGE_LIMIT)
    print(f"\n  현재 운영값: 페이지 {DEFAULT_PAGE_LIMIT} × 오버샘플 {CHUNK_OVERSAMPLE}")
    print(f"    → 서브쿼리당 청크 {window}개까지 봅니다. 그 밖이면 못 찾습니다.\n")

    best_overall = None
    for sq in subs:
        hits = qc.query_points(
            collection_name=collection,
            query=_embed(sq),
            limit=args.depth,
            with_payload=True,
        ).points
        rank = None
        for i, h in enumerate(hits, 1):
            if (h.payload or {}).get("source_url") == target_url:
                rank = (i, h.score, (h.payload or {}).get("chunk_index", "?"))
                break

        print(f"  ■ {sq[:62]}")
        if rank:
            i, score, ci = rank
            mark = "✅ 창 안" if i <= window else "❌ 창 밖"
            print(f"      최고 순위 {i}위 (청크 {ci}, 점수 {score:.4f})  {mark}")
            if best_overall is None or i < best_overall[0]:
                best_overall = (i, sq, score)
        else:
            print(f"      {args.depth}위 안에 없음")
        for j, h in enumerate(hits[: args.show], 1):
            p = h.payload or {}
            same = "←대상" if p.get("source_url") == target_url else "     "
            print(f"        {j}. {h.score:.4f} {same} {str(p.get('title', ''))[:46]}")
        print()

    # ── 결론 ────────────────────────────────────────────────────────────────
    print("  " + "─" * 66)
    if best_overall is None:
        print(f"  결론: 어느 서브쿼리로도 {args.depth}위 안에 들지 못합니다.")
        print("        오버샘플을 늘려도 해결되지 않습니다. 질문의 표현과 문서의 표현이")
        print("        의미 공간에서 멀다는 뜻이라, 어휘 검색(BM25) 같은 다른 축이나")
        print("        청크 분할 방식을 바꾸는 쪽을 봐야 합니다.")
        return 0

    best_rank, best_q, best_score = best_overall
    print(f"  결론: 가장 잘 나온 순위 {best_rank}위 (점수 {best_score:.4f})")
    print(f"        서브쿼리: {best_q[:60]}")
    if best_rank <= window:
        print(f"        현재 창({window}) 안입니다 — 검색은 되는데 다른 단계에서 잃었습니다.")
        print("        diag_golden_miss 의 ③ 전달 단계를 확인하세요.")
    else:
        need = -(-best_rank // DEFAULT_PAGE_LIMIT)  # 올림
        print(f"        현재 창({window}) 밖입니다.")
        print(f"        오버샘플을 {need} 이상으로 올리면 들어옵니다 (현재 {CHUNK_OVERSAMPLE}).")
        if need > CHUNK_OVERSAMPLE * 4:
            print("        ⚠️  다만 그만큼 올리는 것은 이 문항 하나를 위한 과적합입니다.")
            print("           다른 문항으로도 같은 값이 필요한지 먼저 확인하세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
