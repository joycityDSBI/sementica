"""
Week 1 검증 스크립트 — Vertex AI (Claude Sonnet 4.6) 기반 트리플 추출 테스트

Premise 3 합격 기준:
  10페이지 샘플 중 6페이지 이상에서 3개 이상 엔티티-관계-엔티티 트리플 자동 추출

환경변수 (.env 파일):
  GOOGLE_CLOUD_PROJECT=datahub-478802
  VERTEX_AI_LOCATION=us-east5          # Claude 지원 리전
  VERTEX_AI_MODEL=claude-sonnet-4-6@20250514
  GOOGLE_APPLICATION_CREDENTIALS=C:\\sementica\\service-account-key.json

사용법:
  python week1_verify.py                       # data/notion_samples/*.md 전부 검증
  python week1_verify.py --file <page.md>      # 단일 파일 검증
  python week1_verify.py --text "박지수는 HR팀..."  # 텍스트 직접 테스트
"""

import argparse
import json
import os
import sys
from pathlib import Path

# .env 로드
_env_path = Path(__file__).parent.parent.parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

SAMPLES_DIR = Path(__file__).parent.parent.parent / "data" / "notion_samples"
LOGS_DIR    = Path(__file__).parent.parent.parent / "data" / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

GCP_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
LOCATION    = os.environ.get("VERTEX_AI_LOCATION", "us-east5")
MODEL       = os.environ.get("VERTEX_AI_MODEL", "claude-sonnet-4-6@20250514")

# ─── AnthropicVertex 클라이언트 초기화 ───────────────────────────────────────
_client = None

def _init_client():
    global _client
    if _client:
        return True
    if not GCP_PROJECT:
        print("⚠️  GOOGLE_CLOUD_PROJECT 환경변수가 없습니다. .env 파일을 확인하세요.")
        return False
    try:
        from anthropic import AnthropicVertex
        _client = AnthropicVertex(project_id=GCP_PROJECT, region=LOCATION)
        print(f"✅ Claude on Vertex AI 초기화 완료")
        print(f"   프로젝트: {GCP_PROJECT} | 모델: {MODEL} | 리전: {LOCATION}")
        return True
    except Exception as e:
        print(f"❌ AnthropicVertex 초기화 실패: {e}")
        print("   확인 사항:")
        print("   1. GOOGLE_APPLICATION_CREDENTIALS 파일 경로 정확 여부")
        print("   2. 서비스 계정에 Vertex AI User 역할 부여 여부")
        print("   3. Vertex AI Model Garden에서 Claude 모델 사용 동의 여부")
        print(f"      → https://console.cloud.google.com/vertex-ai/model-garden?project={GCP_PROJECT}")
        return False


# ─── LLM 트리플 추출 프롬프트 (타입 + 속성 포함 프로퍼티 그래프) ─────────────
EXTRACT_PROMPT = """\
다음 텍스트에서 엔티티-관계-엔티티 트리플을 추출하세요.
담당자, 팀, 업무, 정책, 프로젝트, 시스템 간의 명시적 관계를 추출합니다.

각 트리플은 아래 형식으로 추출하세요:
- subject / object: name(이름)과 type(엔티티 종류)을 포함
- predicate: name(관계 동사), 그리고 알 수 있다면 condition(조건), order(순서, 정수), duration(소요시간)

엔티티 type 예시: Person(사람), Team(팀), Process(프로세스/업무), System(시스템), Policy(정책/규정), Document(문서), Role(역할)
관계 name 예시: 담당, 소속, 승인, 운영, 참여, 협업, 보고, 관리, 포함, 사용

텍스트:
{text}

JSON 배열로만 응답하세요 (설명 없이):
[
  {{
    "subject":   {{"name": "엔티티A", "type": "Team"}},
    "predicate": {{"name": "담당", "condition": "점검일 한정", "order": 1}},
    "object":    {{"name": "엔티티B", "type": "Process", "duration": "30분"}}
  }},
  ...
]

조건/순서/소요시간이 없으면 해당 키를 생략하세요.
트리플이 없으면 빈 배열 [] 반환."""


def _normalize_node(val) -> dict:
    """subject/object를 {name, type} dict로 정규화 (구형 문자열도 수용)"""
    if isinstance(val, dict):
        return {
            "name": str(val.get("name", "")),
            "type": str(val.get("type", "Unknown")),
        }
    return {"name": str(val), "type": "Unknown"}


def _normalize_pred(val) -> dict:
    """predicate를 {name, ...속성} dict로 정규화 (구형 문자열도 수용)"""
    if isinstance(val, dict):
        pred = {"name": str(val.get("name", ""))}
        if "condition" in val:
            pred["condition"] = str(val["condition"])
        if "order" in val:
            try:
                pred["order"] = int(val["order"])
            except (ValueError, TypeError):
                pass
        if "duration" in val:
            pred["duration"] = str(val["duration"])
        return pred
    return {"name": str(val)}


def extract_triplets(text: str, source_url: str = "") -> dict:
    """Claude Sonnet 4.6 (Vertex AI)로 한국어 텍스트에서 타입 있는 트리플 추출"""
    if not _init_client():
        return {"error": "Claude on Vertex AI 미초기화", "triplets": [], "source_url": source_url, "pass": False}

    raw = ""
    try:
        response = _client.messages.create(
            model=MODEL,
            max_tokens=2048,
            messages=[{
                "role": "user",
                "content": EXTRACT_PROMPT.format(text=text[:3000]),
            }],
        )
        raw = response.content[0].text.strip()

        # 코드 블록 제거
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            parsed = []

        result_triplets = []
        for t in parsed:
            if not isinstance(t, dict):
                continue
            result_triplets.append({
                "subject":   _normalize_node(t.get("subject", "")),
                "predicate": _normalize_pred(t.get("predicate", "")),
                "object":    _normalize_node(t.get("object", "")),
            })

        return {
            "triplet_count": len(result_triplets),
            "triplets": result_triplets[:15],   # 더 많이 저장 (타입 정보 가치 있음)
            "source_url": source_url,
            "model": MODEL,
            "pass": len(result_triplets) >= 3,
        }

    except json.JSONDecodeError as e:
        return {
            "error": f"JSON 파싱 실패: {e} | raw={raw[:200]}",
            "triplets": [], "source_url": source_url, "pass": False,
        }
    except Exception as e:
        return {
            "error": str(e),
            "triplets": [], "source_url": source_url, "pass": False,
        }


# ─── 파일 단위 검증 ───────────────────────────────────────────────────────────
def verify_file(md_path: Path) -> dict:
    content = md_path.read_text(encoding="utf-8")

    source_url = ""
    body = content
    if content.startswith("---"):
        end = content.find("---", 3)
        if end > 0:
            for line in content[3:end].splitlines():
                if line.startswith("notion_url:"):
                    source_url = line.split(":", 1)[1].strip()
            body = content[end + 3:].strip()

    word_count = len(body.split())
    print(f"\n📄 {md_path.name}  ({word_count} 단어)")

    if word_count < 50:
        print(f"   ⚠️  텍스트 부족 (< 50단어) — Premise 1 불합격")
        return {
            "file": str(md_path), "word_count": word_count,
            "premise1": False, "pass": False,
        }

    result = extract_triplets(body, source_url)
    result["file"] = str(md_path)
    result["word_count"] = word_count
    result["premise1"] = True

    if result.get("error"):
        print(f"   ❌ 오류: {result['error']}")
    else:
        tc = result.get("triplet_count", 0)
        p3 = result.get("pass", False)
        print(f"   트리플: {tc}개  {'✅ 합격' if p3 else '❌ 불합격'} (기준: 3개 이상)")
        for t in result.get("triplets", [])[:3]:
            s = t["subject"]
            p = t["predicate"]
            o = t["object"]
            s_str = f"{s['name']}({s.get('type','?')})"
            o_str = f"{o['name']}({o.get('type','?')})"
            p_parts = [p["name"]]
            if "condition" in p:
                p_parts.append(f"조건:{p['condition']}")
            if "order" in p:
                p_parts.append(f"순서:{p['order']}")
            if "duration" in p:
                p_parts.append(f"소요:{p['duration']}")
            p_str = "|".join(p_parts)
            print(f"     {s_str} →[{p_str}]→ {o_str}")

    return result


# ─── 메인 ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Week 1 Claude on Vertex AI 트리플 추출 검증")
    parser.add_argument("--file", help="특정 마크다운 파일 경로")
    parser.add_argument("--text", help="텍스트 직접 입력 (빠른 테스트)")
    args = parser.parse_args()

    results = []

    if args.text:
        print(f"\n🔬 텍스트 직접 검증")
        print(f"   입력: {args.text[:120]}")
        result = extract_triplets(args.text, "manual-test")
        results.append(result)
        err = result.get("error", "")
        if err:
            print(f"\n   ❌ 오류: {err}")
        else:
            tc = result.get("triplet_count", 0)
            print(f"\n   추출된 트리플 ({tc}개):")
            for t in result.get("triplets", []):
                s = t["subject"]
                p = t["predicate"]
                o = t["object"]
                s_str = f"{s['name']}({s.get('type','?')})"
                o_str = f"{o['name']}({o.get('type','?')})"
                p_parts = [p["name"]]
                if "condition" in p:
                    p_parts.append(f"조건:{p['condition']}")
                if "order" in p:
                    p_parts.append(f"순서:{p['order']}")
                if "duration" in p:
                    p_parts.append(f"소요:{p['duration']}")
                p_str = "|".join(p_parts)
                print(f"     {s_str} →[{p_str}]→ {o_str}")
            print(f"\n   판정: {'✅ 합격 (3개 이상)' if result.get('pass') else '❌ 불합격'}")

    elif args.file:
        results.append(verify_file(Path(args.file)))

    else:
        md_files = [f for f in SAMPLES_DIR.glob("*.md") if f.name != "README.md"]
        if not md_files:
            print(f"\n⚠️  {SAMPLES_DIR} 에 마크다운 파일이 없습니다.")
            print("   먼저 notion_fetch.py 로 Notion 페이지를 수집하세요:")
            print("   python src\\pipeline\\notion_fetch.py --token <NOTION_TOKEN>")
            sys.exit(1)

        print(f"\n🔬 Week 1 Claude on Vertex AI 검증 — {len(md_files[:10])}개 파일")
        print("=" * 60)
        for f in md_files[:10]:
            results.append(verify_file(f))

    # ─── 합격 판정 (10개 이상 파일 검증 시) ─────────────────────────────────
    total = len(results)
    if total > 1:
        p1 = sum(1 for r in results if r.get("premise1", r.get("word_count", 0) >= 50))
        p3 = sum(1 for r in results if r.get("pass", False))

        print("\n" + "=" * 60)
        print("📊 합격 기준 판정 (10페이지 기준)")
        print(f"   Premise 1 (텍스트 품질): {p1}/{total} ≥ 6  →  {'✅ 합격' if p1 >= 6 else '❌ 불합격'}")
        print(f"   Premise 3 (트리플 추출): {p3}/{total} ≥ 6  →  {'✅ 합격' if p3 >= 6 else '❌ 불합격'}")
        overall = p1 >= 6 and p3 >= 6
        print(f"\n   전체: {'✅ Phase 1 계속 진행' if overall else '❌ 전환 경로 확인 필요'}")
        if not overall:
            if p1 < 6:
                print("   → 구조화된 Notion DB 페이지 우선 수집으로 전환")
            if p3 < 6:
                print("   → 수동 스키마 설계 경로로 전환 (Phase 1에 1주 추가)")

    # 로그 저장
    log_path = LOGS_DIR / "week1_verification.json"
    log_path.write_text(
        json.dumps({"results": results, "model": MODEL}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n   결과 저장: {log_path}")


if __name__ == "__main__":
    main()
