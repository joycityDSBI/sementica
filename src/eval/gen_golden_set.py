#!/usr/bin/env python3
"""
골든셋 자동 생성 스크립트
실제 Qdrant + FalkorDB 데이터에서 평가 질문을 생성합니다.

채택 기준 (중요) — 두 관문을 모두 통과해야 합니다:
    ① verify_grounded   정답이 소스 원문으로 뒷받침되는가 (환각 필터)
    ② verify_answerable 질문이 답을 하나로 특정하는가 (모호성 필터)

    ②가 필요한 이유: 근거가 있어도 질문이 모호하면 평가가 검색 품질이 아니라
    문항 품질을 재게 됩니다. 실제로 "쿼리에서 GROUP BY 항목은?"(문서에 쿼리
    3개), "제공하는 곳은?"인데 정답에 주기까지 담긴 문항이 반복해서 부분점수를
    받았습니다.

    검색 파이프라인 통과 여부는 채택 기준이 아닙니다 — 그러면 골든셋이 "이미
    답할 수 있는 질문"만 남아 점수가 100%에 수렴하고 약점이 드러나지 않습니다.
    --baseline 으로 참고 정보(baseline_pass)로만 기록합니다.

실행:
    python src/eval/gen_golden_set.py --dept strategic
    python src/eval/gen_golden_set.py --dept strategic --count 30 --baseline
    python src/eval/gen_golden_set.py --dept strategic --out data/eval/golden_set.json

결과:
    data/eval/golden_set_YYYYMMDD.json  ← evaluate.py가 --golden 옵션으로 로드
"""

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

# ─── 경로 설정 ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

_env = ROOT / ".env"
if _env.exists():
    for raw_line in _env.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

GCP_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
LOCATION = os.environ.get("VERTEX_AI_LOCATION", "us-east5")  # 임베딩 리전
# Claude 리전은 임베딩과 별개 — ingest.py와 동일한 환경변수를 사용
ANTHROPIC_REGION = os.environ.get("ANTHROPIC_VERTEX_REGION", "global")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
FALKORDB_HOST = os.environ.get("FALKORDB_HOST", "localhost")
FALKORDB_PORT = int(os.environ.get("FALKORDB_PORT", "6379"))
CLAUDE_MODEL = "claude-sonnet-4-6@default"

# 카테고리 배분 비율 (합 20 기준) — 실제 목표는 --count 에 맞춰 스케일됩니다.
CATEGORY_RATIO = {
    "담당자": 5,
    "정책/규정": 4,
    "관계": 5,
    "문서위치": 3,
    "복합": 3,
}
DIFFICULTY_DIST = {"easy": 0.3, "medium": 0.5, "hard": 0.2}


def _scale_targets(ratio: dict, total: int) -> dict:
    """카테고리 비율을 유지하며 목표 문항 수를 total 로 맞춥니다.

    이전에는 CATEGORY_TARGETS 가 고정(합 20)이라 --count 40 을 줘도 20문항만
    생성됐습니다. --count 는 안내 문구에만 쓰이고 선별 로직은 고정값을 봤습니다.

    >>> _scale_targets({"a": 5, "b": 4, "c": 5, "d": 3, "e": 3}, 40)
    {'a': 10, 'b': 8, 'c': 10, 'd': 6, 'e': 6}
    >>> sum(_scale_targets({"a": 5, "b": 4, "c": 5, "d": 3, "e": 3}, 30).values())
    30
    """
    base = sum(ratio.values())
    if total <= 0 or base <= 0:
        return dict(ratio)
    scaled = {c: max(1, round(n * total / base)) for c, n in ratio.items()}

    # 반올림 오차 보정 — 큰 카테고리부터 ±1 조정해 합계를 total 에 맞춥니다.
    order = sorted(scaled, key=lambda c: (-scaled[c], c))
    i = 0
    while sum(scaled.values()) != total and i < 1000:
        c = order[i % len(order)]
        if sum(scaled.values()) < total:
            scaled[c] += 1
        elif scaled[c] > 1:
            scaled[c] -= 1
        i += 1
    return scaled


# ─── 인수 파싱 ────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--dept", default="", help="본부 키 (config/departments.yaml)")
parser.add_argument("--count", type=int, default=20, help="목표 문항 수 (기본 20)")
parser.add_argument(
    "--sample-pages",
    type=int,
    default=0,
    help="Qdrant 샘플 페이지 수 (0=자동, count x 3 이상). 후보 부족 시 늘리세요",
)
parser.add_argument(
    "--sample-rels",
    type=int,
    default=0,
    help="FalkorDB 샘플 관계 수 (0=자동, count x 4 이상)",
)
parser.add_argument(
    "--out", default="", help="출력 파일 경로 (기본: data/eval/golden_set_YYYYMMDD.json)"
)
parser.add_argument("--seed", type=int, default=42, help="난수 시드")
parser.add_argument(
    "--baseline",
    action="store_true",
    help="각 문항에 대해 현재 검색 파이프라인 통과 여부를 기록 (채택 기준이 아닌 참고 정보). "
    "LLM 호출이 문항당 2회 추가되어 느려집니다.",
)
args = parser.parse_args()

random.seed(args.seed)

# --count 를 카테고리별 목표로 환산 (이 값이 실제 선별 기준)
CATEGORY_TARGETS = _scale_targets(CATEGORY_RATIO, args.count)
# 샘플 페이지·관계 수 — 문항이 늘면 후보도 그만큼 필요합니다.
SAMPLE_PAGES = args.sample_pages or max(60, args.count * 3)
SAMPLE_RELS = args.sample_rels or max(80, args.count * 4)

COLLECTION_NAME = "joycity_pages"
GRAPH_NAME = "joycity_kg"
DEPT_LABEL = "legacy"

if args.dept:
    sys.path.insert(0, str(ROOT / "src" / "pipeline"))
    from dept_config import load_dept as _ld

    _cfg = _ld(args.dept)
    COLLECTION_NAME = _cfg["qdrant_collection"]
    GRAPH_NAME = _cfg["falkordb_graph"]
    DEPT_LABEL = f"{_cfg['name']} ({args.dept})"

OUT_PATH = args.out or str(
    ROOT / "data" / "eval" / f"golden_set_{datetime.now(UTC).strftime('%Y%m%d')}.json"
)

print("=" * 60)
print("  골든셋 자동 생성")
print("=" * 60)
print(f"  본부:     {DEPT_LABEL}")
print(f"  컬렉션:   {COLLECTION_NAME}  그래프: {GRAPH_NAME}")
print(
    f"  목표:     {args.count}문항 ({', '.join(f'{c}:{n}' for c, n in CATEGORY_TARGETS.items())})"
)
print(f"  출력:     {OUT_PATH}")
print()


# ─── 클라이언트 초기화 ────────────────────────────────────────────────────────
print("🔌 클라이언트 초기화...")
from anthropic import AnthropicVertex  # noqa: E402
from google import genai as _genai  # noqa: E402
from qdrant_client import QdrantClient  # noqa: E402

import falkordb as _fdb  # noqa: E402

embed_client = _genai.Client(project=GCP_PROJECT, location=LOCATION, vertexai=True)
qdrant = QdrantClient(url=QDRANT_URL)
_db = _fdb.FalkorDB(host=FALKORDB_HOST, port=FALKORDB_PORT)
graph = _db.select_graph(GRAPH_NAME)
claude = AnthropicVertex(project_id=GCP_PROJECT, region=ANTHROPIC_REGION)
print("✅ 완료\n")


# ─── 1. Qdrant 페이지 샘플링 ─────────────────────────────────────────────────
print(f"📄 Qdrant 페이지 샘플링 (최대 {SAMPLE_PAGES}개)...")

pages = []
offset = None
while len(pages) < SAMPLE_PAGES:
    batch, next_offset = qdrant.scroll(
        collection_name=COLLECTION_NAME,
        limit=50,
        offset=offset,
        with_payload=True,
    )
    if not batch:
        break
    for p in batch:
        payload = p.payload or {}
        text = payload.get("text", "")
        title = payload.get("title", "")
        url = payload.get("source_url", "")
        if len(text) >= 100 and title:
            pages.append({"title": title, "text": text[:3000], "url": url})
    offset = next_offset
    if offset is None:
        break

random.shuffle(pages)
pages = pages[:SAMPLE_PAGES]
print(f"  수집: {len(pages)}개 페이지\n")


# ─── 2. FalkorDB 관계 샘플링 ─────────────────────────────────────────────────
print(f"🔗 FalkorDB 관계 샘플링 (최대 {SAMPLE_RELS}개)...")

try:
    rel_result = graph.query(
        "MATCH (n)-[r:REL]->(m) "
        "RETURN n.name, r.rel_name, m.name, r.condition, r.source_url "
        f"LIMIT {SAMPLE_RELS}"
    )
    relations = [
        {
            "subject": row[0] or "",
            "predicate": row[1] or "",
            "object": row[2] or "",
            "condition": row[3] or "",
            "url": row[4] or "",
        }
        for row in rel_result.result_set
    ]
    random.shuffle(relations)
    print(f"  수집: {len(relations)}개 관계\n")
except Exception as e:
    relations = []
    print(f"  ⚠️  FalkorDB 조회 실패: {e}\n")


# ─── Q&A 생성 프롬프트 ────────────────────────────────────────────────────────
PAGE_QA_PROMPT = """다음 Notion 문서를 읽고 평가용 Q&A를 생성하세요.

문서 제목: {title}
문서 내용:
{text}

다음 카테고리 중 이 문서에서 답할 수 있는 질문을 생성하세요:
- 담당자: 특정 업무/역할의 담당자·팀을 묻는 질문
- 정책/규정: 규칙·기준·절차를 묻는 질문
- 문서위치: 특정 문서·시트·링크 위치를 묻는 질문
- 복합: 여러 조건을 동시에 묻는 질문

아래 JSON 배열 형식으로 반환하세요 (가능한 것만, 최대 3개):
[
  {{
    "category": "담당자|정책/규정|문서위치|복합",
    "difficulty": "easy|medium|hard",
    "question": "한국어 질문",
    "answer": "문서에서 추출한 정확한 답변 (짧고 명확하게)"
  }}
]

조건:
- 질문은 문서 내용만으로 답할 수 있어야 함
- 정답은 문서 텍스트에서 직접 추출 가능해야 함
- 추측이나 추론이 필요한 질문은 제외
- 답변은 50자 이내로 간결하게

━━ 질문 작성 규칙 (반드시 지킬 것) ━━━━━━━━━━━━━━━━━━━━━━━━━

① 대상을 특정하세요.
   문서에 같은 종류의 대상(쿼리·테이블·단계·프로세스 등)이 여럿이면,
   어느 것을 묻는지 질문에 반드시 밝히세요.

   나쁨: "쿼리에서 GROUP BY 절에 포함된 항목들은 무엇인가요?"
         → 문서에 쿼리가 3개면 어느 것인지 알 수 없어 답이 갈립니다.
   좋음: "월별 매출 집계 쿼리의 GROUP BY 절에 포함된 항목은 무엇인가요?"

   나쁨: "테이블 조인 조건과 날짜 집계 단위는 각각 무엇인가요?"
   좋음: "국가별 코호트 쿼리에서 두 테이블을 조인하는 조건은 무엇인가요?"

② 정답은 질문이 요구한 것만 담으세요.
   질문에서 묻지 않은 정보를 정답에 넣지 마세요.

   나쁨: Q "정산 원본 파일을 제공하는 곳은 어디인가요?"
         A "재무팀과 퍼블리셔가 분기마다 제공"   ← 주기는 묻지 않았음
   좋음: Q "정산 원본 파일을 제공하는 곳은 어디인가요?"
         A "재무팀, 퍼블리셔"
   또는: Q "정산 원본 파일은 어디에서 어떤 주기로 제공되나요?"
         A "재무팀과 퍼블리셔가 분기마다 제공"

③ 질문만 읽고도 무엇을 묻는지 알 수 있어야 합니다.
   "해당 쿼리", "이 단계", "위 문서" 처럼 문맥에 기대는 표현을 쓰지 마세요."""

REL_QA_PROMPT = """다음 지식 그래프 관계들을 보고 평가용 Q&A를 생성하세요.

관계 목록:
{relations}

"관계" 카테고리 질문을 생성하세요.
예: "A는 B와 어떤 관계인가요?", "A팀이 담당하는 업무는 무엇인가요?"

아래 JSON 배열 형식으로 반환하세요 (최대 3개):
[
  {{
    "category": "관계",
    "difficulty": "easy|medium|hard",
    "question": "한국어 질문",
    "answer": "관계에서 추출한 정확한 답변"
  }}
]

조건:
- 주어진 관계 데이터만으로 답할 수 있어야 함
- 구체적인 이름/팀명/관계명을 포함
- 답변은 50자 이내

━━ 질문 작성 규칙 (반드시 지킬 것) ━━━━━━━━━━━━━━━━━━━━━━━━━

① 주어를 특정하세요. 관계의 양쪽 엔티티 이름을 질문에 명시합니다.
   나쁨: "이 팀이 담당하는 업무는?"
   좋음: "퍼포먼스팀이 담당하는 업무는 무엇인가요?"

② 정답은 질문이 요구한 것만 담으세요.
   질문이 "무엇을 운영하나요?" 이면 정답도 운영 대상만 적습니다.
   관계의 방향·조건 등 묻지 않은 정보는 넣지 마세요.

③ 같은 엔티티가 여러 관계를 가지면, 어느 관계를 묻는지 드러내세요.
   나쁨: "BI팀과 데이터 적재는 어떤 관계인가요?"
   좋음: "BI팀이 데이터 적재 프로세스에 대해 수행한 역할은 무엇인가요?\""""


def parse_qa_response(text: str) -> list:
    """Claude 응답에서 JSON Q&A 추출

    ※ greedy 매칭 사용: 정답 문자열에 ']' 가 포함되어도 배열이 잘리지 않도록
      첫 '[' 부터 마지막 ']' 까지를 취합니다.
    """
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return []
    try:
        items = json.loads(m.group())
        result = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if not all(k in item for k in ("category", "difficulty", "question", "answer")):
                continue
            if item["category"] not in ("담당자", "정책/규정", "관계", "문서위치", "복합"):
                continue
            if item["difficulty"] not in ("easy", "medium", "hard"):
                continue
            if len(item["question"]) < 10 or len(item["answer"]) < 2:
                continue
            result.append(item)
        return result
    except Exception:
        return []


# ─── 검증 함수 ───────────────────────────────────────────────────────────────
# 채택하려면 두 관문을 모두 통과해야 합니다.
#   ① verify_grounded    — 정답이 원문에 근거하는가 (환각 필터)
#   ② verify_answerable  — 질문이 답을 하나로 특정하는가 (모호성 필터)
#
# ※ 중요 — 검색 파이프라인 통과 여부를 채택 기준으로 쓰면 안 됩니다:
#   골든셋 채택 기준 = 평가 대상 파이프라인 → "이미 답할 수 있는 질문"만 남아
#   평가 점수가 인위적으로 100%에 수렴하고 시스템 약점이 측정되지 않습니다.
#   검색 통과 여부는 --baseline 플래그로 참고 정보(baseline_pass)로만 기록합니다.

_GROUND_PROMPT = """다음 답변이 주어진 원문으로 뒷받침되는지 엄격하게 판단하세요.

원문:
{source}

질문: {question}
답변: {answer}

판단 기준:
- pass: 답변의 핵심 정보가 원문에 명시적으로 있음 (표현이 달라도 의미가 같으면 pass)
- fail: 원문에 없는 내용 / 추측 / 원문과 불일치 / 원문보다 과도하게 구체적

JSON으로만 응답: {{"verdict": "pass"|"fail", "reason": "한 줄"}}"""

# 모호성 필터 — 실제 평가에서 반복 실패한 문항 유형을 걸러냅니다.
#   · "쿼리에서 GROUP BY 항목은?"      → 문서에 쿼리가 여럿이라 답이 갈림
#   · "테이블 조인 조건은?"             → 어느 조인인지 불명
#   · Q "제공하는 곳은?" / A "…분기마다" → 묻지 않은 정보가 정답에 포함
# 이런 문항은 시스템이 정답을 찾아도 채점에서 부분점수가 나와, 검색 품질이
# 아니라 문항 품질을 측정하게 됩니다.
_ANSWERABLE_PROMPT = """다음 Q&A가 검색 시스템 평가 문항으로 적절한지 판단하세요.

원문:
{source}

질문: {question}
정답: {answer}

아래 중 하나라도 해당하면 reject:

1. 대상 미특정 — 원문에 같은 종류의 대상(쿼리·테이블·단계·프로세스·문서 등)이
   여럿인데 질문이 어느 것인지 밝히지 않아, 원문을 본 사람도 답을 하나로
   고를 수 없음
2. 범위 초과 — 정답이 질문에서 묻지 않은 정보를 포함
   (예: "어디인가요?" 라고 물었는데 정답에 주기·시점·이유가 들어감)
3. 문맥 의존 — "해당 쿼리", "이 단계", "위 문서" 처럼 질문만으로는
   무엇을 가리키는지 알 수 없음
4. 정답 불완전 — 원문에 근거가 더 있는데 정답이 일부만 담아, 완전한 답변이
   오히려 오답 처리될 수 있음

문제가 없으면 accept.

JSON으로만 응답: {{"verdict": "accept"|"reject", "reason": "한 줄"}}"""

_ANSWER_PROMPT = """아래 컨텍스트를 바탕으로 질문에 답하세요. 컨텍스트에 없는 내용은 답하지 마세요.

컨텍스트:
{context}

질문: {question}

답변 (간결하게):"""

_SCORE_PROMPT = """다음 응답이 정답의 핵심 정보를 포함하는지 판단하세요.

질문: {question}
정답: {answer}
응답: {response}

JSON으로만 응답: {{"verdict": "pass"|"fail", "reason": "한 줄"}}
- pass: 응답이 정답의 핵심 정보를 포함
- fail: 응답에 정보 없음/틀림/부분적"""


def _embed_text(text: str) -> list:
    result = embed_client.models.embed_content(
        model="text-multilingual-embedding-002", contents=[text[:2000]]
    )
    return result.embeddings[0].values


def _judge_verdict(prompt: str, ok_value: str = "pass", max_tokens: int = 150) -> tuple[bool, str]:
    """LLM 판정을 요청하고 (통과 여부, 사유) 를 반환."""
    resp = claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return False, "판정 파싱 실패"
    d = json.loads(m.group())
    return d.get("verdict", "") == ok_value, str(d.get("reason", ""))


# API 오류를 조용히 삼키면 모든 문항이 탈락해도 원인을 알 수 없으므로
# 첫 실패만 표면화합니다 (리전 설정 오류 등의 진단용).
_err_shown: set[str] = set()

# 탈락 사유 집계 — 생성 프롬프트를 어디로 고쳐야 할지 알려줍니다.
_reject_counts: dict = {}
_reject_samples: list = []


def _warn_once(kind: str, exc: Exception) -> None:
    if kind not in _err_shown:
        _err_shown.add(kind)
        print(f"\n  ⚠️  {kind} LLM 호출 실패 — 이후 동일 오류는 생략합니다:\n     {exc}\n")


def verify_grounded(question: str, answer: str, source_text: str) -> bool:
    """★ 채택 관문 ① — 정답이 소스 원문으로 뒷받침되는지 확인.

    LLM이 생성한 정답의 환각을 걸러냅니다.
    검색 파이프라인을 거치지 않으므로 평가 대상과 독립적입니다.
    """
    try:
        ok, _ = _judge_verdict(
            _GROUND_PROMPT.format(
                source=source_text[:3000],
                question=question,
                answer=answer,
            )
        )
        return ok
    except Exception as e:
        _warn_once("근거 검증", e)
        return False


def verify_answerable(question: str, answer: str, source_text: str) -> tuple[bool, str]:
    """★ 채택 관문 ② — 질문이 답을 하나로 특정하는지 확인.

    근거가 있어도 질문이 모호하면 평가가 문항 품질을 재게 됩니다.
    실제로 "쿼리에서 GROUP BY 항목은?"(문서에 쿼리 3개), "제공하는 곳은?"에
    주기까지 담은 정답 같은 문항이 반복해서 부분점수를 받았습니다.

    Returns:
        (채택 여부, 사유)
    """
    try:
        return _judge_verdict(
            _ANSWERABLE_PROMPT.format(
                source=source_text[:3000],
                question=question,
                answer=answer,
            ),
            ok_value="accept",
        )
    except Exception as e:
        _warn_once("모호성 검증", e)
        # 판정 불가 시에는 통과시킵니다 — 근거 검증은 이미 통과한 문항이므로
        # 검증기 장애로 골든셋이 비는 것보다 낫습니다.
        return True, "검증 생략"


def baseline_search_pass(question: str, answer: str, search_limit: int = 7) -> bool:
    """참고 정보 — 현재 검색 파이프라인이 이 질문에 답할 수 있는지.

    ※ 채택 기준이 아닙니다. --baseline 플래그로만 실행되며
      결과는 문항의 baseline_pass 필드에 기록됩니다.
      fail인 문항이 곧 '개선이 필요한 지점'이므로 오히려 가치가 높습니다.
    """
    try:
        vec = _embed_text(question)
        result = qdrant.query_points(
            collection_name=COLLECTION_NAME,
            query=vec,
            limit=search_limit,
            with_payload=True,
        )
        if not result.points:
            return False

        context = ""
        for h in result.points:
            p = h.payload or {}
            context += f"[{p.get('title', '')}]\n{p.get('text', '')[:600]}\n\n"

        gen = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=200,
            messages=[
                {
                    "role": "user",
                    "content": _ANSWER_PROMPT.format(context=context[:4000], question=question),
                }
            ],
        )
        response = gen.content[0].text.strip()

        ok, _ = _judge_verdict(
            _SCORE_PROMPT.format(question=question, answer=answer, response=response[:500]),
            max_tokens=100,
        )
        return ok
    except Exception as e:
        _warn_once("baseline 검색", e)
        return False


# ─── 3. 페이지 기반 Q&A 생성 + 즉시 검증 ────────────────────────────────────
print("🤖 페이지 기반 Q&A 생성 + 검증 중...")
all_candidates = []

# 카테고리별 현재 수집 현황 추적
cat_counts = dict.fromkeys(CATEGORY_TARGETS, 0)

for i, page in enumerate(pages):
    # 목표 달성 시 중단 (관계 제외)
    non_rel_done = all(
        cat_counts[c] >= CATEGORY_TARGETS[c] * 2  # 후보 2배 수집 후 선별
        for c in ("담당자", "정책/규정", "문서위치", "복합")
    )
    if non_rel_done:
        break

    print(f"  [{i + 1}/{len(pages)}] {page['title'][:40]}", end="", flush=True)
    try:
        msg = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=600,
            messages=[
                {
                    "role": "user",
                    "content": PAGE_QA_PROMPT.format(
                        title=page["title"],
                        text=page["text"],
                    ),
                }
            ],
        )
        items = parse_qa_response(msg.content[0].text)

        verified = 0
        for item in items:
            # ★ 관문 ①: 정답이 이 문서 원문으로 뒷받침되는지 (환각 필터)
            if not verify_grounded(item["question"], item["answer"], page["text"]):
                _reject_counts["근거 없음"] = _reject_counts.get("근거 없음", 0) + 1
                time.sleep(0.2)
                continue

            # ★ 관문 ②: 질문이 답을 하나로 특정하는지 (모호성 필터)
            ok, why = verify_answerable(item["question"], item["answer"], page["text"])
            if not ok:
                _reject_counts["모호함"] = _reject_counts.get("모호함", 0) + 1
                _reject_samples.append((item["question"][:48], why[:40]))
                time.sleep(0.2)
                continue

            item["source_url"] = page["url"]
            item["source_title"] = page["title"]
            item["grounded"] = True
            # 참고 정보: 현재 검색 파이프라인 통과 여부 (채택 여부와 무관)
            if args.baseline:
                item["baseline_pass"] = baseline_search_pass(item["question"], item["answer"])
            all_candidates.append(item)
            cat_counts[item["category"]] = cat_counts.get(item["category"], 0) + 1
            verified += 1
            time.sleep(0.2)

        print(f" → {len(items)}개 생성, {verified}개 근거 확인")
    except Exception as e:
        print(f" ⚠️  {e}")

    time.sleep(0.3)

print()

# ─── 4. 관계 기반 Q&A 생성 ───────────────────────────────────────────────────
if relations and cat_counts.get("관계", 0) < CATEGORY_TARGETS["관계"]:
    print("🤖 관계 기반 Q&A 생성 중...")
    # 관계를 5개씩 묶어 처리. 훑을 관계 수는 목표 문항에 비례합니다
    # (고정 30개였을 때는 --count 를 올려도 관계 후보가 늘지 않았습니다).
    _rel_scan = min(len(relations), max(30, CATEGORY_TARGETS["관계"] * 8))
    for chunk_start in range(0, _rel_scan, 5):
        if cat_counts.get("관계", 0) >= CATEGORY_TARGETS["관계"] * 3:
            break
        chunk = relations[chunk_start : chunk_start + 5]
        rel_text = "\n".join(
            f"- {r['subject']} →[{r['predicate']}]→ {r['object']}"
            + (f" (조건: {r['condition']})" if r.get("condition") else "")
            for r in chunk
        )
        print(f"  관계 묶음 [{chunk_start + 1}~{chunk_start + len(chunk)}]...", end="", flush=True)
        try:
            msg = claude.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=400,
                messages=[
                    {
                        "role": "user",
                        "content": REL_QA_PROMPT.format(relations=rel_text),
                    }
                ],
            )
            items = parse_qa_response(msg.content[0].text)
            verified = 0
            for item in items:
                # ★ 관문 ①: 정답이 이 관계 데이터로 뒷받침되는지
                if not verify_grounded(item["question"], item["answer"], rel_text):
                    _reject_counts["근거 없음"] = _reject_counts.get("근거 없음", 0) + 1
                    time.sleep(0.2)
                    continue

                # ★ 관문 ②: 질문이 답을 하나로 특정하는지
                ok, why = verify_answerable(item["question"], item["answer"], rel_text)
                if not ok:
                    _reject_counts["모호함"] = _reject_counts.get("모호함", 0) + 1
                    _reject_samples.append((item["question"][:48], why[:40]))
                    time.sleep(0.2)
                    continue

                item["source_url"] = chunk[0].get("url", "")
                item["source_title"] = f"관계: {chunk[0]['subject']}"
                item["grounded"] = True
                if args.baseline:
                    item["baseline_pass"] = baseline_search_pass(item["question"], item["answer"])
                all_candidates.append(item)
                cat_counts["관계"] = cat_counts.get("관계", 0) + 1
                verified += 1
                time.sleep(0.2)
            print(f" → {len(items)}개 생성, {verified}개 근거 확인")
        except Exception as e:
            print(f" ⚠️  {e}")
        time.sleep(0.3)
    print()


# ─── 5. 후보 선별 및 균형 조정 ───────────────────────────────────────────────
print("⚖️  카테고리 균형 조정 중...")

# 카테고리별로 분류
by_cat: dict = {c: [] for c in CATEGORY_TARGETS}
for item in all_candidates:
    cat = item.get("category", "")
    if cat in by_cat:
        by_cat[cat].append(item)

# 카테고리별 난이도 비율에 맞게 선택
final_set = []
qid = 1

for cat, target in CATEGORY_TARGETS.items():
    pool = by_cat.get(cat, [])
    if not pool:
        print(f"  ⚠️  {cat}: 후보 없음")
        continue

    # 난이도별 분류
    by_diff = {"easy": [], "medium": [], "hard": []}
    for item in pool:
        d = item.get("difficulty", "medium")
        if d in by_diff:
            by_diff[d].append(item)

    # 난이도 목표 수 계산
    diff_targets = {d: max(1, round(target * ratio)) for d, ratio in DIFFICULTY_DIST.items()}
    # 합이 target과 다르면 medium에서 보정
    diff_sum = sum(diff_targets.values())
    diff_targets["medium"] += target - diff_sum

    selected = []
    for diff, n in diff_targets.items():
        pool_d = by_diff[diff]
        random.shuffle(pool_d)
        selected.extend(pool_d[:n])

    # 부족하면 남은 풀에서 보충
    if len(selected) < target:
        remaining = [x for x in pool if x not in selected]
        random.shuffle(remaining)
        selected.extend(remaining[: target - len(selected)])

    selected = selected[:target]
    for item in selected:
        entry = {
            "id": f"Q{qid:02d}",
            "category": item["category"],
            "difficulty": item.get("difficulty", "medium"),
            "question": item["question"],
            "answer": item["answer"],
            "source_url": item.get("source_url", ""),
            "source_title": item.get("source_title", ""),
            # 정답이 원문으로 뒷받침됨 (채택 기준)
            "grounded": item.get("grounded", False),
        }
        # --baseline 실행 시에만 존재 — 현재 파이프라인 통과 여부 (참고)
        if "baseline_pass" in item:
            entry["baseline_pass"] = item["baseline_pass"]
        final_set.append(entry)
        qid += 1

    short = f"  ⚠️ 목표 {target}개 미달" if len(selected) < target else ""
    print(f"  {cat:<10}: {len(selected)}개 선택 (후보 {len(pool)}개){short}")

print(f"\n  최종 선정: {len(final_set)}문항 (목표 {args.count})")

# 목표 미달 — 원인에 따라 대응이 다르므로 구분해 안내합니다.
if len(final_set) < args.count:
    print(f"  ⚠️  목표보다 {args.count - len(final_set)}문항 부족합니다.")
    print(f"      · 샘플 확대: --sample-pages {SAMPLE_PAGES * 2} --sample-rels {SAMPLE_RELS * 2}")
    print("      · 탈락이 많으면 아래 사유를 보고 생성 프롬프트를 보강하세요")

# 탈락 사유 — 생성 프롬프트를 어디로 보강할지 알려줍니다.
if _reject_counts:
    print(f"  탈락: {', '.join(f'{k} {v}건' for k, v in _reject_counts.items())}")
    for q, why in _reject_samples[:4]:
        print(f"    · {q}  ← {why}")
print()


# ─── 6. 저장 ──────────────────────────────────────────────────────────────────
Path(OUT_PATH).parent.mkdir(parents=True, exist_ok=True)
meta = {
    "generated_at": datetime.now(UTC).isoformat(),
    "dept": args.dept,
    "collection": COLLECTION_NAME,
    "graph": GRAPH_NAME,
    "total": len(final_set),
    # 두 관문을 모두 통과한 문항만 채택됩니다.
    "acceptance_criteria": ["answer_grounded_in_source", "question_answerable_unambiguously"],
    "rejected": dict(_reject_counts),
    "category_counts": {
        c: sum(1 for q in final_set if q["category"] == c) for c in CATEGORY_TARGETS
    },
}
if args.baseline:
    _bp = [q for q in final_set if "baseline_pass" in q]
    meta["baseline"] = {
        "measured": len(_bp),
        "passed": sum(1 for q in _bp if q["baseline_pass"]),
    }
output = {"meta": meta, "questions": final_set}
Path(OUT_PATH).write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"💾 저장 완료: {OUT_PATH}")
if args.baseline and meta.get("baseline", {}).get("measured"):
    _b = meta["baseline"]
    print(
        f"   참고 — 현재 파이프라인 baseline: {_b['passed']}/{_b['measured']} 통과 "
        f"(낮을수록 개선 여지가 큼)"
    )
print()
print("  다음 단계:")
print(f"  1. 파일 검토 및 수동 수정: {OUT_PATH}")
print(
    f"  2. 평가 실행: python src/eval/evaluate.py --dept {args.dept or 'strategic'} --golden {OUT_PATH}"
)
