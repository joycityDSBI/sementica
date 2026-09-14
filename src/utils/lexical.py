"""
어휘 검색 (BM25) — 벡터가 못 보는 축
─────────────────────────────────────────────────────────────────────────────
임베딩은 "비슷한 뜻"을 찾고, 어휘 검색은 "그 단어가 있는 곳"을 찾습니다.
둘은 다른 것을 놓칩니다. 실측(Q37):

    질문   "워크북 붙여넣기(5단계) 전에 반드시 선행해야 하는 확인 작업"
    근거   「수수료율BEP산출 260831」 — 재무 문서 안에 묻힌 운영 세부사항

    벡터   페이지 20위 (4개 서브쿼리 중 3개는 청크 80위 창에도 못 듦)
           경쟁 문서가 전부 프로세스 문서라 문서 수준에서 질문에 더 가까움
           0.58 vs 0.71 — 파라미터로 옮길 수 있는 거리가 아님
    BM25   페이지 **1위** (25.9 vs 차순위 22.8)
           "워크북" 이 코퍼스에서 희귀하므로 바로 걸림

**주의 — 골든셋 점수를 그대로 믿으면 안 됩니다.** 골든셋 문항은 문서에서
LLM 이 생성하므로 문서의 어휘를 그대로 물려받습니다. 어휘 검색에 유리하게
기울어진 측정이라, dev 44/45 라는 수치는 실제 사용자 질문에 대한 성능이
아닙니다. 그래서 벡터를 **대체**하지 않고 **함께** 씁니다.

한국어 토큰화:
    형태소 분석기 없이 문자 2-gram 을 씁니다. "워크북" → 워크, 크북.
    조사가 붙어도("워크북을") 앞부분 2-gram 이 그대로 걸립니다. 영문·숫자는
    단어 단위로 둡니다.

규모:
    색인을 프로세스 메모리에 올립니다. 실측 730청크 / 어휘 12,725개 수준에서는
    가볍지만, 코퍼스가 수십만 청크로 커지면 Qdrant 의 sparse vector 로 옮겨야
    합니다. LEXICAL_MAX_CHUNKS 를 넘으면 경고합니다.
"""

import math
import os
import re
import threading
import time
from collections import Counter, defaultdict

# BM25 표준 파라미터. 튜닝 대상이 아닙니다 — 이 값들은 넓은 범위에서 잘 동작하고,
# 골든셋에 맞춰 움직이면 그 문항들에 맞추는 것이 됩니다.
BM25_K1: float = float(os.environ.get("BM25_K1", "1.5"))
BM25_B: float = float(os.environ.get("BM25_B", "0.75"))

# 색인 갱신 주기. 인제스트가 코퍼스를 바꾸면 이 시간 뒤에 반영됩니다.
LEXICAL_TTL: float = float(os.environ.get("LEXICAL_TTL", "900"))
# 이 수를 넘으면 메모리 색인이 부적절하다는 뜻입니다 (위 "규모" 참고).
LEXICAL_MAX_CHUNKS: int = int(os.environ.get("LEXICAL_MAX_CHUNKS", "50000"))

_ASCII_WORD = re.compile(r"[A-Za-z0-9_]+")
_HANGUL = re.compile(r"[가-힣]+")


def tokenize(text: str) -> list:
    """한국어는 문자 2-gram, 영문·숫자는 단어 단위.

    >>> tokenize("워크북을 붙여넣기")
    ['워크', '크북', '북을', '붙여', '여넣', '넣기']
    >>> tokenize("BigQuery 적재")
    ['bigquery', '적재']
    >>> tokenize("")
    []

    한 글자 한국어는 그대로 둡니다 (2-gram 을 만들 수 없음):

    >>> tokenize("전 단계")
    ['전', '단계']
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

    >>> b = BM25([["워크", "크북"], ["리포", "포트"], ["워크", "크북", "붙여"]])
    >>> s = b.scores(["워크", "크북"])
    >>> s[1] == 0.0 and s[0] > 0 and s[2] > 0
    True

    질의에 없는 어휘는 무시하고, 겹치는 것이 없으면 전부 0 입니다:

    >>> BM25([["a"]]).scores(["zzz"])
    [0.0]
    """

    def __init__(self, docs: list):
        self.n = len(docs)
        self.freqs = [Counter(d) for d in docs]
        self.lens = [len(d) for d in docs]
        self.avglen = (sum(self.lens) / self.n) if self.n else 0.0
        df: dict = defaultdict(int)
        for f in self.freqs:
            for t in f:
                df[t] += 1
        # 흔한 어휘일수록 0 에 가까워집니다 — 희귀어가 변별력을 갖습니다.
        self.idf = {t: math.log(1 + (self.n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
        # 역색인: 어휘 → [(문서 index, tf), ...]. 질의어가 든 문서만 훑습니다.
        self.postings: dict = defaultdict(list)
        for i, f in enumerate(self.freqs):
            for t, tf in f.items():
                self.postings[t].append((i, tf))

    def scores(self, query_tokens: list) -> list:
        out = [0.0] * self.n
        for t in set(query_tokens):
            idf = self.idf.get(t)
            if not idf:
                continue
            for i, tf in self.postings[t]:
                denom = tf + BM25_K1 * (1 - BM25_B + BM25_B * self.lens[i] / (self.avglen or 1))
                out[i] += idf * tf * (BM25_K1 + 1) / denom
        return out

    def top(self, query_tokens: list, k: int) -> list:
        """상위 k 개 (문서 index, 점수) — 점수 0 은 제외합니다."""
        s = self.scores(query_tokens)
        idx = [i for i, v in enumerate(s) if v > 0]
        idx.sort(key=lambda i: -s[i])
        return [(i, s[i]) for i in idx[:k]]


class LexicalIndex:
    """Qdrant 코퍼스에 대한 BM25 색인. TTL 로 갱신합니다.

    호출부가 색인을 직접 만들지 않도록 감쌉니다 — 두 벌이 생기면 서로 다른
    코퍼스를 보게 되고, 그러면 평가와 서비스가 다른 것을 재게 됩니다.
    """

    def __init__(self, ttl: float = LEXICAL_TTL):
        self.ttl = ttl
        self._built_at = 0.0
        self._lock = threading.Lock()
        self._bm25: BM25 | None = None
        self._meta: list = []  # [(page_id, source_url, chunk_index), ...]

    def _load(self, qc, collection: str) -> None:
        meta: list = []
        docs: list = []
        offset = None
        while True:
            rows, offset = qc.scroll(
                collection_name=collection,
                limit=512,
                offset=offset,
                with_payload=["page_id", "source_url", "chunk_index", "text"],
            )
            for r in rows:
                p = r.payload or {}
                meta.append(
                    (
                        str(p.get("page_id", "")),
                        str(p.get("source_url", "")),
                        p.get("chunk_index", 0),
                    )
                )
                docs.append(tokenize(str(p.get("text", ""))))
            if offset is None:
                break
        if len(docs) > LEXICAL_MAX_CHUNKS:
            print(
                f"  ⚠️  어휘 색인 {len(docs)}청크 — 메모리 색인의 적정 범위를 넘었습니다. "
                "Qdrant sparse vector 로 옮기세요 (utils/lexical 문서 참고)."
            )
        self._meta = meta
        self._bm25 = BM25(docs)
        self._built_at = time.monotonic()

    def ensure(self, qc, collection: str) -> None:
        if self._bm25 is not None and time.monotonic() - self._built_at < self.ttl:
            return
        with self._lock:
            if self._bm25 is not None and time.monotonic() - self._built_at < self.ttl:
                return
            self._load(qc, collection)

    def search(self, qc, collection: str, query: str, k: int) -> list:
        """[(page_id, source_url, chunk_index, score), ...] 점수 내림차순."""
        try:
            self.ensure(qc, collection)
        except Exception as exc:
            print(f"  ⚠️  어휘 색인 생성 실패 — 벡터 검색만 사용합니다: {exc}")
            return []
        if not self._bm25:
            return []
        out = []
        for i, s in self._bm25.top(tokenize(query), k):
            pid, url, ci = self._meta[i]
            out.append((pid, url, ci, s))
        return out


# 프로세스 공용 색인 — 호출부마다 만들면 메모리와 로딩이 배로 듭니다.
_INDEX = LexicalIndex()


def lexical_search(qc, collection: str, query: str, k: int) -> list:
    """공용 색인으로 어휘 검색. [(page_id, source_url, chunk_index, score), ...]"""
    return _INDEX.search(qc, collection, query, k)


def reset_index() -> None:
    """색인을 버립니다 — 인제스트 직후나 테스트에서 씁니다."""
    global _INDEX
    _INDEX = LexicalIndex()
