#!/usr/bin/env python3
"""
골든셋 자동 생성 스크립트
실제 Qdrant + FalkorDB 데이터에서 평가 질문을 생성합니다.

실행:
    python src/eval/gen_golden_set.py --dept strategic
    python src/eval/gen_golden_set.py --dept strategic --count 30 --out data/eval/golden_set.json

결과:
    data/eval/golden_set.json  ← evaluate.py가 --golden 옵션으로 로드
"""

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

# ─── 경로 설정 ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

_env = ROOT / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

GCP_PROJECT   = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
LOCATION      = os.environ.get("VERTEX_AI_LOCATION", "us-east5")
QDRANT_URL    = os.environ.get("QDRANT_URL", "http://localhost:6333")
FALKORDB_HOST = os.environ.get("FALKORDB_HOST", "localhost")
FALKORDB_PORT = int(os.environ.get("FALKORDB_PORT", "6379"))
CLAUDE_MODEL  = "claude-sonnet-4-6@default"

# 카테고리별 목표 문항 수
CATEGORY_TARGETS = {
    "담당자":   5,
    "정책/규정": 4,
    "관계":     5,
    "문서위치":  3,
    "복합":     3,
}
DIFFICULTY_DIST = {"easy": 0.3, "medium": 0.5, "hard": 0.2}

# ─── 인수 파싱 ────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--dept",  default="", help="본부 키 (config/departments.yaml)")
parser.add_argument("--count", type=int, default=20, help="목표 문항 수 (기본 20)")
parser.add_argument("--sample-pages", type=int, default=60,
                    help="Qdrant 샘플 페이지 수 (기본 60)")
parser.add_argument("--sample-rels",  type=int, default=80,
                    help="FalkorDB 샘플 관계 수 (기본 80)")
parser.add_argument("--out", default="",
                    help="출력 파일 경로 (기본: data/eval/golden_set_YYYYMMDD.json)")
parser.add_argument("--seed", type=int, default=42, help="난수 시드")
args = parser.parse_args()

random.seed(args.seed)

COLLECTION_NAME = "joycity_pages"
GRAPH_NAME      = "joycity_kg"
DEPT_LABEL      = "legacy"

if args.dept:
    sys.path.insert(0, str(ROOT / "src" / "pipeline"))
    from dept_config import load_dept as _ld
    _cfg = _ld(args.dept)
    COLLECTION_NAME = _cfg["qdrant_collection"]
    GRAPH_NAME      = _cfg["falkordb_graph"]
    DEPT_LABEL      = f"{_cfg['name']} ({args.dept})"

OUT_PATH = args.out or str(
    ROOT / "data" / "eval" / f"golden_set_{datetime.now().strftime('%Y%m%d')}.json"
)

print("=" * 60)
print("  골든셋 자동 생성")
print("=" * 60)
print(f"  본부:     {DEPT_LABEL}")
print(f"  컬렉션:   {COLLECTION_NAME}  그래프: {GRAPH_NAME}")
print(f"  목표:     {args.count}문항 ({', '.join(f'{c}:{n}' for c,n in CATEGORY_TARGETS.items())})")
print(f"  출력:     {OUT_PATH}")
print()


# ─── 클라이언트 초기화 ────────────────────────────────────────────────────────
print("🔌 클라이언트 초기화...")
from google import genai as _genai
from qdrant_client import QdrantClient
import falkordb as _fdb
from anthropic import AnthropicVertex

embed_client = _genai.Client(project=GCP_PROJECT, location=LOCATION, vertexai=True)
qdrant       = QdrantClient(url=QDRANT_URL)
_db          = _fdb.FalkorDB(host=FALKORDB_HOST, port=FALKORDB_PORT)
graph        = _db.select_graph(GRAPH_NAME)
claude       = AnthropicVertex(project_id=GCP_PROJECT, region=LOCATION)
print("✅ 완료\n")


# ─── 1. Qdrant 페이지 샘플링 ─────────────────────────────────────────────────
print(f"📄 Qdrant 페이지 샘플링 (최대 {args.sample_pages}개)...")

pages = []
offset = None
while len(pages) < args.sample_pages:
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
pages = pages[:args.sample_pages]
print(f"  수집: {len(pages)}개 페이지\n")


# ─── 2. FalkorDB 관계 샘플링 ─────────────────────────────────────────────────
print(f"🔗 FalkorDB 관계 샘플링 (최대 {args.sample_rels}개)...")

try:
    rel_result = graph.query(
        "MATCH (n)-[r:REL]->(m) "
        "RETURN n.name, r.rel_name, m.name, r.condition, r.source_url "
        f"LIMIT {args.sample_rels}"
    )
    relations = []
    for row in rel_result.result_set:
        relations.append({
            "subject":   row[0] or "",
            "predicate": row[1] or "",
            "object":    row[2] or "",
            "condition": row[3] or "",
            "url":       row[4] or "",
        })
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
- 답변은 50자 이내로 간결하게"""

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
- 답변은 50자 이내"""


def parse_qa_response(text: str) -> list:
    """Claude 응답에서 JSON Q&A 추출"""
    m = re.search(r'\[.*?\]', text, re.DOTALL)
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


# ─── 검색 기반 검증 함수 (전체 파이프라인) ───────────────────────────────────
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


def verify_by_search(question: str, answer: str, search_limit: int = 7) -> bool:
    """검색 → 답변 생성 → 정답 일치 확인 전체 파이프라인으로 Q&A 검증.

    평가 시와 동일한 흐름으로 검증하므로
    '검색은 되지만 답변 생성 때 정보가 누락되는' 질문도 걸러낼 수 있습니다.
    """
    try:
        # 1. 검색
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
            context += f"[{p.get('title','')}]\n{p.get('text','')[:600]}\n\n"

        # 2. 답변 생성
        gen = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": _ANSWER_PROMPT.format(
                    context=context[:4000], question=question
                ),
            }],
        )
        response = gen.content[0].text.strip()

        # 3. 생성된 답변이 정답과 일치하는지 채점
        judge = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=100,
            messages=[{
                "role": "user",
                "content": _SCORE_PROMPT.format(
                    question=question, answer=answer, response=response[:500]
                ),
            }],
        )
        text = judge.content[0].text.strip()
        m = re.search(r'\{.*?\}', text, re.DOTALL)
        if m:
            verdict = json.loads(m.group()).get("verdict", "fail")
            return verdict == "pass"
    except Exception:
        pass
    return False


# ─── 3. 페이지 기반 Q&A 생성 + 즉시 검증 ────────────────────────────────────
print("🤖 페이지 기반 Q&A 생성 + 검증 중...")
all_candidates = []

# 카테고리별 현재 수집 현황 추적
cat_counts = {c: 0 for c in CATEGORY_TARGETS}

for i, page in enumerate(pages):
    # 목표 달성 시 중단 (관계 제외)
    non_rel_done = all(
        cat_counts[c] >= CATEGORY_TARGETS[c] * 2   # 후보 2배 수집 후 선별
        for c in ("담당자", "정책/규정", "문서위치", "복합")
    )
    if non_rel_done:
        break

    print(f"  [{i+1}/{len(pages)}] {page['title'][:40]}", end="", flush=True)
    try:
        msg = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=600,
            messages=[{
                "role": "user",
                "content": PAGE_QA_PROMPT.format(
                    title=page["title"],
                    text=page["text"],
                ),
            }],
        )
        items = parse_qa_response(msg.content[0].text)

        verified = 0
        for item in items:
            # 검색 결과로 검증 — 답변 가능한 질문만 채택
            ok = verify_by_search(item["question"], item["answer"])
            if ok:
                item["source_url"] = page["url"]
                item["source_title"] = page["title"]
                item["verified"] = True
                all_candidates.append(item)
                cat_counts[item["category"]] = cat_counts.get(item["category"], 0) + 1
                verified += 1
            time.sleep(0.2)

        print(f" → {len(items)}개 생성, {verified}개 검증 통과")
    except Exception as e:
        print(f" ⚠️  {e}")

    time.sleep(0.3)

print()

# ─── 4. 관계 기반 Q&A 생성 ───────────────────────────────────────────────────
if relations and cat_counts.get("관계", 0) < CATEGORY_TARGETS["관계"]:
    print("🤖 관계 기반 Q&A 생성 중...")
    # 관계를 묶음으로 처리 (5개씩)
    for chunk_start in range(0, min(len(relations), 30), 5):
        if cat_counts.get("관계", 0) >= CATEGORY_TARGETS["관계"] * 3:
            break
        chunk = relations[chunk_start:chunk_start + 5]
        rel_text = "\n".join(
            f"- {r['subject']} →[{r['predicate']}]→ {r['object']}"
            + (f" (조건: {r['condition']})" if r.get("condition") else "")
            for r in chunk
        )
        print(f"  관계 묶음 [{chunk_start+1}~{chunk_start+len(chunk)}]...", end="", flush=True)
        try:
            msg = claude.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=400,
                messages=[{
                    "role": "user",
                    "content": REL_QA_PROMPT.format(relations=rel_text),
                }],
            )
            items = parse_qa_response(msg.content[0].text)
            verified = 0
            for item in items:
                ok = verify_by_search(item["question"], item["answer"])
                if ok:
                    item["source_url"] = chunk[0].get("url", "")
                    item["source_title"] = f"관계: {chunk[0]['subject']}"
                    item["verified"] = True
                    all_candidates.append(item)
                    cat_counts["관계"] = cat_counts.get("관계", 0) + 1
                    verified += 1
                time.sleep(0.2)
            print(f" → {len(items)}개 생성, {verified}개 검증 통과")
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
    diff_targets = {
        d: max(1, round(target * ratio))
        for d, ratio in DIFFICULTY_DIST.items()
    }
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
        selected.extend(remaining[:target - len(selected)])

    selected = selected[:target]
    for item in selected:
        final_set.append({
            "id":       f"Q{qid:02d}",
            "category": item["category"],
            "difficulty": item.get("difficulty", "medium"),
            "question": item["question"],
            "answer":   item["answer"],
            "source_url":   item.get("source_url", ""),
            "source_title": item.get("source_title", ""),
        })
        qid += 1

    print(f"  {cat:<10}: {len(selected)}개 선택 (후보 {len(pool)}개)")

print(f"\n  최종 선정: {len(final_set)}문항\n")


# ─── 6. 저장 ──────────────────────────────────────────────────────────────────
Path(OUT_PATH).parent.mkdir(parents=True, exist_ok=True)
meta = {
    "generated_at": datetime.now().isoformat(),
    "dept":         args.dept,
    "collection":   COLLECTION_NAME,
    "graph":        GRAPH_NAME,
    "total":        len(final_set),
    "category_counts": {c: sum(1 for q in final_set if q["category"] == c)
                        for c in CATEGORY_TARGETS},
}
output = {"meta": meta, "questions": final_set}
Path(OUT_PATH).write_text(
    json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(f"💾 저장 완료: {OUT_PATH}")
print()
print("  다음 단계:")
print(f"  1. 파일 검토 및 수동 수정: {OUT_PATH}")
print(f"  2. 평가 실행: python src/eval/evaluate.py --dept {args.dept or 'strategic'} --golden {OUT_PATH}")
