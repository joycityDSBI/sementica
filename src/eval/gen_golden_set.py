#!/usr/bin/env python3
"""
골든셋 자동 생성 스크립트
실제 Qdrant + FalkorDB 데이터에서 평가 질문을 생성합니다.

채택 기준 (중요) — 두 관문을 모두 통과해야 합니다 (verify_qa 가 한 번에 판정):
    ① grounded    정답이 소스 원문으로 뒷받침되는가 (환각 필터)
    ② answerable  질문이 답을 하나로 특정하는가 (모호성 필터)

    ②가 필요한 이유: 근거가 있어도 질문이 모호하면 평가가 검색 품질이 아니라
    문항 품질을 재게 됩니다. 실제로 "쿼리에서 GROUP BY 항목은?"(문서에 쿼리
    3개), "제공하는 곳은?"인데 정답에 주기까지 담긴 문항이 반복해서 부분점수를
    받았습니다.

    검색 파이프라인 통과 여부는 채택 기준이 아닙니다 — 그러면 골든셋이 "이미
    답할 수 있는 질문"만 남아 점수가 100%에 수렴하고 약점이 드러나지 않습니다.
    --baseline 으로 참고 정보(baseline_pass)로만 기록합니다.

속도:
    페이지·관계 처리를 병렬로 수행하고(--workers, 기본 8), 두 관문을 한 번의
    LLM 호출로 판정합니다. 순차·분리 호출이던 이전 대비 문항당 호출이 절반이고
    대기 시간이 겹칩니다.
    검증 모델을 Haiku 로 낮추는 것도 시도했으나 "범위 초과" 판정을 놓쳐
    되돌렸습니다 — 필요하면 GOLDEN_JUDGE_MODEL 로 바꿀 수 있습니다.

실행:
    python src/eval/gen_golden_set.py --dept strategic
    python src/eval/gen_golden_set.py --dept strategic --count 40
    python src/eval/gen_golden_set.py --dept strategic --count 40 --workers 12
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
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
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
GEN_MODEL = os.environ.get("GOLDEN_GEN_MODEL", CLAUDE_MODEL)
# 검증도 Sonnet 을 씁니다. Haiku 로 낮춰 실측했더니 "범위 초과"(정답이 묻지 않은
# 정보를 포함) 판정을 놓치고 JSON 이 잘려 파싱에 실패했습니다 — 골든셋 품질이
# 떨어지면 평가 전체가 무의미해지므로 속도보다 판정 정확도를 택합니다.
# 속도가 급하면 GOLDEN_JUDGE_MODEL 로 낮출 수 있으나 탈락 기준이 느슨해집니다.
JUDGE_MODEL = os.environ.get("GOLDEN_JUDGE_MODEL", CLAUDE_MODEL)
# 판정 JSON 이 잘리지 않도록 넉넉히 — 150 에서는 reason 이 길 때 파싱에 실패했습니다.
JUDGE_MAX_TOKENS = 300

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
    "--workers",
    type=int,
    default=8,
    help="병렬 워커 수 (기본 8). Vertex AI 쿼터에 따라 조정",
)
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
# 채택하려면 두 관문을 모두 통과해야 합니다 (verify_qa 가 한 번에 판정).
#   ① grounded    — 정답이 원문에 근거하는가 (환각 필터)
#   ② answerable  — 질문이 답을 하나로 특정하는가 (모호성 필터)
#
# ※ 중요 — 검색 파이프라인 통과 여부를 채택 기준으로 쓰면 안 됩니다:
#   골든셋 채택 기준 = 평가 대상 파이프라인 → "이미 답할 수 있는 질문"만 남아
#   평가 점수가 인위적으로 100%에 수렴하고 시스템 약점이 측정되지 않습니다.
#   검색 통과 여부는 --baseline 플래그로 참고 정보(baseline_pass)로만 기록합니다.

# 두 관문을 한 번의 호출로 판정합니다. 입력(원문·질문·정답)이 동일하므로
# 나눠 보내면 같은 컨텍스트를 두 번 전송하게 되고, 검증이 전체 LLM 호출의
# 대부분을 차지해 생성 시간이 배로 늘어납니다.
_VERIFY_PROMPT = """다음 Q&A가 검색 시스템 평가 문항으로 적절한지 두 단계로 판단하세요.

원문:
{source}

질문: {question}
정답: {answer}

━━ 1단계: 근거 (grounded) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
정답이 원문으로 뒷받침되는가?
- true : 정답의 핵심 정보가 원문에 있음 (표현이 달라도 의미가 같으면 true)
- false: 원문에 없음 / 추측 / 원문과 불일치 / 원문보다 과도하게 구체적

━━ 2단계: 명확성 (answerable) ━━━━━━━━━━━━━━━━━━━━━━━━━━━
질문이 답을 하나로 특정하는가? 아래 중 하나라도 해당하면 false:
1. 대상 미특정 — 원문에 같은 종류의 대상(쿼리·테이블·단계·프로세스·문서 등)이
   여럿인데 질문이 어느 것인지 밝히지 않아, 원문을 본 사람도 답을 하나로 고를 수 없음
2. 범위 초과 — 정답이 질문에서 묻지 않은 정보를 포함
   (예: "어디인가요?" 라고 물었는데 정답에 주기·시점·이유가 들어감)
3. 문맥 의존 — "해당 쿼리", "이 단계", "위 문서" 처럼 질문만으로 대상을 알 수 없음
4. 정답 불완전 — 원문에 근거가 더 있는데 정답이 일부만 담아, 완전한 답변이
   오히려 오답 처리될 수 있음

JSON으로만 응답:
{{"grounded": true, "answerable": true, "reason": "한 줄"}}"""

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


def _judge_json(prompt: str, model: str = JUDGE_MODEL, max_tokens: int = 150) -> dict:
    """LLM 판정을 요청하고 JSON dict 를 반환. 실패 시 {}."""
    resp = claude.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    return json.loads(m.group()) if m else {}


def _judge_verdict(prompt: str, ok_value: str = "pass", max_tokens: int = 150) -> tuple[bool, str]:
    """단일 verdict 판정 — (통과 여부, 사유)."""
    d = _judge_json(prompt, max_tokens=max_tokens)
    if not d:
        return False, "판정 파싱 실패"
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


def verify_qa(question: str, answer: str, source_text: str) -> tuple[bool, str, str]:
    """★ 채택 판정 — 근거와 명확성을 한 번의 LLM 호출로 확인합니다.

    두 관문을 나눠 호출하면 같은 원문을 두 번 전송하게 되고, 검증이 전체
    호출의 대부분이라 생성 시간이 배로 늘어납니다.

    Returns:
        (채택 여부, 탈락 사유 분류, 상세 사유)
        분류는 "근거 없음" | "모호함" | "" (채택).
    """
    try:
        d = _judge_json(
            _VERIFY_PROMPT.format(
                source=source_text[:3000],
                question=question,
                answer=answer,
            ),
            max_tokens=JUDGE_MAX_TOKENS,
        )
    except Exception as e:
        _warn_once("문항 검증", e)
        return False, "검증 실패", str(e)[:40]

    if not d:
        return False, "검증 실패", "판정 파싱 실패"
    reason = str(d.get("reason", ""))
    if not d.get("grounded", False):
        return False, "근거 없음", reason
    if not d.get("answerable", False):
        return False, "모호함", reason
    return True, "", reason


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

        # 평가 파이프라인을 재현하는 목적이므로 evaluate.py 와 같은 모델을 씁니다
        # (검증용 JUDGE_MODEL 이 아님).
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


# ─── 3. 페이지 기반 Q&A 생성 + 즉시 검증 (병렬) ─────────────────────────────
print(f"🤖 페이지 기반 Q&A 생성 + 검증 중... (워커 {args.workers}개)")
all_candidates = []

# 카테고리별 현재 수집 현황 추적
cat_counts = dict.fromkeys(CATEGORY_TARGETS, 0)

_lock = threading.Lock()
_stop = threading.Event()  # 목표 달성 시 남은 페이지 처리를 건너뜁니다


def _generate_and_verify(source_text: str, prompt: str) -> tuple[list, list]:
    """LLM으로 Q&A를 생성하고 각 후보를 검증합니다.

    Returns:
        (채택된 item 목록, [(분류, 질문, 사유), ...] 탈락 목록)
    """
    msg = claude.messages.create(
        model=GEN_MODEL,
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    accepted: list = []
    rejected: list = []
    for item in parse_qa_response(msg.content[0].text):
        ok, kind, why = verify_qa(item["question"], item["answer"], source_text)
        if ok:
            item["grounded"] = True
            accepted.append(item)
        else:
            rejected.append((kind, item["question"][:48], why[:40]))
    return accepted, rejected


def _process_page(page: dict) -> tuple:
    """한 페이지에서 Q&A를 생성·검증합니다. (페이지, 생성수, 채택, 탈락, 오류)"""
    if _stop.is_set():
        return page, 0, [], [], None
    try:
        prompt = PAGE_QA_PROMPT.format(title=page["title"], text=page["text"])
        accepted, rejected = _generate_and_verify(page["text"], prompt)
        for item in accepted:
            item["source_url"] = page["url"]
            item["source_title"] = page["title"]
            # 참고 정보: 현재 검색 파이프라인 통과 여부 (채택 여부와 무관)
            if args.baseline:
                item["baseline_pass"] = baseline_search_pass(item["question"], item["answer"])
        return page, len(accepted) + len(rejected), accepted, rejected, None
    except Exception as e:
        return page, 0, [], [], e


_NON_REL = ("담당자", "정책/규정", "문서위치", "복합")
_done = 0
with ThreadPoolExecutor(max_workers=args.workers) as pool:
    futures = [pool.submit(_process_page, p) for p in pages]
    for fut in as_completed(futures):
        page, generated, accepted, rejected, err = fut.result()
        _done += 1
        with _lock:
            for item in accepted:
                all_candidates.append(item)
                cat_counts[item["category"]] = cat_counts.get(item["category"], 0) + 1
            for kind, q, why in rejected:
                _reject_counts[kind] = _reject_counts.get(kind, 0) + 1
                if kind == "모호함":
                    _reject_samples.append((q, why))
            # 후보를 목표의 2배까지 모으면 중단 — 이후 난이도 균형을 맞춰 선별합니다
            if all(cat_counts[c] >= CATEGORY_TARGETS[c] * 2 for c in _NON_REL):
                _stop.set()

        tail = f"⚠️  {err}" if err else f"→ {generated}개 생성, {len(accepted)}개 채택"
        print(f"  [{_done}/{len(pages)}] {page['title'][:36]:38} {tail}")

if _stop.is_set():
    print("  (카테고리 목표 도달 — 남은 페이지 생략)")
print()

# ─── 4. 관계 기반 Q&A 생성 ───────────────────────────────────────────────────
if relations and cat_counts.get("관계", 0) < CATEGORY_TARGETS["관계"]:
    print("🤖 관계 기반 Q&A 생성 중...")
    # 관계를 5개씩 묶어 처리. 훑을 관계 수는 목표 문항에 비례합니다
    # (고정 30개였을 때는 --count 를 올려도 관계 후보가 늘지 않았습니다).
    _rel_scan = min(len(relations), max(30, CATEGORY_TARGETS["관계"] * 8))
    _rel_chunks = [relations[s : s + 5] for s in range(0, _rel_scan, 5)]
    _rel_stop = threading.Event()

    def _process_rel_chunk(chunk: list) -> tuple:
        """관계 묶음에서 Q&A를 생성·검증합니다. (라벨, 생성수, 채택, 탈락, 오류)"""
        label = f"{chunk[0]['subject'][:20]} 외 {len(chunk) - 1}건"
        if _rel_stop.is_set():
            return label, 0, [], [], None
        rel_text = "\n".join(
            f"- {r['subject']} →[{r['predicate']}]→ {r['object']}"
            + (f" (조건: {r['condition']})" if r.get("condition") else "")
            for r in chunk
        )
        try:
            accepted, rejected = _generate_and_verify(
                rel_text, REL_QA_PROMPT.format(relations=rel_text)
            )
            for item in accepted:
                item["source_url"] = chunk[0].get("url", "")
                item["source_title"] = f"관계: {chunk[0]['subject']}"
                if args.baseline:
                    item["baseline_pass"] = baseline_search_pass(item["question"], item["answer"])
            return label, len(accepted) + len(rejected), accepted, rejected, None
        except Exception as e:
            return label, 0, [], [], e

    _rdone = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(_process_rel_chunk, c) for c in _rel_chunks]
        for fut in as_completed(futures):
            label, generated, accepted, rejected, err = fut.result()
            _rdone += 1
            with _lock:
                for item in accepted:
                    all_candidates.append(item)
                    cat_counts["관계"] = cat_counts.get("관계", 0) + 1
                for kind, q, why in rejected:
                    _reject_counts[kind] = _reject_counts.get(kind, 0) + 1
                    if kind == "모호함":
                        _reject_samples.append((q, why))
                if cat_counts.get("관계", 0) >= CATEGORY_TARGETS["관계"] * 3:
                    _rel_stop.set()

            tail = f"⚠️  {err}" if err else f"→ {generated}개 생성, {len(accepted)}개 채택"
            print(f"  [{_rdone}/{len(_rel_chunks)}] {label:38} {tail}")
    print()


# ─── 5. 후보 선별 및 균형 조정 ───────────────────────────────────────────────
print("⚖️  카테고리 균형 조정 중...")


def _norm_question(q: str) -> str:
    """중복 판정용 정규화 — 공백·문장부호·대소문자 차이를 무시합니다."""
    return re.sub(r"[\s\W_]+", "", (q or "").lower())


# 같은 질문 중복 제거.
# 같은 문서의 여러 버전(예: "점검 진행 프로세스" 사본들)이 각각 후보로 들어와
# 글자 하나 다르지 않은 질문이 여러 문항으로 채택된 적이 있습니다. 전부 같은
# 점수를 받으므로 평균이 부풀고, 40문항인데 실제로 검증하는 것은 37개였습니다.
_seen_q: set = set()
_dupes: list = []
_deduped: list = []
for item in all_candidates:
    key = _norm_question(item.get("question", ""))
    if not key or key in _seen_q:
        _dupes.append(item.get("question", ""))
        continue
    _seen_q.add(key)
    _deduped.append(item)

if _dupes:
    print(f"  중복 질문 {len(_dupes)}건 제거 (후보 {len(all_candidates)} → {len(_deduped)})")
    for q in _dupes[:3]:
        print(f"    - {q[:60]}")
all_candidates = _deduped

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
# 최종 점검 — 위에서 걸렀어도 한 번 더 확인합니다. 중복이 남으면 평균이
# 부풀고 실제 검증 범위가 문항 수보다 좁아집니다.
_final_keys = [_norm_question(q["question"]) for q in final_set]
_final_dupes = len(_final_keys) - len(set(_final_keys))
_sources = {q.get("source_url", "") for q in final_set if q.get("source_url")}
print(f"  최종 {len(final_set)}문항 / 서로 다른 출처 문서 {len(_sources)}개")
if _final_dupes:
    print(f"  ⚠️  중복 질문 {_final_dupes}건이 최종 세트에 남아 있습니다 — 확인하세요")
meta["distinct_sources"] = len(_sources)
meta["duplicate_questions"] = _final_dupes

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
