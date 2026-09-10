#!/usr/bin/env python3
"""
골든셋 실패 문항 진단기
─────────────────────────────────────────────────────────────────────────────
평가에서 틀린 문항에 대해 "정답이 어디에 있고, 왜 LLM에 닿지 않았는지"를
단계별로 확인합니다.

확인 항목:
  1. 정답 문자열이 Qdrant 청크에 실제로 존재하는가        (데이터 유무)
  2. 그 청크가 벡터 검색 상위에 들어오는가                (검색 품질)
  3. 페이지 전문 조립 시 max_chars 안에 들어오는가        (전달 손실)  ← 핵심
  4. 정답 청크의 페이지 내 위치(문자 오프셋)

실행:
    python tools/diag_golden_miss.py --dept strategic \\
        --golden data/eval/golden_set_20260910.json \\
        --result data/eval/eval_result_20260910_023958.json
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
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
EMBED_MODEL = "text-multilingual-embedding-002"
PAGE_MAX_CHARS = 4000  # server.py `_fetch_full_pages` 기본값과 동일


def _norm(s: str) -> str:
    """공백·대소문자를 무시한 느슨한 비교용 정규화."""
    return "".join(str(s).split()).lower()


def main() -> int:
    ap = argparse.ArgumentParser(description="골든셋 실패 문항 진단")
    ap.add_argument("--dept", default="strategic")
    ap.add_argument("--golden", required=True, help="골든셋 JSON")
    ap.add_argument("--result", default="", help="평가 결과 JSON (없으면 전 문항 검사)")
    ap.add_argument("--top", type=int, default=24, help="벡터 검색 청크 수")
    ap.add_argument("--max-chars", type=int, default=PAGE_MAX_CHARS)
    args = ap.parse_args()

    from dept_config import load_dept

    collection = load_dept(args.dept)["qdrant_collection"]

    from google import genai
    from qdrant_client import QdrantClient

    embed = genai.Client(project=GCP_PROJECT, location=LOCATION, vertexai=True)
    qc = QdrantClient(url=QDRANT_URL)

    golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    questions = {q["id"]: q for q in golden.get("questions", golden)}

    # 실패 문항 선별
    if args.result:
        res = json.loads(Path(args.result).read_text(encoding="utf-8"))
        rows = res.get("results", res if isinstance(res, list) else [])
        targets = [r.get("id") for r in rows if float(r.get("score", 0)) < 0.7]
        targets = [t for t in targets if t in questions]
    else:
        targets = list(questions)

    print(f"  컬렉션: {collection} | 진단 대상 {len(targets)}문항\n")

    for qid in targets:
        q = questions[qid]
        question, answer = q["question"], q["answer"]
        print("=" * 72)
        print(f"  {qid} | {q.get('category', '')} / {q.get('difficulty', '')}")
        print(f"  Q: {question[:66]}")
        print(f"  A(정답): {answer[:66]}")
        print(f"  출처: {q.get('source_url', '(없음)')}")

        needle = _norm(answer)[:40]  # 정답 앞부분으로 탐색

        # ── 1. 정답이 담긴 청크가 존재하는가 (전수 스캔) ────────────────────
        found_chunks: list = []
        offset = None
        while True:
            rows_, offset = qc.scroll(
                collection_name=collection,
                limit=500,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for p in rows_:
                pl = p.payload or {}
                if needle and needle in _norm(pl.get("text", "")):
                    found_chunks.append(
                        {
                            "page_id": pl.get("page_id", ""),
                            "title": pl.get("title", ""),
                            "chunk_index": pl.get("chunk_index", -1),
                            "source_url": pl.get("source_url", ""),
                        }
                    )
            if offset is None:
                break

        if not found_chunks:
            print("  ① 데이터: ❌ 정답 문자열을 담은 청크 없음 (골든셋이 원문과 어긋남)")
            print()
            continue
        print(f"  ① 데이터: ✅ 정답 청크 {len(found_chunks)}개")
        for c in found_chunks[:3]:
            print(f"       page={c['page_id'][:8]} chunk#{c['chunk_index']} {c['title'][:34]}")

        gold_pages = {c["page_id"] for c in found_chunks}

        # ── 2. 벡터 검색 상위에 들어오는가 ──────────────────────────────────
        vec = embed.models.embed_content(model=EMBED_MODEL, contents=[question[:2000]])
        hits = qc.query_points(
            collection_name=collection,
            query=list(vec.embeddings[0].values),
            limit=args.top,
            with_payload=True,
        )
        hit_pages: dict = {}
        gold_rank = None
        for rank, h in enumerate(hits.points, 1):
            pl = h.payload or {}
            pid = pl.get("page_id", "")
            hit_pages.setdefault(pid, rank)
            if pid in gold_pages and gold_rank is None:
                gold_rank = rank
        if gold_rank is None:
            print(f"  ② 검색: ❌ 상위 {args.top}청크 안에 정답 페이지 없음 (임베딩 미스)")
            print()
            continue
        page_rank = list(hit_pages).index(next(p for p in hit_pages if p in gold_pages)) + 1
        print(f"  ② 검색: ✅ 정답 페이지가 청크 {gold_rank}위 / 페이지 {page_rank}위")

        # ── 3. 페이지 전문 조립 시 잘리는가 ─────────────────────────────────
        for pid in gold_pages:
            chunks: list = []
            offset = None
            while True:
                from qdrant_client.models import FieldCondition, Filter, MatchValue

                rows_, offset = qc.scroll(
                    collection_name=collection,
                    scroll_filter=Filter(
                        must=[FieldCondition(key="page_id", match=MatchValue(value=pid))]
                    ),
                    limit=500,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
                chunks.extend(
                    {
                        "i": (p.payload or {}).get("chunk_index", 9999),
                        "t": (p.payload or {}).get("text", ""),
                    }
                    for p in rows_
                )
                if offset is None:
                    break
            chunks.sort(key=lambda c: c["i"])
            full = "\n\n".join(c["t"] for c in chunks)
            pos = _norm(full).find(needle)
            # 정규화 전 원문 기준 대략 위치 추정
            approx = int(pos / max(len(_norm(full)), 1) * len(full)) if pos >= 0 else -1
            cut = args.max_chars
            verdict = "✅ 포함" if 0 <= approx < cut else "❌ 잘림"
            print(
                f"  ③ 전달: {verdict} | 페이지 {len(full)}자, 정답 위치 ~{approx}자, "
                f"전달 한도 {cut}자 (청크 {len(chunks)}개)"
            )
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
