"""
Week 4: 골든셋 평가 스크립트
20개 질문으로 Semantica 검색 품질을 자동 평가합니다.

실행:
    python src/eval/evaluate.py

결과:
    data/eval/eval_result_YYYYMMDD_HHMMSS.json
    data/eval/eval_report_YYYYMMDD_HHMMSS.md
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# ─── 경로 설정 ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

# .env 로드
_env = ROOT / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

# ─── 설정 ─────────────────────────────────────────────────────────────────────
GCP_PROJECT     = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
LOCATION        = os.environ.get("VERTEX_AI_LOCATION", "us-east5")
EMBED_MODEL     = "text-multilingual-embedding-002"
QDRANT_URL      = os.environ.get("QDRANT_URL", "http://localhost:6333")
COLLECTION_NAME = "joycity_pages"
FALKORDB_HOST   = os.environ.get("FALKORDB_HOST", "localhost")
FALKORDB_PORT   = int(os.environ.get("FALKORDB_PORT", "6379"))
GRAPH_NAME      = "joycity_kg"
CLAUDE_MODEL    = "claude-sonnet-4-6@default"

# ─── 골든셋 ───────────────────────────────────────────────────────────────────
GOLDEN_SET = [
    # 카테고리 1: 담당자
    {"id": "Q01", "category": "담당자", "difficulty": "easy",
     "question": "점검 시작과 서버 오픈 단계는 어느 팀이 담당하나요?",
     "answer": "운영팀"},
    {"id": "Q02", "category": "담당자", "difficulty": "easy",
     "question": "에러코드 198이 발생했을 때 확인을 요청해야 하는 담당자는 누구인가요?",
     "answer": "정보시스템팀 안제민"},
    {"id": "Q03", "category": "담당자", "difficulty": "medium",
     "question": "RESU 라이브 + PM 이슈 전달의 담당자는 누구누구인가요?",
     "answer": "김도형, 허현철, 고명수 / 김정빈, 신동화"},
    {"id": "Q04", "category": "담당자", "difficulty": "medium",
     "question": "iOS 빌드 관련 채널이 없을 때 데브옵스팀에서 문의할 수 있는 담당자는 누구인가요?",
     "answer": "임재욱"},
    {"id": "Q05", "category": "담당자", "difficulty": "medium",
     "question": "애플 앱스토어 iOS 내부테스터를 등록할 때 애니플렉스 측에 요청을 전달하는 담당자는 누구인가요?",
     "answer": "김원태"},
    # 카테고리 2: 정책/규정
    {"id": "Q06", "category": "정책/규정", "difficulty": "easy",
     "question": "점검 소요 시간 확인은 점검 당일 기준 언제까지 완료해야 하나요?",
     "answer": "점검 전날 15시까지"},
    {"id": "Q07", "category": "정책/규정", "difficulty": "medium",
     "question": "iOS 버전 표기에서 괄호 안의 숫자(예: 1.9.1(10)에서 10)는 무엇을 의미하나요?",
     "answer": "번들버전(Bundle Version)"},
    {"id": "Q08", "category": "정책/규정", "difficulty": "easy",
     "question": "QA 빌드 후 접속까지 소요되는 시간은 얼마로 안내하나요?",
     "answer": "약 30분"},
    {"id": "Q09", "category": "정책/규정", "difficulty": "medium",
     "question": "QA 빌드 공유 시 공유해야 하는 빌드 항목은 어떻게 구성되나요?",
     "answer": "안드로이드 링크 2개(애니플렉스, 조이시티) + iOS 버전 2개(애니플렉스, 조이시티)를 QA방(+DQA방)에 공유"},
    # 카테고리 3: 관계
    {"id": "Q10", "category": "관계", "difficulty": "medium",
     "question": "FDE1팀과 FDE2팀의 기반 조직과 담당 리더는 각각 누구인가요?",
     "answer": "FDE1팀: 데이터사이언스실 기반, 리더 정민호 / FDE2팀: 플랫폼실 기반, 리더 김주철"},
    {"id": "Q11", "category": "관계", "difficulty": "medium",
     "question": "온톨로지, 디지털 트윈, End-to-End 도구는 각각 어떤 역할로 설명되나요?",
     "answer": "온톨로지(규칙) → 디지털 트윈(엔진) → End-to-End 도구(화면)"},
    {"id": "Q12", "category": "관계", "difficulty": "easy",
     "question": "FDE 활동 기여도는 무엇에 반영되나요?",
     "answer": "인사 평가 (GIVE + TAKE 두 기준으로 반영)"},
    {"id": "Q13", "category": "관계", "difficulty": "medium",
     "question": "FDE 파견이 종료되면 도구와 지식은 각각 어디에 남나요?",
     "answer": "도구는 해당 팀에, 지식(온톨로지)은 전사 온톨로지에 남음"},
    {"id": "Q14", "category": "관계", "difficulty": "medium",
     "question": "AppGuard Upload/Download Timeout 에러가 지속 발생할 경우 어떻게 해야 하나요?",
     "answer": "시간을 두고 재실행하고, 지속 발생 시 라이브팀에 공유"},
    # 카테고리 4: 문서위치
    {"id": "Q15", "category": "문서위치", "difficulty": "easy",
     "question": "iOS 버전 관리 시트는 어디서 확인할 수 있나요?",
     "answer": "https://www.notion.so/joycity/2e6ea67a5681804997f6e69195b4c008"},
    {"id": "Q16", "category": "문서위치", "difficulty": "medium",
     "question": "버전 표기 규칙 문서의 파일명은 무엇인가요?",
     "answer": "STRAT-버전 표기 규칙-101125-144421.pdf"},
    {"id": "Q17", "category": "문서위치", "difficulty": "hard",
     "question": "pLTV D3D5 관련 도커 이미지는 어느 GCP 레포지토리 경로에 업로드되나요?",
     "answer": "https://console.cloud.google.com/artifacts/docker/data-science-division-216308/us-west1/pltv-preprocessor-repo/pltv-uid-d3d5-model"},
    # 카테고리 5: 복합
    {"id": "Q18", "category": "복합", "difficulty": "hard",
     "question": "빌드 중 에러코드 138과 198이 발생했을 때 각각의 대응 방법은 무엇인가요?",
     "answer": "에러코드 138: 재빌드 / 에러코드 198: 정보시스템팀 안제민 확인 (급한 경우 빌드머신 재부팅 또는 sudo pkill -f Unity.Licensing.Client)"},
    {"id": "Q19", "category": "복합", "difficulty": "medium",
     "question": "점검 진행 중 서버 상태 확인에 사용하는 도구는 무엇이 있나요?",
     "answer": "Grafana, Kibana, OpenSearch 대시보드 (서버 상태 확인 단계에서 사용)"},
    {"id": "Q20", "category": "복합", "difficulty": "hard",
     "question": "IN-JOY가 '왜 매출이 떨어졌나'에 답하지 못하는 이유는 무엇이고, FDE는 이를 어떻게 해결하려 하나요?",
     "answer": "IN-JOY는 End-to-End 도구(화면)만 먼저 만들었으나 온톨로지(규칙)와 디지털 트윈(엔진)이 비어 있어 분석 불가. FDE의 TAKE 모델로 파견 중 수집한 업무 지식을 온톨로지에 축적하여 AI 분석 기반을 구축하는 것이 해결 방향."},
]

# ─── 클라이언트 초기화 ────────────────────────────────────────────────────────
def init_clients():
    from google import genai
    from qdrant_client import QdrantClient
    import falkordb as fdb
    from anthropic import AnthropicVertex

    embed_client = genai.Client(project=GCP_PROJECT, location=LOCATION, vertexai=True)
    qdrant = QdrantClient(url=QDRANT_URL)
    db = fdb.FalkorDB(host=FALKORDB_HOST, port=FALKORDB_PORT)
    graph = db.select_graph(GRAPH_NAME)
    claude = AnthropicVertex(project_id=GCP_PROJECT, region=LOCATION)

    return embed_client, qdrant, graph, claude


def embed(client, text: str) -> list:
    result = client.models.embed_content(model=EMBED_MODEL, contents=[text[:2000]])
    return result.embeddings[0].values


# ─── 검색 함수 ────────────────────────────────────────────────────────────────
def semantic_search(embed_client, qdrant, query: str, limit: int = 5) -> list:
    vec = embed(embed_client, query)
    result = qdrant.query_points(
        collection_name=COLLECTION_NAME,
        query=vec,
        limit=limit,
        with_payload=True,
    )
    out = []
    for h in result.points:
        p = h.payload or {}
        out.append({
            "title":   p.get("title", ""),
            "text":    p.get("text", "")[:2000],
            "score":   round(h.score, 4),
            "url":     p.get("source_url", ""),
        })
    return out


def graph_search(graph, entity: str) -> list:
    """엔티티와 연결된 모든 관계 탐색"""
    relations = []
    # 이름으로 노드 찾기 (부분 일치)
    words = [w for w in entity.split() if len(w) >= 2][:3]
    seen = set()
    for word in words:
        q = ("MATCH (n)-[r:REL]->(m) WHERE n.name CONTAINS $w OR m.name CONTAINS $w "
             "RETURN n.name, r.rel_name, m.name, r.condition LIMIT 10")
        try:
            res = graph.query(q, {"w": word})
            for row in res.result_set:
                key = (row[0], row[1], row[2])
                if key not in seen:
                    seen.add(key)
                    relations.append({
                        "subject":   row[0],
                        "predicate": row[1],
                        "object":    row[2],
                        "condition": row[3] if len(row) > 3 else "",
                    })
        except Exception:
            pass
    return relations


def hybrid_search(embed_client, qdrant, graph, query: str) -> dict:
    sem = semantic_search(embed_client, qdrant, query, limit=5)
    grp = graph_search(graph, query)

    # 관계 요약 텍스트 생성
    graph_text = ""
    for r in grp[:10]:
        cond = f" (조건: {r['condition']})" if r.get("condition") else ""
        graph_text += f"- {r['subject']} →[{r['predicate']}]→ {r['object']}{cond}\n"

    # 벡터 검색 텍스트 합치기
    vector_text = ""
    for s in sem:
        vector_text += f"[{s['title']}]\n{s['text']}\n\n"

    return {
        "semantic": sem,
        "graph": grp,
        "combined_context": (
            "=== 그래프 관계 ===\n" + graph_text +
            "\n=== 관련 문서 ===\n" + vector_text
        ).strip()
    }


# ─── Claude 채점 ──────────────────────────────────────────────────────────────
SCORE_PROMPT = """당신은 검색 시스템 평가자입니다.

질문: {question}
정답: {answer}
검색 결과에서 생성된 응답: {response}

위 응답이 정답을 얼마나 잘 포함하고 있는지 채점하세요.

채점 기준:
- 1.0: 정답의 핵심 정보를 완전히 포함
- 0.5: 정답의 핵심 정보를 부분적으로 포함 (일부 누락 또는 부정확)
- 0.0: 정답과 관련 없거나 완전히 틀림

반드시 아래 JSON 형식으로만 답변하세요 (다른 텍스트 없이):
{{"score": 0.0, "reason": "한 줄 이유"}}"""


def score_with_claude(claude, question: str, answer: str, response: str) -> dict:
    prompt = SCORE_PROMPT.format(
        question=question,
        answer=answer,
        response=response[:1000],
    )
    try:
        msg = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        # JSON 파싱
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(text[start:end])
    except Exception as e:
        print(f"    ⚠️  채점 오류: {e}")
    return {"score": 0.0, "reason": "채점 실패"}


def generate_response(context: str, question: str, claude) -> str:
    """검색 결과를 바탕으로 답변 생성"""
    prompt = f"""아래 컨텍스트를 바탕으로 질문에 답하세요. 컨텍스트에 없는 내용은 답하지 마세요.

컨텍스트:
{context[:5000]}

질문: {question}

답변 (간결하게):"""
    try:
        msg = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        return f"응답 생성 실패: {e}"


# ─── 평가 실행 ────────────────────────────────────────────────────────────────
def run_evaluation():
    print("=" * 60)
    print("  Semantica 골든셋 평가 — Week 4")
    print("=" * 60)
    print(f"  시작: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  총 질문: {len(GOLDEN_SET)}개")
    print()

    print("🔌 클라이언트 초기화 중...")
    embed_client, qdrant, graph, claude = init_clients()
    print("✅ 완료\n")

    results = []
    category_scores = {}

    for i, item in enumerate(GOLDEN_SET, 1):
        qid = item["id"]
        cat = item["category"]
        diff = item["difficulty"]
        question = item["question"]
        answer = item["answer"]

        print(f"[{i:02d}/20] {qid} ({cat} / {diff})")
        print(f"  Q: {question}")

        # 1. 하이브리드 검색
        t0 = time.time()
        try:
            search_result = hybrid_search(embed_client, qdrant, graph, question)
            context = search_result["combined_context"]
            sem_count = len(search_result["semantic"])
            grp_count = len(search_result["graph"])
        except Exception as e:
            print(f"  ❌ 검색 오류: {e}")
            results.append({**item, "score": 0.0, "reason": f"검색 실패: {e}",
                             "response": "", "search_time": 0})
            continue

        search_time = round(time.time() - t0, 2)

        # 2. 응답 생성
        response = generate_response(context, question, claude)
        print(f"  A: {response[:100]}{'...' if len(response) > 100 else ''}")

        # 3. Claude 채점
        scored = score_with_claude(claude, question, answer, response)
        score = scored.get("score", 0.0)
        reason = scored.get("reason", "")

        score_icon = "✅" if score >= 0.8 else ("⚡" if score >= 0.4 else "❌")
        print(f"  {score_icon} 점수: {score:.1f} | {reason}")
        print(f"     검색: 벡터 {sem_count}건 + 그래프 {grp_count}건 ({search_time}s)")
        print()

        row = {
            **item,
            "score": score,
            "reason": reason,
            "response": response,
            "search_time": search_time,
            "sem_count": sem_count,
            "grp_count": grp_count,
        }
        results.append(row)

        if cat not in category_scores:
            category_scores[cat] = []
        category_scores[cat].append(score)

        time.sleep(0.5)  # API 요청 간격

    # ─── 결과 집계 ────────────────────────────────────────────────────────────
    total_score = sum(r["score"] for r in results) / len(results)
    passed = sum(1 for r in results if r["score"] >= 0.7)

    print("=" * 60)
    print("  📊 평가 결과 요약")
    print("=" * 60)
    print(f"  전체 평균:  {total_score:.3f} ({'✅ 목표 달성' if total_score >= 0.7 else '❌ 목표 미달'}, 목표 0.70)")
    print(f"  통과 (≥0.7): {passed}/20문항")
    print()
    print("  카테고리별:")
    for cat, scores in category_scores.items():
        avg = sum(scores) / len(scores)
        bar = "█" * int(avg * 10) + "░" * (10 - int(avg * 10))
        print(f"    {cat:<10} {bar} {avg:.2f}  ({len(scores)}문항)")
    print()

    # 난이도별
    for diff in ["easy", "medium", "hard"]:
        d_scores = [r["score"] for r in results if r["difficulty"] == diff]
        if d_scores:
            avg = sum(d_scores) / len(d_scores)
            print(f"  {diff:<8}: {avg:.2f} ({len(d_scores)}문항)")

    # ─── 결과 저장 ────────────────────────────────────────────────────────────
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = ROOT / "data" / "eval"
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / f"eval_result_{ts}.json"
    json_path.write_text(
        json.dumps({"timestamp": ts, "total_score": round(total_score, 4),
                    "passed": passed, "category_scores": {
                        c: round(sum(s)/len(s), 4) for c, s in category_scores.items()
                    }, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    # Markdown 리포트
    md_lines = [
        f"# Semantica 골든셋 평가 결과\n",
        f"- **평가일시**: {ts}",
        f"- **전체 평균**: {total_score:.3f} ({'✅ 목표 달성' if total_score >= 0.7 else '❌ 목표 미달'})",
        f"- **통과 문항**: {passed}/20\n",
        "## 카테고리별 점수\n",
        "| 카테고리 | 평균 점수 | 문항 수 |",
        "|---------|---------|--------|",
    ]
    for cat, scores in category_scores.items():
        avg = sum(scores) / len(scores)
        md_lines.append(f"| {cat} | {avg:.2f} | {len(scores)} |")

    md_lines += ["\n## 문항별 결과\n",
                 "| ID | 카테고리 | 난이도 | 점수 | 이유 |",
                 "|----|---------|-------|------|-----|"]
    for r in results:
        icon = "✅" if r["score"] >= 0.8 else ("⚡" if r["score"] >= 0.4 else "❌")
        md_lines.append(
            f"| {r['id']} | {r['category']} | {r['difficulty']} "
            f"| {icon} {r['score']:.1f} | {r.get('reason','')[:40]} |"
        )

    md_path = out_dir / f"eval_report_{ts}.md"
    md_path.write_text("\n".join(md_lines), encoding="utf-8")

    print(f"\n  💾 결과 저장:")
    print(f"     JSON: {json_path}")
    print(f"     MD:   {md_path}")
    print()

    return total_score


if __name__ == "__main__":
    score = run_evaluation()
    sys.exit(0 if score >= 0.7 else 1)
