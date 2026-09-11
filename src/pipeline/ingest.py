"""
Semantica 인제스천 파이프라인 — Week 2
Notion 샘플 페이지 → Qdrant(벡터) + FalkorDB(그래프) 저장

파이프라인:
  .md 파일 → 텍스트 파싱
    → 임베딩 (Vertex AI text-multilingual-embedding-002) → Qdrant 저장
    → 타입 트리플 추출 (Claude Sonnet 4.6 on Vertex AI) → FalkorDB 저장

사전 조건:
  - Docker Desktop 실행 중
  - docker-compose up -d (C:\\sementica\\docker-compose.yml)
  - .env 파일 설정 완료

사용법:
  python src\\pipeline\\ingest.py              # 전체 샘플 인제스천
  python src\\pipeline\\ingest.py --dry-run    # 연결 확인만 (저장 안 함)
  python src\\pipeline\\ingest.py --reset      # 기존 데이터 삭제 후 재인제스천
"""

import argparse
import contextlib
import json
import os
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
# src/ — utils 패키지(synonym_resolver·korean·datespan) import 에 필요.
# 누락 시 동의어 정규화와 이벤트 주체 판정이 조용히 비활성화됩니다.
sys.path.insert(0, str(Path(__file__).parent.parent))
try:
    from db_logger import upsert_notion_page as _upsert_notion_page
except Exception:

    def _upsert_notion_page(*a, **kw):
        pass  # PostgreSQL 없으면 no-op


from semantica_helper import (
    _warn_if_output_truncated,
    classify_page,
    content_hash,
    detect_realization_status,
    ensure_indexes,
    event_from_db_props,
    extract_with_fallback,
    find_evidence_chunk_id,
    format_scope_report,
    is_decision_triplet,
    merge_node,
    record_decision_node,
    reset_scope_report,
    text_windows,
    upsert_event_node,
)
from utils.llm import create_message

# ─── .env 로드 ────────────────────────────────────────────────────────────────
_env_path = Path(__file__).parent.parent.parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# ─── 경로 설정 ────────────────────────────────────────────────────────────────
ROOT_DIR = Path(__file__).parent.parent.parent
LOGS_DIR = ROOT_DIR / "data" / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# 하위 호환: --dept 없을 때 기존 notion_samples 사용
_LEGACY_SAMPLES_DIR = ROOT_DIR / "data" / "notion_samples"

# ─── Qdrant / FalkorDB 설정 (--dept 로 덮어씀) ───────────────────────────────
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
FALKORDB_HOST = os.environ.get("FALKORDB_HOST", "localhost")
FALKORDB_PORT = int(os.environ.get("FALKORDB_PORT", "6379"))
COLLECTION_NAME = "joycity_pages"  # --dept 없을 때 기본값
GRAPH_NAME = "joycity_kg"  # --dept 없을 때 기본값
# 용어집 진단 메시지용 기본 URL (synonym_resolver 와 동일 값)
GLOSSARY_DEFAULT = "https://catalog.joycityplay.com/api/glossary/all"
# Vertex AI 다국어 임베딩 (한국어 지원, 768차원)
EMBED_MODEL_NAME = "text-multilingual-embedding-002"
EMBED_DIM = 768
EMBED_BATCH_SIZE = 50  # Vertex AI 배치 최대 권장 크기 (최대 250, 안전 수치 50)

# ─── Vertex AI / LLM 설정 ────────────────────────────────────────────────────
GCP_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
LOCATION = os.environ.get("VERTEX_AI_LOCATION", "us-east5")  # 임베딩 리전
ANTHROPIC_REGION = os.environ.get("ANTHROPIC_VERTEX_REGION", "global")  # Claude LLM 리전
MODEL = os.environ.get("VERTEX_AI_MODEL", "claude-sonnet-4-6@default")
# 트리플/이벤트 추출: Haiku 사용 (Sonnet 대비 3~5배 빠름, 추출 품질 충분)
HAIKU_MODEL = "claude-haiku-4-5@20251001"

# 추출·판정 계열 LLM 호출의 온도. 추출은 창작이 아니라 파싱이므로 0 입니다.
#
# 전달 경로: anthropic 1.x 에는 temperature 명명 인자가 없어 utils.llm 이
# extra_body 로 넘깁니다. 적용 여부는 tools/probe_llm.py 로 확인했고, 짧은
# 생성에서 온도 0 은 2회 동일 / 1.0 은 변동으로 **정상 작동이 확인**됐습니다.
#
# ※ 그래도 트리플 추출은 재현되지 않습니다. 실측(10페이지 2회):
#   **일치율 45.9%**. 긴 생성(2048토큰)에서는 서빙 계층 비결정성이 누적되고,
#   차이는 관계 발견이 아니라 **엔티티 이름 선택**에서 납니다:
#       1회차: IN_JOY_MOBILE →[제공]→ In-Joy
#       2회차: IN_JOY_MOBILE →[제공]→ 모바일 프로젝트
#   같은 사실을 다르게 부르는 것이라 온도로는 해결되지 않습니다. 해결은
#   추출 이후 단계 — merge_node 의 엔티티 정규화 범위를 넓히는 쪽입니다.
#   온도 0 은 유지합니다(편차를 줄이고 비용이 없음). 다만 "이것으로 그래프가
#   재현된다"고 기대하면 안 됩니다.
EXTRACT_TEMPERATURE = float(os.environ.get("EXTRACT_TEMPERATURE", "0"))

# ─── 트리플 추출 프롬프트 ────────────────────────────────────────────────────
EXTRACT_PROMPT = """\
다음 텍스트에서 엔티티-관계-엔티티 트리플을 추출하세요.

━━ 1. 엔티티 type (아래 8가지만 사용, 그 외 타입 금지) ━━━━━━━━━━━━━━
  Person   — 실명이 있는 사람. 예: 홍길동, 김철수 팀장
             ※ "담당자", "관리자" 처럼 역할어만 있으면 Role로 분류
  Team     — 팀·본부·실·센터·부문 등 조직 단위.
             예: 전략사업본부, DI팀, 마케팅실
             ※ "조이시티"처럼 회사 전체는 Team 아님 → 생략
  System   — IT 시스템, 플랫폼, DB, 툴, API.
             예: BigQuery, Slack, 인사시스템, MMP
  Process  — 반복 수행되는 업무 절차·프로세스.
             예: 정산 프로세스, 데이터 적재 파이프라인
  Policy   — 정책, 규정, 기준, 지침.
             예: 개인정보처리방침, 결재 기준
  Document — 보고서, 문서, 양식, 기획서.
             예: 주간보고서, UA 전략 문서
  Role     — 직책·역할어 (이름 없이 역할만).
             예: 팀장, 담당자, 승인권자, PO
  Decision — 명시적으로 결정·확정된 사항.
             예: 예산 승인, 런칭 결정

━━ 2. 엔티티 이름 표기 규칙 (중복 방지 핵심) ━━━━━━━━━━━━━━━━━━━━━
  ① 문서에 나온 표기를 그대로 사용 (번역·변형 금지)
     올바름: "빅쿼리" (문서에 이렇게 표기)  금지: "BigQuery"로 바꾸지 말 것
  ② 조직명은 약칭보다 공식 명칭 우선 — 단, 공식 명칭을 확실히 알 때만 변환할 것
     올바름: "전략사업본부"  금지: "전략본부", "전략사업부"로 변형
     ※ 중요: 약칭의 원형을 모른다면 반드시 약칭 그대로 사용할 것. 추측·추론으로 공식 명칭을 만들지 말 것.
        예) "데사실"의 원형을 모른다면 → "데사실" 그대로 사용 (절대 "데이터전략실" 등으로 변환 금지)
  ③ 사람 이름은 성+이름 전체 사용
     올바름: "홍길동"  금지: "홍씨", "길동"
  ④ 시스템명은 고유명사 그대로 (대소문자, 영문 유지)
     올바름: "BigQuery", "Slack"  금지: "빅쿼리"로 번역
  ⑤ 동일 개념이 여러 표기로 등장하면 → 첫 등장 표기 기준으로 통일

━━ 3. 관계명 (아래 목록에서만 선택) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  조직 관계: 소속, 관리, 보고, 협업
  업무 관계: 담당, 운영, 요청, 승인, 검토
  시스템 관계: 연동, 적재, 활용, 생성, 분석, 참조
  문서 관계: 작성, 포함, 정의
  ※ 목록에 없는 관계는 가장 가까운 것으로 대체. 임의 신조어 금지.

━━ 4. 추출하지 않는 것 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ✗ 날짜·시간 (2026-01-01, 오전 9시)
  ✗ 숫자·통계 (100만원, 30%, 3회)
  ✗ 컬럼명·필드명 (user_id, created_at)
  ✗ 일반 동사·형용사 (진행, 완료, 중요)
  ✗ 회사 전체 이름 ("조이시티" 단독 엔티티)
  ✗ 의미 없는 단어 (것, 내용, 사항, 경우)

━━ 5. evidence_quote (필수) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  각 트리플에는 반드시 원문에서 그대로 인용한 문구를 포함하세요.
  • 원문 텍스트에 실제로 있는 문장·구절을 그대로 복사 (번역·요약 금지)
  • 트리플 근거가 될 문구가 없으면 해당 트리플 전체를 제외
  • evidence_quote 없는 트리플은 환각(hallucination)으로 간주해 필터링됩니다.

━━ 6. 추출 예시 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  입력: "DI팀 유현상이 마케팅실 요청으로 BigQuery에 일별 유저 데이터를 적재한다."
  출력:
  [
    {{"subject": {{"name": "유현상", "type": "Person"}},
      "predicate": {{"name": "소속"}},
      "object":   {{"name": "DI팀", "type": "Team"}},
      "evidence_quote": "DI팀 유현상이"}},
    {{"subject": {{"name": "마케팅실", "type": "Team"}},
      "predicate": {{"name": "요청"}},
      "object":   {{"name": "유현상", "type": "Person"}},
      "evidence_quote": "마케팅실 요청으로"}},
    {{"subject": {{"name": "유현상", "type": "Person"}},
      "predicate": {{"name": "적재"}},
      "object":   {{"name": "BigQuery", "type": "System"}},
      "evidence_quote": "BigQuery에 일별 유저 데이터를 적재한다"}}
  ]

━━ 7. 텍스트 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{text}

JSON 배열로만 응답 (설명·마크다운 없이):
[
  {{
    "subject":        {{"name": "이름", "type": "타입"}},
    "predicate":      {{"name": "관계명"}},
    "object":         {{"name": "이름", "type": "타입"}},
    "evidence_quote": "원문에서 그대로 인용한 문구 (필수)"
  }}
]
트리플이 없으면 [] 반환."""

# ─── 이벤트 추출 프롬프트 ────────────────────────────────────────────────────
EVENT_EXTRACT_PROMPT = """\
다음 텍스트에서 게임/서비스의 이벤트·업데이트를 추출하세요.
날짜가 명시된 항목만 추출합니다. 날짜 형식: YYYY-MM-DD 또는 YY-MM-DD.

이벤트 유형 (event_type):
  client_update   — 클라이언트 패치·업데이트
  server_update   — 서버 점검·배포
  user_event      — 신규·복귀·기간한정 유저 이벤트
  season          — 시즌 개막·종료
  content_release — 신규 콘텐츠 오픈
  maintenance     — 정기 점검
  incident        — 장애 발생·복구
  kpi_milestone   — DAU·매출·ROAS 마일스톤 달성
  ua_budget       — UA 매체 예산 변경 (증액·감액·중단)
  ua_creative     — UA 소재(크리에이티브) 교체·추가·중단
  ua_channel      — UA 매체·채널 추가·제거·전략 변경
  ua_targeting    — UA 타겟·오디언스 세그먼트 변경
  ua_abtest       — UA A/B 테스트 시작·종료·결과 적용

텍스트:
{text}

이벤트가 있으면 JSON 배열, 없으면 [] 로만 응답하세요:
[
  {{
    "game":        "게임명",
    "event_type":  "client_update",
    "date":        "YYYY-MM-DD",
    "title":       "이벤트 제목",
    "description": "상세 설명 (없으면 빈 문자열)",
    "target":      "신규유저,복귀유저 (해당 없으면 빈 문자열)",
    "manager":     "담당자 또는 팀 이름 (모르면 빈 문자열)"
  }}
]"""


# ─── 스레드 안전 잠금 ────────────────────────────────────────────────────────
_qdrant_lock = threading.Lock()  # Qdrant 동시 쓰기 보호
_falkordb_lock = threading.Lock()  # FalkorDB 동시 쓰기 보호

# ─── 클라이언트 초기화 ────────────────────────────────────────────────────────
_llm_client = None
_embed_model = None
_qdrant_store = None
_falkordb = None


def init_llm():
    global _llm_client
    if _llm_client:
        return True
    try:
        from anthropic import AnthropicVertex

        _llm_client = AnthropicVertex(project_id=GCP_PROJECT, region=ANTHROPIC_REGION)
        print(f"  ✅ Claude on Vertex AI — {MODEL}")
        return True
    except Exception as e:
        print(f"  ❌ Claude 초기화 실패: {e}")
        return False


def init_embed():
    global _embed_model
    if _embed_model:
        return True
    try:
        from google import genai

        client = genai.Client(project=GCP_PROJECT, location=LOCATION, vertexai=True)
        _embed_model = client
        print(f"  ✅ Vertex AI 임베딩 초기화 — {EMBED_MODEL_NAME} (dim={EMBED_DIM})")
        return True
    except Exception as e:
        print(f"  ❌ Vertex AI 임베딩 초기화 실패: {e}")
        return False


def init_qdrant(reset: bool = False):
    global _qdrant_store
    try:
        from semantica.vector_store.qdrant_store import QdrantStore

        store = QdrantStore(url=QDRANT_URL)
        store.connect()

        if reset:
            try:
                # 기존 컬렉션 삭제 (reset 모드)
                from qdrant_client import QdrantClient

                qc = QdrantClient(url=QDRANT_URL)
                if COLLECTION_NAME in [c.name for c in qc.get_collections().collections]:
                    qc.delete_collection(COLLECTION_NAME)
                    print(f"  🗑️  Qdrant 컬렉션 삭제: {COLLECTION_NAME}")
            except Exception:
                pass

        try:
            store.create_collection(COLLECTION_NAME, vector_size=EMBED_DIM, distance="Cosine")
            print(f"  ✅ Qdrant 컬렉션 생성: {COLLECTION_NAME}")
        except Exception as ce:
            if "already exists" in str(ce).lower() or "409" in str(ce):
                # 기존 컬렉션 재사용 — 내부 상태 초기화를 위해 get_collection 호출
                store.get_collection(COLLECTION_NAME)
                print(f"  ✅ Qdrant 컬렉션 기존 사용: {COLLECTION_NAME}")
            else:
                raise
        _qdrant_store = store
        print(f"  ✅ Qdrant 연결 완료 — 컬렉션: {COLLECTION_NAME}")
        return True
    except Exception as e:
        print(f"  ❌ Qdrant 연결 실패: {e}")
        print("     → Docker Desktop 실행 후 'docker-compose up -d' 확인")
        return False


def init_falkordb(reset: bool = False):
    global _falkordb
    try:
        import falkordb as _fdb_lib

        db = _fdb_lib.FalkorDB(host=FALKORDB_HOST, port=FALKORDB_PORT)

        if reset:
            try:
                db.select_graph(GRAPH_NAME).delete()
                print(f"  🗑️  FalkorDB 그래프 삭제: {GRAPH_NAME}")
            except Exception as _del_err:
                # 그래프가 존재하지 않으면 정상 (첫 실행), 그 외 오류는 경고 출력
                _msg = str(_del_err).lower()
                if "no such graph" not in _msg and "unknown graph" not in _msg:
                    print(f"  ⚠️  FalkorDB 그래프 삭제 실패 (무시): {_del_err}")

        _falkordb = db.select_graph(GRAPH_NAME)
        print(f"  ✅ FalkorDB 연결 완료 — 그래프: {GRAPH_NAME}")
        # 인덱스는 여기서 만듭니다. --reset 이 그래프를 지우면 인덱스도 함께
        # 사라지는데, 인제스트는 노드마다 name 으로, 이벤트마다 event_id 로
        # MERGE 하므로 인덱스 없이 돌리면 전건 스캔이 쌓여 급격히 느려집니다.
        # (별도 실행하던 scripts/create_indexes.py 와 같은 목록을 씁니다.)
        ensure_indexes(_falkordb)
        return True
    except Exception as e:
        print(f"  ❌ FalkorDB 연결 실패: {e}")
        print("     → Docker Desktop 실행 후 'docker-compose up -d' 확인")
        return False


# ─── 파싱 유틸 ───────────────────────────────────────────────────────────────
def parse_md(path: Path) -> dict:
    """마크다운 파일에서 frontmatter + body 파싱.
    db_properties 줄이 있으면 JSON으로 파싱해 meta에 포함합니다.
    """
    content = path.read_text(encoding="utf-8")
    meta = {
        "title": path.stem,
        "notion_url": "",
        "page_id": "",
        "last_edited_time": "",
        "db_properties": {},
    }
    body = content
    if content.startswith("---"):
        end = content.find("---", 3)
        if end > 0:
            for line in content[3:end].splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    k = k.strip()
                    v = v.strip()
                    if k == "db_properties":
                        with contextlib.suppress(Exception):
                            meta["db_properties"] = json.loads(v)
                    else:
                        meta[k] = v
            body = content[end + 3 :].strip()
    return {"meta": meta, "body": body, "file": str(path)}


# ─── 트리플 추출 ─────────────────────────────────────────────────────────────
def _norm_node(val) -> dict:
    if isinstance(val, dict):
        return {"name": str(val.get("name", "")), "type": str(val.get("type", "Unknown"))}
    return {"name": str(val), "type": "Unknown"}


def _norm_pred(val) -> dict:
    if isinstance(val, dict):
        pred = {"name": str(val.get("name", ""))}
        for k in ("condition", "duration"):
            if k in val:
                pred[k] = str(val[k])
        if "order" in val:
            with contextlib.suppress(ValueError, TypeError):
                pred["order"] = int(val["order"])
        return pred
    return {"name": str(val)}


# DB 속성 키 별칭 — 다양한 한국어/영어 컬럼명을 통일
# event_from_db_props: semantica_helper 에서 import (단일 정의)
# - 대소문자 무관 컬럼명 매칭
# - PROJECT, 변경카테고리, 생성자 등 커스텀 컬럼 지원
# - 변경카테고리 → EVENT_TYPES 정규값 자동 변환


def _events_in_window(window: str) -> list[dict]:
    """창 하나에서 이벤트를 추출합니다."""
    try:
        resp = create_message(
            _llm_client,
            model=HAIKU_MODEL,  # Sonnet → Haiku (3~5배 빠름)
            max_tokens=1024,
            temperature=EXTRACT_TEMPERATURE,
            messages=[{"role": "user", "content": EVENT_EXTRACT_PROMPT.format(text=window)}],
        )
        _warn_if_output_truncated(resp, "이벤트")
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            raw = raw.removeprefix("json")
        raw = raw.strip()
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [e for e in parsed if isinstance(e, dict) and e.get("game") and e.get("date")]
        return []
    except Exception as e:
        # 호출부가 "이벤트 없음"과 구분할 수 있도록 예외를 올립니다.
        print(f"    ⚠️  이벤트 추출 실패: {type(e).__name__}: {e}")
        raise


def extract_events_from_text(text: str) -> list[dict]:
    """본문 전체에서 이벤트를 추출합니다 (긴 문서는 창으로 나눠 합칩니다)."""
    if not _llm_client:
        return []
    seen: set = set()
    out: list[dict] = []
    for window in text_windows(text):
        for ev in _events_in_window(window):
            key = (ev.get("game", ""), ev.get("date", ""), (ev.get("title") or "")[:60])
            if key in seen:
                continue
            seen.add(key)
            out.append(ev)
    return out


def _triplets_in_window(window: str) -> list:
    """창 하나에서 트리플을 추출합니다."""
    raw = ""
    try:
        resp = create_message(
            _llm_client,
            model=HAIKU_MODEL,  # Sonnet → Haiku
            max_tokens=2048,
            temperature=EXTRACT_TEMPERATURE,
            messages=[{"role": "user", "content": EXTRACT_PROMPT.format(text=window)}],
        )
        _warn_if_output_truncated(resp, "트리플")
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            raw = raw.removeprefix("json")
        raw = raw.strip()
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            return []
        result = []
        for t in parsed:
            if not isinstance(t, dict):
                continue
            eq = (t.get("evidence_quote") or "").strip()
            if not eq:
                continue  # evidence_quote 없는 트리플은 환각으로 간주, 제외
            result.append(
                {
                    "subject": _norm_node(t.get("subject", "")),
                    "predicate": _norm_pred(t.get("predicate", "")),
                    "object": _norm_node(t.get("object", "")),
                    "evidence_quote": eq,
                }
            )
        return result
    except Exception as e:
        # extract_with_fallback 이 "error" 로 표시할 수 있도록 예외를 올립니다.
        print(f"    ⚠️  LLM 트리플 추출 실패: {type(e).__name__}: {e}")
        raise


def extract_triplets(text: str) -> list:
    """본문 전체에서 트리플을 추출합니다 (긴 문서는 창으로 나눠 합칩니다).

    창 하나라도 실패하면 예외를 올립니다 — 절반만 담긴 그래프보다
    다음 회차 재시도가 낫습니다 (content_hash 가 비어 있어 재처리됩니다).
    """
    if not _llm_client:
        return []
    seen: set = set()
    out: list = []
    for window in text_windows(text):
        for t in _triplets_in_window(window):
            key = (t["subject"]["name"], t["predicate"]["name"], t["object"]["name"])
            if key in seen:
                continue  # 창 겹침 구간에서 같은 트리플이 두 번 나옵니다
            seen.add(key)
            out.append(t)
    return out


# ─── 청킹 유틸 ───────────────────────────────────────────────────────────────
CHUNK_SIZE = 800  # 청크 크기 (자)
CHUNK_OVERLAP = 200  # 청크 간 겹침 (자)


def _make_chunks(text: str) -> list[str]:
    """텍스트를 CHUNK_SIZE 크기로 CHUNK_OVERLAP 겹침을 두고 분할"""
    chunks = []
    start = 0
    while start < len(text):
        end = start + CHUNK_SIZE
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = end - CHUNK_OVERLAP
    return chunks


# ─── 임베딩 배치 헬퍼 ────────────────────────────────────────────────────────
def _embed_batch(chunks: list[str]) -> list[list[float]]:
    """청크 목록을 EMBED_BATCH_SIZE 단위 배치로 임베딩.
    기존 1개씩 순차 호출 대비 API 호출 횟수를 1/50로 줄입니다.
    """
    all_vecs = []
    for i in range(0, len(chunks), EMBED_BATCH_SIZE):
        batch = chunks[i : i + EMBED_BATCH_SIZE]
        result = _embed_model.models.embed_content(
            model=EMBED_MODEL_NAME,
            contents=batch,
        )
        all_vecs.extend([list(e.values) for e in result.embeddings])
    return all_vecs


# ─── 벡터 저장 (배치 임베딩 + 단일 Qdrant 삽입) ─────────────────────────────
def _with_title(chunk: str, title: str) -> str:
    """임베딩용 텍스트 — 청크 앞에 문서 제목을 붙입니다 (저장되는 원문은 그대로)."""
    title = (title or "").strip()
    return f"{title}\n\n{chunk}" if title else chunk


def store_vector(page: dict) -> int:
    """페이지를 청크로 분할 → 배치 임베딩 → Qdrant 일괄 저장
    개선: 청크당 1회 API 호출 → 페이지당 1회 배치 호출 (최대 50배 빠름)

    Returns:
        저장된 청크 수. 0 은 "저장할 본문이 없음"이라는 뜻이며, 실패는 예외로
        올립니다 — 호출부가 둘을 구분하지 못하면 실패한 페이지에 content_hash
        가 기록되어 다음 동기화에서 영영 건너뛰게 됩니다.
    """
    body = page["body"]
    if not body.strip():
        return 0
    meta = page["meta"]
    base_url = meta.get("notion_url") or page["file"]

    chunks = _make_chunks(body)
    if not chunks:
        return 0

    # 1. 전체 청크 배치 임베딩 (API 호출 최소화)
    #
    # 임베딩에는 **제목을 앞에 붙입니다**. payload 의 text 는 청크 원문 그대로
    # 두므로 근거 대조·전문 조립·인용문 매칭은 영향을 받지 않고, 벡터만 문서
    # 맥락을 얻습니다.
    #
    # 왜: Notion DB 행은 속성 나열 한 줄이라 청크가 100자 안팎입니다. 그 짧은
    # 텍스트만 임베딩하면 긴 질문과 유사도가 낮아 검색에 안 잡힙니다. 실측으로
    # "구글 탑티어2 캠페인 소재 최적화 + TCPA 캡 해제"(102자, 청크 1개)는 두
    # 번의 골든셋에서 연속으로 검색 실패했는데, 정작 질문이 묻는 문구가 제목에
    # 그대로 있었습니다.
    try:
        vecs = _embed_batch([_with_title(c, meta.get("title", "")) for c in chunks])
    except Exception as e:
        print(f"     ⚠️  임베딩 배치 실패: {e}")
        raise

    # 2. 전체 ID·페이로드 구성
    all_ids = []
    all_payloads = []
    for i, chunk in enumerate(chunks):
        all_ids.append(str(uuid.uuid5(uuid.NAMESPACE_URL, f"{base_url}#chunk{i}")))
        all_payloads.append(
            {
                "title": meta.get("title", ""),
                "source_url": meta.get("notion_url", ""),
                "page_id": meta.get("page_id", ""),
                "text": chunk,
                "chunk_index": i,
                "chunk_total": len(chunks),
                "file": page["file"],
            }
        )

    # 3. 한 번에 Qdrant 저장 (락으로 동시 쓰기 보호)
    try:
        with _qdrant_lock:
            _qdrant_store.insert_vectors(
                vectors=vecs,
                ids=all_ids,
                payloads=all_payloads,
            )
        return len(chunks)
    except Exception as e:
        print(f"     ⚠️  Qdrant 배치 저장 실패: {e}")
        raise


# ─── 그래프 저장 ─────────────────────────────────────────────────────────────
def store_graph(
    triplets: list,
    source_url: str,
    chunks: "list[str] | None" = None,
    reset: bool = False,
) -> dict:
    """타입 트리플 → FalkorDB 노드/엣지 저장

    reset=True: --reset 재인제스트 모드.
      그래프가 이미 비어 있으므로 DB 수준 중복 체크를 건너뜁니다.
      in-memory seen_edges 만으로 동일 페이지 내 중복을 차단합니다.
    """
    nodes_created = 0
    edges_created = 0

    # 노드 캐시 (세션 내 중복 호출 방지)
    node_cache: dict[tuple, int] = {}
    # 엣지 중복 방지: (subj_id, rel_name, obj_id, source_url) → 동일 페이지 내
    # 같은 트리플이 추출되어도 DB 조회 없이 즉시 차단
    seen_edges: set[tuple[int, str, int, str]] = set()

    def get_or_create_node(entity: dict) -> int:
        key = (entity["name"], entity["type"])
        if key in node_cache:
            return node_cache[key]
        # merge_node: 그래프 전체에서 MERGE → 크로스-문서 중복 제거
        node_id = merge_node(_falkordb, entity["name"], entity["type"], source_url)
        if node_id >= 0:
            node_cache[key] = node_id
        return node_id

    for t in triplets:
        subj_id = get_or_create_node(t["subject"])
        obj_id = get_or_create_node(t["object"])
        if subj_id < 0 or obj_id < 0:
            continue

        nodes_created += (
            2 - list(node_cache.values()).count(subj_id) - list(node_cache.values()).count(obj_id)
        )

        pred = t["predicate"]
        # FalkorDB rel_type은 ASCII만 허용 → "REL" 고정, 한국어 이름은 속성으로 저장
        rel_props = {"rel_name": pred["name"], "source_url": source_url}
        for k in ("condition", "order", "duration"):
            if k in pred:
                rel_props[k] = pred[k]
        # v2: 근거 인용문 + 실현 상태 + 청크 직접 링크
        eq = (t.get("evidence_quote") or "").strip()
        if eq:
            rel_props["evidence_quote"] = eq
            rel_props["realization_status"] = detect_realization_status(eq)
            # evidence_chunk_id: Qdrant 청크 UUID → MCP graph_search에서 직접 조회 가능
            cid = find_evidence_chunk_id(eq, chunks or [], source_url)
            if cid:
                rel_props["evidence_chunk_id"] = cid

        # ── 중복 판단 기준: (subj, rel_name, obj, source_url) ────────────────
        # · 같은 source_url + 같은 관계 → 진짜 중복 (한 페이지 내 여러 청크)
        # · 다른 source_url + 같은 관계 → 독립 증거이므로 각각 저장
        edge_key = (subj_id, rel_props["rel_name"], obj_id, source_url)
        if edge_key in seen_edges:
            continue
        seen_edges.add(edge_key)

        try:
            # ── MERGE 방식으로 엣지 생성 (CREATE 대신 사용) ──────────────────
            # MERGE 키: (rel_name, source_url) → 동일 쌍에 대해 DB 레벨 멱등성 보장
            # in-memory seen_edges 가 동일 호출 내 중복을 빠르게 차단하고,
            # MERGE 가 호출 간 중복(재인제스트, 동일 source_url 중복 파일 등)을 차단합니다.
            merge_params: dict = {
                "_s": subj_id,
                "_o": obj_id,
                "_p_rel_name": rel_props["rel_name"],
                "_p_source_url": source_url,
            }
            on_create_parts: list[str] = []
            for k, v in rel_props.items():
                if k in ("rel_name", "source_url"):
                    continue  # MERGE 패턴에 이미 포함
                pk = f"_p_{k}"
                merge_params[pk] = v
                on_create_parts.append(f"r.{k} = ${pk}")
            on_create_clause = (
                ("ON CREATE SET " + ", ".join(on_create_parts)) if on_create_parts else ""
            )

            _falkordb.query(
                "MATCH (s) WHERE id(s) = $_s "
                "MATCH (o) WHERE id(o) = $_o "
                "MERGE (s)-[r:REL {rel_name: $_p_rel_name, source_url: $_p_source_url}]->(o) "
                f"{on_create_clause}",
                merge_params,
            )
            edges_created += 1

            # 의사결정 트리플이면 :Decision 노드로도 기록
            if is_decision_triplet(t):
                record_decision_node(_falkordb, t, source_url)
        except Exception as e:
            print(f"       엣지 생성 실패 ({pred['name']}): {e}")

    return {"nodes": len(node_cache), "edges": edges_created}


# ─── 페이지 인제스천 ─────────────────────────────────────────────────────────
def ingest_page(path: Path, dry_run: bool = False, dept: str = "", reset: bool = False) -> dict:
    page = parse_md(path)
    meta = page["meta"]
    body = page["body"]
    word_count = len(body.split())

    # ── DB 항목: page body가 비어있으면 속성값에서 텍스트 합성 ───────────────
    # Notion DB row는 속성(properties)만 채워지고 page body가 빈 경우가 많다.
    # .md 파일의 db_properties frontmatter를 읽어 body를 재합성한다.
    db_props_meta = meta.get("db_properties", {})
    if db_props_meta and word_count < 30:
        prop_text = "\n".join(f"{k}: {v}" for k, v in db_props_meta.items())
        body = (prop_text + ("\n\n" + body if body.strip() else "")).strip()
        word_count = len(body.split())
        # page 에도 반영해야 합니다. store_vector 는 page["body"] 를 읽으므로,
        # 여기서 로컬 변수만 바꾸면 벡터는 원본(거의 빈 본문)으로 저장되고
        # 해시·청킹·트리플만 합성본으로 계산됩니다. 그러면 sync.py 가 계산한
        # 해시와 영원히 달라져 DB 행이 매번 LLM 재처리되고, evidence_chunk_id
        # 가 Qdrant 에 없는 청크를 가리킵니다.
        page["body"] = body
        if word_count > 0:
            print(f"     🔧 DB 속성에서 텍스트 합성 ({word_count} 단어)")

    print(f"\n  📄 {path.name}  ({word_count} 단어)")
    print(f"     URL: {meta.get('notion_url', '-')}")

    # ── Phase 1-① 경로 분류 ─────────────────────────────────────────────────
    route = classify_page(body, meta, word_count)
    body_hash = content_hash(body)
    has_html_attach = "[첨부 HTML:" in body
    print(f"     경로: {route}")

    if route == "excluded":
        print("     ⚠️  excluded 판정 — 건너뜀")
        if not dry_run and meta.get("page_id"):
            _upsert_notion_page(
                page_id=meta["page_id"],
                dept=dept,
                notion_url=meta.get("notion_url", ""),
                title=meta.get("title", ""),
                last_edited_time=meta.get("last_edited_time"),
                word_count=word_count,
                is_db_item=bool(meta.get("db_properties")),
                has_html_attachment=has_html_attach,
                status="skipped",
                route="excluded",
                content_hash=body_hash,
            )
        return {"file": str(path), "skipped": True, "reason": "excluded"}

    result = {
        "file": str(path),
        "title": meta.get("title", ""),
        "source_url": meta.get("notion_url", ""),
        "word_count": word_count,
        "skipped": False,
    }

    # ── 쓰기 실패 추적 ────────────────────────────────────────────────────
    # 실패한 페이지에 content_hash 를 남기면 이후 sync 가 hash 일치로 건너뛰어
    # 영영 재처리되지 않습니다. 하나라도 실패하면 해시를 비워 둡니다.
    write_failed: list[str] = []

    def _persist_state() -> tuple[str, str, str | None]:
        """(status, content_hash, error_msg)"""
        if write_failed:
            return "error", "", " / ".join(write_failed)[:500]
        return "ok", body_hash, None

    if not dry_run:
        # 1. 벡터 저장 (청킹) — defer·core 모두 실행
        try:
            chunk_count = store_vector(page)
        except Exception as e:
            chunk_count = 0
            write_failed.append(f"벡터 저장 실패: {type(e).__name__}: {e}")
        result["vector_stored"] = chunk_count > 0
        result["chunk_count"] = chunk_count
        print(f"     벡터: {'✅' if chunk_count > 0 else '❌'} {chunk_count}개 청크 저장")

        if route == "defer":
            # defer: 벡터만, LLM 추출 건너뜀
            print("     ↩️  defer 경로 — LLM 추출 생략 (정보 밀도 낮음)")
            result["triplet_count"] = 0
            result["graph"] = {"nodes": 0, "edges": 0}
            result["event_count"] = 0
            _status, _hash, _err = _persist_state()
            if _err:
                result["error"] = _err
            if meta.get("page_id"):
                _upsert_notion_page(
                    page_id=meta["page_id"],
                    dept=dept,
                    notion_url=meta.get("notion_url", ""),
                    title=meta.get("title", ""),
                    last_edited_time=meta.get("last_edited_time"),
                    word_count=word_count,
                    chunk_count=chunk_count,
                    is_db_item=bool(meta.get("db_properties")),
                    has_html_attachment=has_html_attach,
                    status=_status,
                    error_msg=_err,
                    route="defer",
                    content_hash=_hash,
                )
            return result

        # 2. 트리플 추출 (LLM 우선 → 실패 시 Semantica fallback) — core만
        triplets, src = extract_with_fallback(extract_triplets, body)
        if src == "error":
            write_failed.append("트리플 추출 실패")
        result["triplet_count"] = len(triplets)
        print(f"     트리플: {len(triplets)}개 추출 [{src}]")

        # 3. 그래프 저장 (FalkorDB 락으로 동시 쓰기 보호)
        # evidence_chunk_id 연결을 위해 청크 목록 재계산 (store_vector 내부와 동일 로직, 저비용)
        _chunks_for_graph = _make_chunks(body)
        if triplets:
            with _falkordb_lock:
                stats = store_graph(
                    triplets, meta.get("notion_url", ""), chunks=_chunks_for_graph, reset=reset
                )
            result["graph"] = stats
            print(f"     그래프: 노드 {stats['nodes']}개, 엣지 {stats['edges']}개 저장")
        else:
            result["graph"] = {"nodes": 0, "edges": 0}
            print("     그래프: 트리플 없음 — 건너뜀")

        # 4. 이벤트 저장 (DB 속성 우선 → 없으면 LLM 텍스트 추출)
        source_url = meta.get("notion_url", "")
        db_props = meta.get("db_properties", {})
        ev_stored = 0
        skip_llm_ev = False

        # 4a. Notion DB 속성에서 직접 생성 (LLM 없이, 정확도 100%)
        if db_props:
            ev = event_from_db_props(db_props, source_url, meta.get("title", ""))
            if ev:
                with _falkordb_lock:
                    nid = upsert_event_node(_falkordb, ev)
                if nid >= 0:
                    ev_stored += 1
                    skip_llm_ev = True
                    print(f"     이벤트: DB 속성에서 직접 생성 ({ev['game']} / {ev['date']})")

        # 4b. DB 속성에 이벤트 없으면 LLM으로 텍스트 추출 (API 호출, 락 불필요)
        if not skip_llm_ev:
            try:
                events = extract_events_from_text(body)
            except Exception as e:
                events = []
                write_failed.append(f"이벤트 추출 실패: {type(e).__name__}: {e}")
            if events:
                with _falkordb_lock:
                    for ev in events:
                        ev["source_url"] = source_url
                        nid = upsert_event_node(_falkordb, ev)
                        if nid >= 0:
                            ev_stored += 1
                if ev_stored:
                    print(f"     이벤트: {ev_stored}/{len(events)}개 :Event 노드 저장 (LLM 추출)")
            else:
                print("     이벤트: 없음 (날짜 명시 이벤트 미감지)")

        result["event_count"] = ev_stored

        # ── notion_pages 레지스트리 업서트 (PostgreSQL) ──────────────────
        _status, _hash, _err = _persist_state()
        if _err:
            result["error"] = _err
            print(f"     ⚠️  일부 쓰기 실패 — 해시 미기록, 다음 동기화에서 재처리: {_err}")
        if meta.get("page_id"):
            _upsert_notion_page(
                page_id=meta["page_id"],
                dept=dept,
                notion_url=meta.get("notion_url", ""),
                title=meta.get("title", ""),
                last_edited_time=meta.get("last_edited_time"),
                word_count=word_count,
                chunk_count=result.get("chunk_count", 0),
                triplet_count=result.get("triplet_count", 0),
                event_count=ev_stored,
                is_db_item=bool(meta.get("db_properties")),
                has_html_attachment=has_html_attach,
                status=_status,
                error_msg=_err,
                route=route,
                content_hash=_hash,
            )
    else:
        print("     [DRY-RUN] 저장 없이 확인만")

    return result


# ─── 메인 ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Semantica 인제스천 파이프라인")
    parser.add_argument(
        "--dept",
        default="",
        help="본부 이름 (config/departments.yaml의 key). 미지정 시 legacy 모드(data/notion_samples)",
    )
    parser.add_argument("--dry-run", action="store_true", help="연결 확인만 (저장 안 함)")
    parser.add_argument("--reset", action="store_true", help="기존 데이터 삭제 후 재인제스천")
    parser.add_argument(
        "--workers",
        type=int,
        default=10,
        help="병렬 처리 워커 수 (기본: 10). Vertex AI 쿼터에 따라 조정",
    )
    args = parser.parse_args()

    # ── 비즈니스 용어집 사전 미리 로드 (동의어 해결기 워밍업) ────────────────
    # 이벤트 주체 판정(게임 vs 조직)이 용어집 category 에 의존하므로
    # 인제스트 시작 전에 반드시 로드되어 있어야 합니다.
    try:
        from utils.synonym_resolver import categories as _syn_categories, preload as _syn_preload

        _syn_preload()
        _cats = _syn_categories()
        if _cats:
            print(f"  📖 용어집 카테고리: {_cats}")
        else:
            print("  ⚠️  용어집을 사용할 수 없습니다 (네트워크·서비스 확인 필요)")
            print("      → 동의어 정규화와 게임/조직 판정이 비활성화됩니다.")
            print("      → 모든 이벤트가 scope_type='unknown' 으로 저장됩니다.")
            print(f"      → 확인: curl -m 5 {os.environ.get('GLOSSARY_API_URL', GLOSSARY_DEFAULT)}")
    except Exception as _syn_err:
        print(f"  ⚠️  용어집 로드 실패 — 동의어·주체 판정 비활성화: {_syn_err}")

    # 주체 판정 리포트 초기화 (미등록 게임·미분류 이벤트 집계)
    reset_scope_report()

    # ── 본부 설정 로드 ──────────────────────────────────────────────────────
    global COLLECTION_NAME, GRAPH_NAME
    samples_dir = _LEGACY_SAMPLES_DIR

    if args.dept:
        sys.path.insert(0, str(Path(__file__).parent))
        from dept_config import load_dept

        dept_cfg = load_dept(args.dept)
        COLLECTION_NAME = dept_cfg["qdrant_collection"]
        GRAPH_NAME = dept_cfg["falkordb_graph"]
        samples_dir = dept_cfg["data_dir"] / "notion_pages"
        print(f"\n  본부: {dept_cfg['name']} ({args.dept})")
        print(f"  컬렉션: {COLLECTION_NAME}  그래프: {GRAPH_NAME}")
        print(f"  데이터: {samples_dir}")
    else:
        print("\n  ⚠️  --dept 없음 → legacy 모드 (data/notion_samples, joycity_pages)")

    print("\n" + "=" * 60)
    print("🚀 Semantica 인제스천 파이프라인")
    print("=" * 60)

    # ── 클라이언트 초기화 ───────────────────────────────────────────────────
    print("\n[1/4] 클라이언트 초기화")
    ok_llm = init_llm()
    ok_embed = init_embed()
    ok_qdrant = init_qdrant(reset=args.reset) if not args.dry_run else True
    ok_falkor = init_falkordb(reset=args.reset) if not args.dry_run else True

    if args.dry_run:
        print("\n  [DRY-RUN 모드] Docker 연결 테스트만 수행합니다.")
        # 간단히 연결만 시도
        try:
            from qdrant_client import QdrantClient

            qc = QdrantClient(url=QDRANT_URL, timeout=3)
            qc.get_collections()
            print("  ✅ Qdrant 연결 확인")
        except Exception as e:
            print(f"  ❌ Qdrant 미연결: {e}")

        try:
            import redis

            r = redis.Redis(host=FALKORDB_HOST, port=FALKORDB_PORT, socket_timeout=3)
            r.ping()
            print("  ✅ FalkorDB 연결 확인")
        except Exception as e:
            print(f"  ❌ FalkorDB 미연결: {e}")

        try:
            from google import genai

            client = genai.Client(project=GCP_PROJECT, location=LOCATION, vertexai=True)
            test = client.models.embed_content(model=EMBED_MODEL_NAME, contents=["테스트"])
            print(f"  ✅ Vertex AI 임베딩 확인 (dim={len(test.embeddings[0].values)})")
        except Exception as e:
            print(f"  ❌ Vertex AI 임베딩 미연결: {e}")

        print("\n  ✅ 인프라 준비 확인 완료. --dry-run 없이 실행하면 인제스천이 시작됩니다.")
        return

    if not (ok_llm and ok_embed and ok_qdrant and ok_falkor):
        print("\n❌ 초기화 실패 — 위 오류를 확인하고 다시 실행하세요.")
        sys.exit(1)

    # ── 파일 목록 수집 ──────────────────────────────────────────────────────
    md_files = [
        f
        for f in samples_dir.glob("*.md")
        if f.name not in ("README.md", "golden_set.md", "fetch_summary.json")
    ]
    md_files.sort()

    print(f"\n[2/4] 인제스천 대상: {len(md_files)}개 파일")
    print(f"       {samples_dir}")

    # ── 인제스천 실행 (병렬) ─────────────────────────────────────────────────
    workers = args.workers
    print(f"\n[3/4] 페이지 인제스천 시작 (워커: {workers}개 병렬)")
    print(f"       임베딩: 배치 {EMBED_BATCH_SIZE}개씩 / 트리플: Haiku / DB 쓰기: 락 보호")
    results = []
    _t_start = time.time()

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(ingest_page, f, args.dry_run, args.dept, args.reset): f
            for f in md_files
        }
        for done, future in enumerate(as_completed(futures), start=1):
            f = futures[future]
            try:
                r = future.result()
                results.append(r)
                status = "⏭️ " if r.get("skipped") else "✅"
                print(f"  [{done:03d}/{len(md_files):03d}] {status} {f.name}")
            except Exception as e:
                print(f"  [{done:03d}/{len(md_files):03d}] ❌ {f.name}: {e}")
                results.append({"file": str(f), "error": str(e), "skipped": True})

    elapsed = int(time.time() - _t_start)
    print(f"\n  ⏱️  소요 시간: {elapsed // 60}분 {elapsed % 60}초")

    # ── 결과 요약 ────────────────────────────────────────────────────────────
    print("\n[4/4] 결과 요약")
    print("=" * 60)
    stored = [r for r in results if not r.get("skipped") and r.get("vector_stored")]
    skipped = [r for r in results if r.get("skipped")]
    total_chunks = sum(r.get("chunk_count", 0) for r in results)
    total_tri = sum(r.get("triplet_count", 0) for r in results)
    total_nod = sum(r.get("graph", {}).get("nodes", 0) for r in results)
    total_edg = sum(r.get("graph", {}).get("edges", 0) for r in results)
    total_ev = sum(r.get("event_count", 0) for r in results)

    print(f"  페이지:  {len(stored)}/{len(md_files)} 저장 완료")
    print(f"  청크:    {total_chunks}개 벡터 저장 (800자 단위, 200자 겹침)")
    print(f"  건너뜀:  {len(skipped)}개 (텍스트 부족)")
    print(f"  트리플:  {total_tri}개 추출")
    print(f"  그래프:  노드 {total_nod}개 / 엣지 {total_edg}개 저장")
    print(f"  이벤트:  {total_ev}개 :Event 노드 저장")

    # ── 주체 판정 이슈 리포트 ────────────────────────────────────────────────
    _scope_msg = format_scope_report()
    if _scope_msg:
        print()
        print(_scope_msg)

    # ── 로그 저장 ────────────────────────────────────────────────────────────
    log = {
        "summary": {
            "total": len(md_files),
            "stored": len(stored),
            "skipped": len(skipped),
            "triplets": total_tri,
            "nodes": total_nod,
            "edges": total_edg,
        },
        "results": results,
    }
    log_path = LOGS_DIR / "ingest_results.json"
    log_path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  결과 저장: {log_path}")
    print(f"\n  {'✅ 인제스천 완료' if stored else '❌ 저장된 페이지 없음'}")


def _close_clients() -> None:
    """가지고 있는 클라이언트를 닫습니다 (실패해도 종료를 막지 않습니다)."""
    for obj in (_llm_client, _embed_model, _qdrant_store, _falkordb):
        closer = getattr(obj, "close", None)
        if callable(closer):
            with contextlib.suppress(Exception):
                closer()


if __name__ == "__main__":
    main()
    # 작업이 끝나도 프로세스가 안 죽는 문제가 있었습니다. 결과 출력과
    # ingest_results.json 까지 모두 끝난 뒤 인터프리터가 종료를 기다리며
    # 멈춥니다 — Vertex/Qdrant 클라이언트가 남긴 비데몬 스레드 때문입니다.
    # 배치 작업이고 이 시점에는 모든 쓰기가 이미 끝났으므로, 정리해 본 뒤
    # 출력만 flush 하고 확실히 종료합니다. cron 에서 프로세스가 쌓이거나
    # 다음 단계가 영영 시작되지 않는 것보다 낫습니다.
    _close_clients()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
