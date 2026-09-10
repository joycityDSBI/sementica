#!/usr/bin/env python3
"""
골든셋 실패 문항 진단기 (의미 기반)
─────────────────────────────────────────────────────────────────────────────
평가에서 틀린 문항이 "왜 답을 못 했는지"를 세 단계로 좁힙니다.

  ① 근거 : 정답을 뒷받침하는 청크가 출처 페이지에 실제로 있는가?
  ② 검색 : 그 페이지가 벡터 검색 상위에 들어오는가?
  ③ 전달 : 근거 청크가 페이지 조립·윈도우를 거쳐 LLM 컨텍스트에 남는가?

※ ①은 반드시 **의미 기반**이어야 합니다. 골든셋 정답은 gen_golden_set 의
  verify_grounded 가 "표현이 달라도 의미가 같으면 pass" 로 채택한 재서술
  문장이라, 원문에 같은 문자열이 존재하지 않습니다. 문자열 매칭으로 검사하면
  모든 문항이 "데이터 없음" 으로 오판됩니다.

③은 server.py 의 _window_around_anchor 를 소스에서 그대로 불러와 적용하므로,
운영 코드가 실제로 남기는 범위를 검사합니다.

실행:
    python tools/diag_golden_miss.py --dept strategic \\
        --golden data/eval/golden_set_20260910.json \\
        --result data/eval/eval_result_20260910_023958.json
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

GROUND_PROMPT = """다음은 한 문서를 청크로 나눈 목록입니다.

{chunks}

질문: {question}
정답: {answer}

위 정답을 뒷받침하는 내용이 담긴 청크의 번호를 모두 찾으세요.
- 표현이 달라도 의미가 같으면 근거로 인정합니다.
- 정답의 일부만 담고 있어도 포함하세요.
- 근거가 전혀 없으면 빈 배열을 반환하세요.

JSON 배열로만 응답: [12, 13]"""


def load_window_fn():
    """운영 코드가 실제로 쓰는 윈도우 함수를 그대로 사용합니다.

    검색 로직이 utils.retrieval 로 통합되어 서버·평가·진단기가 같은 구현을
    참조합니다 (이전에는 server.py 소스에서 함수 정의를 추출해야 했습니다).
    """
    from utils.retrieval import window_around_anchor

    return window_around_anchor


def page_max_chars() -> int:
    """운영 코드의 페이지 전달 한도."""
    from utils.retrieval import PAGE_MAX_CHARS

    return PAGE_MAX_CHARS


def fetch_page_chunks(qc, collection: str, source_url: str) -> list:
    """source_url 의 모든 청크를 chunk_index 순으로 반환."""
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    chunks: list = []
    offset = None
    while True:
        rows, offset = qc.scroll(
            collection_name=collection,
            scroll_filter=Filter(
                must=[FieldCondition(key="source_url", match=MatchValue(value=source_url))]
            ),
            limit=500,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for p in rows:
            pl = p.payload or {}
            chunks.append(
                {
                    "index": pl.get("chunk_index", 9999),
                    "text": pl.get("text", ""),
                    "page_id": pl.get("page_id", ""),
                    "title": pl.get("title", ""),
                }
            )
        if offset is None:
            break
    chunks.sort(key=lambda c: c["index"])
    return chunks


def find_grounding_chunks(claude, question: str, answer: str, chunks: list) -> list:
    """정답을 뒷받침하는 청크 index 를 LLM 으로 판정 (의미 기반)."""
    listing = "\n\n".join(f"[청크 {c['index']}]\n{c['text'][:1500]}" for c in chunks)
    try:
        msg = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=200,
            messages=[
                {
                    "role": "user",
                    "content": GROUND_PROMPT.format(
                        chunks=listing[:120000], question=question, answer=answer
                    ),
                }
            ],
        )
        text = msg.content[0].text.strip()
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if not m:
            return []
        return [int(x) for x in json.loads(m.group()) if isinstance(x, int)]
    except Exception as exc:
        print(f"     ⚠️ 근거 판정 실패: {exc}")
        return []


def main() -> int:
    ap = argparse.ArgumentParser(description="골든셋 실패 문항 진단 (의미 기반)")
    ap.add_argument("--dept", default="strategic")
    ap.add_argument("--golden", required=True)
    ap.add_argument("--result", default="", help="평가 결과 JSON (없으면 전 문항)")
    ap.add_argument("--top", type=int, default=24, help="벡터 검색 청크 수")
    args = ap.parse_args()

    from dept_config import load_dept

    collection = load_dept(args.dept)["qdrant_collection"]

    from anthropic import AnthropicVertex
    from google import genai
    from qdrant_client import QdrantClient

    embed = genai.Client(project=GCP_PROJECT, location=LOCATION, vertexai=True)
    claude = AnthropicVertex(project_id=GCP_PROJECT, region=ANTHROPIC_REGION)
    qc = QdrantClient(url=QDRANT_URL)

    window_fn = load_window_fn()
    max_chars = page_max_chars()
    if window_fn is None:
        print("  ⚠️ _window_around_anchor 를 찾지 못해 ③은 단순 앞자르기로 검사합니다")

    golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    questions = {q["id"]: q for q in golden.get("questions", golden)}

    if args.result:
        res = json.loads(Path(args.result).read_text(encoding="utf-8"))
        rows = res.get("results", res if isinstance(res, list) else [])
        targets = [r.get("id") for r in rows if float(r.get("score", 0)) < 0.7]
        targets = [t for t in targets if t in questions]
    else:
        targets = list(questions)

    print(f"  컬렉션: {collection} | 페이지 한도: {max_chars}자 | 대상 {len(targets)}문항\n")
    verdicts: dict = {}

    for qid in targets:
        q = questions[qid]
        question, answer = q["question"], q["answer"]
        url = q.get("source_url", "")
        print("=" * 74)
        print(f"  {qid} | {q.get('category', '')} / {q.get('difficulty', '')}")
        print(f"  Q: {question[:68]}")
        print(f"  A: {answer[:68]}")

        if not url:
            print("  ⚠️ 골든셋에 source_url 이 없어 진단 불가\n")
            verdicts[qid] = "no_source"
            continue

        chunks = fetch_page_chunks(qc, collection, url)
        if not chunks:
            print(f"  ① 근거: ❌ 출처 페이지의 청크를 찾을 수 없음 ({url[:52]})")
            print("     → 인제스트에서 제외되었거나 source_url 이 달라졌습니다\n")
            verdicts[qid] = "page_missing"
            continue

        full = "\n\n".join(c["text"] for c in chunks)
        print(f"  페이지: 청크 {len(chunks)}개 / {len(full)}자")

        # ── ① 근거 (의미 기반) ──────────────────────────────────────────────
        ground = find_grounding_chunks(claude, question, answer, chunks)
        if not ground:
            print("  ① 근거: ❌ 이 페이지에서 정답 근거를 찾지 못함")
            print("     → 골든셋 정답이 원문을 넘어선 재서술이거나, 다른 페이지가 출처입니다")
            print("     → 문항 교체 대상\n")
            verdicts[qid] = "no_grounding"
            continue
        print(f"  ① 근거: ✅ 청크 {ground} 에 정답 근거 있음")

        # ── ② 검색 ──────────────────────────────────────────────────────────
        vec = embed.models.embed_content(model=EMBED_MODEL, contents=[question[:2000]])
        hits = qc.query_points(
            collection_name=collection,
            query=list(vec.embeddings[0].values),
            limit=args.top,
            with_payload=True,
        )
        page_id = chunks[0]["page_id"]
        chunk_rank = None
        anchor = None
        page_order: list = []
        for rank, h in enumerate(hits.points, 1):
            pl = h.payload or {}
            pid = pl.get("page_id", "")
            if pid not in page_order:
                page_order.append(pid)
            if pid == page_id and chunk_rank is None:
                chunk_rank = rank
                anchor = pl.get("chunk_index", 0)

        if chunk_rank is None:
            print(f"  ② 검색: ❌ 상위 {args.top}청크 안에 출처 페이지 없음")
            print("     → 임베딩 미스. 쿼리 확장·청크 크기 조정이 필요합니다\n")
            verdicts[qid] = "retrieval_miss"
            continue
        prank = page_order.index(page_id) + 1
        hit_ground = anchor in ground
        print(
            f"  ② 검색: ✅ 청크 {chunk_rank}위 / 페이지 {prank}위 "
            f"(앵커 청크 #{anchor}{'  ← 근거 청크' if hit_ground else '  ← 근거 아님'})"
        )

        # ── ③ 전달 ──────────────────────────────────────────────────────────
        piece = window_fn(full, chunks, max_chars, anchor) if window_fn else full[:max_chars]

        missing = [g for g in ground if next((c for c in chunks if c["index"] == g), None) is None]
        kept = [
            g
            for g in ground
            if (t := next((c["text"] for c in chunks if c["index"] == g), "")) and t[:60] in piece
        ]
        lost = [g for g in ground if g not in kept and g not in missing]

        if len(full) <= max_chars:
            print(f"  ③ 전달: ✅ 페이지 전문 전달 ({len(full)}자 ≤ {max_chars}자)")
            verdicts[qid] = "delivered" if prank <= 6 else "rank_low"
        elif lost:
            print(f"  ③ 전달: ❌ 근거 청크 {lost} 가 윈도우 밖 (윈도우 {len(piece)}자)")
            print(f"     → 앵커 #{anchor} 기준으로 잘려 근거를 잃었습니다")
            verdicts[qid] = "window_cut"
        else:
            print(f"  ③ 전달: ✅ 근거 청크 {kept} 모두 윈도우 포함 ({len(piece)}자)")
            verdicts[qid] = "delivered" if prank <= 6 else "rank_low"

        if prank > 6:
            print(f"     ⚠️ 페이지 {prank}위 — 컨텍스트 상위 6건 밖이라 전달되지 않았을 수 있습니다")
        print()

    # ── 요약 ────────────────────────────────────────────────────────────────
    print("=" * 74)
    print("  진단 요약")
    labels = {
        "no_grounding": "정답 근거 없음 — 문항 교체",
        "page_missing": "출처 페이지 미적재",
        "retrieval_miss": "검색 미스 — 임베딩/청킹",
        "window_cut": "윈도우 절단 — 전달 손실",
        "rank_low": "페이지 순위 낮음 — 재랭킹",
        "delivered": "전달됨 — 생성/채점 단계 문제",
        "no_source": "골든셋에 출처 없음",
    }
    for kind, label in labels.items():
        ids = [q for q, v in verdicts.items() if v == kind]
        if ids:
            print(f"    {label:32} {', '.join(ids)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
