#!/usr/bin/env python3
"""
어휘 검색(BM25)을 넣으면 실제로 찾아지는가 — 짓기 전에 재는 실험
─────────────────────────────────────────────────────────────────────────────
Q37 실측에서 벡터 검색의 한계가 드러났습니다:

    대상 문서 「수수료율BEP산출 260831」  최고점 0.5908 (페이지 20위)
    경쟁 문서 「리포트자동화」「데이터소스검증」「로그설계」  0.61~0.71

질문은 "워크북 붙여넣기 전 선행 확인 작업" 인데, 대상 문서는 수수료율 계산
문서이고 그 내용은 안에 묻힌 운영 세부사항입니다. 반면 경쟁 문서들은 전부
프로세스 문서라 문서 수준에서 질문과 더 가깝습니다. **의미 공간에서 진짜로
멀리 있는 것**이라 오버샘플·limit 같은 파라미터로는 옮길 수 없습니다.

하지만 "워크북" 이라는 **단어 자체**는 그 문서에 있습니다. 어휘 검색은 이런
것을 바로 잡습니다 — 벡터가 못 보는 축입니다.

이 도구는 **파이프라인을 바꾸기 전에** 그 가정을 검증합니다. BM25 를 코퍼스
전체에 적용해 대상 문서가 몇 위에 오는지 보고, 벡터 순위와 나란히 놓습니다.
여기서 안 잡히면 BM25 를 넣어도 소용없으니 짓지 않으면 됩니다.

한국어 토큰화:
    형태소 분석기 없이 **문자 2-gram** 을 씁니다. "워크북" → 워크, 크북.
    조사가 붙어도("워크북을") 앞부분 2-gram 이 그대로 걸리므로 한국어에서
    실용적입니다. 영문·숫자는 단어 단위로 둡니다.

실행:
    python tools/lexical_probe.py --dept strategic \\
        --golden data/eval/golden_v2_dev.json --id Q37
    python tools/lexical_probe.py --dept strategic \\
        --golden data/eval/golden_v2_dev.json --all      # 전 문항 일괄
"""

import argparse
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
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

# BM25 기본 파라미터 — 널리 쓰이는 값입니다.
BM25_K1 = 1.5
BM25_B = 0.75

_ASCII_WORD = re.compile(r"[A-Za-z0-9_]+")
_HANGUL = re.compile(r"[가-힣]+")


def tokenize(text: str) -> list:
    """한국어는 문자 2-gram, 영문·숫자는 단어 단위.

    형태소 분석기 없이 조사를 견디는 실용적인 방법입니다.

    >>> tokenize("워크북을 붙여넣기")
    ['워크', '크북', '북을', '붙여', '여넣', '넣기']
    >>> tokenize("BigQuery 적재")
    ['bigquery', '적재']
    >>> tokenize("")
    []
    """
    low = (text or "").lower()
    out: list = [m.group() for m in _ASCII_WORD.finditer(low)]
    for m in _HANGUL.finditer(low):
        w = m.group()
        if len(w) == 1:
            out.append(w)
        else:
            out.extend(w[i : i + 2] for i in range(len(w) - 1))
    return out


class BM25:
    """문서 집합에 대한 BM25 점수기.

    >>> docs = [["워크", "크북"], ["리포", "포트"], ["워크", "크북", "붙여"]]
    >>> b = BM25(docs)
    >>> s = b.scores(["워크", "크북"])
    >>> s[1] == 0.0 and s[0] > 0 and s[2] > 0
    True
    """

    def __init__(self, docs: list):
        self.docs = docs
        self.n = len(docs)
        self.freqs = [Counter(d) for d in docs]
        self.lens = [len(d) for d in docs]
        self.avglen = (sum(self.lens) / self.n) if self.n else 0.0
        df: dict = defaultdict(int)
        for f in self.freqs:
            for t in f:
                df[t] += 1
        # BM25 의 표준 idf — 흔한 단어일수록 0 에 가까워집니다.
        self.idf = {t: math.log(1 + (self.n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def scores(self, query_tokens: list) -> list:
        out = [0.0] * self.n
        qt = Counter(query_tokens)
        for t in qt:
            idf = self.idf.get(t)
            if not idf:
                continue
            for i, f in enumerate(self.freqs):
                tf = f.get(t, 0)
                if not tf:
                    continue
                denom = tf + BM25_K1 * (1 - BM25_B + BM25_B * self.lens[i] / (self.avglen or 1))
                out[i] += idf * tf * (BM25_K1 + 1) / denom
        return out


def load_chunks(qc, collection: str) -> list:
    """코퍼스의 모든 청크를 가져옵니다. [(source_url, title, chunk_index, text), ...]"""
    out: list = []
    offset = None
    while True:
        rows, offset = qc.scroll(
            collection_name=collection,
            limit=512,
            offset=offset,
            with_payload=["source_url", "title", "chunk_index", "text"],
        )
        for r in rows:
            p = r.payload or {}
            out.append(
                (
                    str(p.get("source_url", "")),
                    str(p.get("title", "")),
                    p.get("chunk_index", 0),
                    str(p.get("text", "")),
                )
            )
        if offset is None:
            break
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="어휘 검색(BM25) 효과 측정")
    ap.add_argument("--dept", default="strategic")
    ap.add_argument("--golden", required=True)
    ap.add_argument("--id", default="", help="문항 ID")
    ap.add_argument("--all", action="store_true", help="전 문항 일괄 측정")
    ap.add_argument("--limit", type=int, default=10, help="페이지 limit (운영값)")
    ap.add_argument("--show", type=int, default=8)
    args = ap.parse_args()

    from qdrant_client import QdrantClient

    from dept_config import load_dept

    cfg = load_dept(args.dept)
    qc = QdrantClient(url=QDRANT_URL)

    print(f"  코퍼스 로드 중… ({cfg['qdrant_collection']})")
    chunks = load_chunks(qc, cfg["qdrant_collection"])
    if not chunks:
        print("  ❌ 청크가 없습니다")
        return 1
    pages = len({c[0] for c in chunks})
    print(f"  청크 {len(chunks)}개 / 페이지 {pages}개")

    docs = [tokenize(c[3]) for c in chunks]
    bm25 = BM25(docs)
    print(f"  BM25 색인 완료 (어휘 {len(bm25.idf)}개)\n")

    data = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    qs = data.get("questions", data)
    if args.id:
        qs = [q for q in qs if str(q.get("id")) == args.id]
        if not qs:
            print(f"❌ 문항 {args.id} 없음")
            return 1
    elif not args.all:
        ap.error("--id 또는 --all 이 필요합니다")

    ranks: list = []
    for qitem in qs:
        question = qitem["question"]
        target = qitem.get("source_url", "")
        scores = bm25.scores(tokenize(question))

        # 페이지 단위로 최고점 집계 — vector_search_pages 와 같은 방식
        page_best: dict = {}
        for (url, title, _ci, _t), s in zip(chunks, scores, strict=False):
            if s > 0 and (url not in page_best or s > page_best[url][0]):
                page_best[url] = (s, title)
        ordered = sorted(page_best.items(), key=lambda kv: -kv[1][0])
        rank = next((i for i, (u, _v) in enumerate(ordered, 1) if u == target), None)
        ranks.append((str(qitem.get("id")), rank))

        if args.id or (rank is None or rank > args.limit):
            print(f"  ■ {qitem.get('id')} | {question[:58]}")
            print(f"    대상: {qitem.get('source_url', '')[-46:]}")
            if rank:
                mark = "✅ 통과" if rank <= args.limit else "❌ 잘림"
                print(f"    BM25 페이지 순위 {rank}위 / 걸린 페이지 {len(ordered)}개  {mark}")
            else:
                print("    BM25 로도 걸리지 않음 (공통 어휘 없음)")
            if args.id:
                for j, (u, (s, ttl)) in enumerate(ordered[: args.show], 1):
                    same = "←대상" if u == target else "     "
                    cut = " " if j <= args.limit else "✂"
                    print(f"      {cut}{j}. {s:7.3f} {same} {ttl[:46]}")
                # 어느 단어가 점수를 만들었는지
                qtok = set(tokenize(question))
                tgt = [i for i, c in enumerate(chunks) if c[0] == target]
                contrib: dict = defaultdict(float)
                for i in tgt:
                    for t in qtok & set(docs[i]):
                        contrib[t] += bm25.idf.get(t, 0)
                top = sorted(contrib.items(), key=lambda kv: -kv[1])[:10]
                if top:
                    print("    대상 문서와 겹치는 희귀 어휘:")
                    print("      " + ", ".join(f"{t}({v:.1f})" for t, v in top))
            print()

    if args.all:
        hit = sum(1 for _i, r in ranks if r and r <= args.limit)
        miss = [i for i, r in ranks if not r or r > args.limit]
        print("  " + "─" * 66)
        print(f"  BM25 단독으로 상위 {args.limit} 안에 근거 문서가 든 문항: {hit}/{len(ranks)}")
        if miss:
            print(f"  못 든 문항: {', '.join(miss)}")
        print()
        print("  ※ 이 수치는 BM25 **단독** 성능입니다. 실제로는 벡터와 함께 쓰므로,")
        print("    벡터가 이미 찾는 문항까지 BM25 가 찾을 필요는 없습니다. 중요한 것은")
        print("    **벡터가 놓친 문항을 BM25 가 잡는가** 입니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
