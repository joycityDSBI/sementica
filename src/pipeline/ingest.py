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
import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from semantica_helper import (
    merge_node, extract_with_fallback,
    is_decision_triplet, record_decision_node,
    upsert_event_node,
)

# ─── .env 로드 ────────────────────────────────────────────────────────────────
_env_path = Path(__file__).parent.parent.parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# ─── 경로 설정 ────────────────────────────────────────────────────────────────
ROOT_DIR  = Path(__file__).parent.parent.parent
LOGS_DIR  = ROOT_DIR / "data" / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# 하위 호환: --dept 없을 때 기존 notion_samples 사용
_LEGACY_SAMPLES_DIR = ROOT_DIR / "data" / "notion_samples"

# ─── Qdrant / FalkorDB 설정 (--dept 로 덮어씀) ───────────────────────────────
QDRANT_URL       = os.environ.get("QDRANT_URL", "http://localhost:6333")
FALKORDB_HOST    = os.environ.get("FALKORDB_HOST", "localhost")
FALKORDB_PORT    = int(os.environ.get("FALKORDB_PORT", "6379"))
COLLECTION_NAME  = "joycity_pages"   # --dept 없을 때 기본값
GRAPH_NAME       = "joycity_kg"      # --dept 없을 때 기본값
# Vertex AI 다국어 임베딩 (한국어 지원, 768차원)
EMBED_MODEL_NAME = "text-multilingual-embedding-002"
EMBED_DIM        = 768

# ─── Vertex AI 설정 ───────────────────────────────────────────────────────────
GCP_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
LOCATION    = os.environ.get("VERTEX_AI_LOCATION", "us-east5")
MODEL       = os.environ.get("VERTEX_AI_MODEL", "claude-sonnet-4-6@default")

# ─── 트리플 추출 프롬프트 (week1_verify.py 와 동일) ─────────────────────────
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
  }}
]

조건/순서/소요시간이 없으면 해당 키를 생략하세요.
트리플이 없으면 빈 배열 [] 반환."""

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
  kpi_milestone   — DAU·매출 마일스톤 달성

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


# ─── 클라이언트 초기화 ────────────────────────────────────────────────────────
_llm_client    = None
_embed_model   = None
_qdrant_store  = None
_falkordb      = None


def init_llm():
    global _llm_client
    if _llm_client:
        return True
    try:
        from anthropic import AnthropicVertex
        _llm_client = AnthropicVertex(project_id=GCP_PROJECT, region=LOCATION)
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

        store.create_collection(COLLECTION_NAME, vector_size=EMBED_DIM, distance="Cosine")
        _qdrant_store = store
        print(f"  ✅ Qdrant 연결 완료 — 컬렉션: {COLLECTION_NAME}")
        return True
    except Exception as e:
        print(f"  ❌ Qdrant 연결 실패: {e}")
        print(f"     → Docker Desktop 실행 후 'docker-compose up -d' 확인")
        return False


def init_falkordb(reset: bool = False):
    global _falkordb
    try:
        import falkordb as _fdb_lib
        db = _fdb_lib.FalkorDB(host=FALKORDB_HOST, port=FALKORDB_PORT)

        if reset:
            try:
                db.delete_graph(GRAPH_NAME)
                print(f"  🗑️  FalkorDB 그래프 삭제: {GRAPH_NAME}")
            except Exception:
                pass

        _falkordb = db.select_graph(GRAPH_NAME)
        print(f"  ✅ FalkorDB 연결 완료 — 그래프: {GRAPH_NAME}")
        return True
    except Exception as e:
        print(f"  ❌ FalkorDB 연결 실패: {e}")
        print(f"     → Docker Desktop 실행 후 'docker-compose up -d' 확인")
        return False


# ─── 파싱 유틸 ───────────────────────────────────────────────────────────────
def parse_md(path: Path) -> dict:
    """마크다운 파일에서 frontmatter + body 파싱.
    db_properties 줄이 있으면 JSON으로 파싱해 meta에 포함합니다.
    """
    content = path.read_text(encoding="utf-8")
    meta = {"title": path.stem, "notion_url": "", "page_id": "", "db_properties": {}}
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
                        try:
                            meta["db_properties"] = json.loads(v)
                        except Exception:
                            pass
                    else:
                        meta[k] = v
            body = content[end + 3:].strip()
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
            try:
                pred["order"] = int(val["order"])
            except (ValueError, TypeError):
                pass
        return pred
    return {"name": str(val)}


# DB 속성 키 별칭 — 다양한 한국어/영어 컬럼명을 통일
_DATE_KEYS    = {"이벤트날짜", "날짜", "일자", "date", "event_date", "시작일", "시작날짜"}
_GAME_KEYS    = {"게임명", "게임", "game", "product", "서비스명", "서비스"}
_TYPE_KEYS    = {"이벤트유형", "유형", "event_type", "type", "종류"}
_MANAGER_KEYS = {"담당자", "담당팀", "manager", "owner", "담당"}


def _event_from_db_props(db_props: dict, source_url: str, title: str) -> dict | None:
    """
    Notion DB 속성에서 이벤트 정보를 추출합니다.
    날짜 + 게임명이 모두 있을 때만 Event 노드로 변환합니다.
    LLM 없이 100% 정확하게 처리됩니다.
    """
    def _first(keys):
        for k in keys:
            if k in db_props:
                v = db_props[k]
                return ", ".join(v) if isinstance(v, list) else str(v)
        return None

    date = _first(_DATE_KEYS)
    game = _first(_GAME_KEYS)
    if not date or not game:
        return None   # 필수 필드 없으면 이벤트 아님

    return {
        "game":        game,
        "event_type":  _first(_TYPE_KEYS) or "user_event",
        "date":        date[:10],
        "title":       title,
        "description": "",
        "manager":     _first(_MANAGER_KEYS) or "",
        "source_url":  source_url,
    }


def extract_events_from_text(text: str) -> list[dict]:
    """Claude로 텍스트에서 날짜 기반 시계열 이벤트를 추출합니다."""
    if not _llm_client:
        return []
    try:
        resp = _llm_client.messages.create(
            model=MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": EVENT_EXTRACT_PROMPT.format(text=text[:3000])}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [
                e for e in parsed
                if isinstance(e, dict) and e.get("game") and e.get("date")
            ]
        return []
    except Exception:
        return []


def extract_triplets(text: str) -> list:
    """Claude Sonnet 4.6로 타입 있는 트리플 추출"""
    if not _llm_client:
        return []
    raw = ""
    try:
        resp = _llm_client.messages.create(
            model=MODEL,
            max_tokens=2048,
            messages=[{"role": "user", "content": EXTRACT_PROMPT.format(text=text[:3000])}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            return []
        result = []
        for t in parsed:
            if isinstance(t, dict):
                result.append({
                    "subject":   _norm_node(t.get("subject", "")),
                    "predicate": _norm_pred(t.get("predicate", "")),
                    "object":    _norm_node(t.get("object", "")),
                })
        return result
    except Exception:
        return []


# ─── 청킹 유틸 ───────────────────────────────────────────────────────────────
CHUNK_SIZE    = 800   # 청크 크기 (자)
CHUNK_OVERLAP = 200   # 청크 간 겹침 (자)


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


# ─── 벡터 저장 (청킹) ────────────────────────────────────────────────────────
def store_vector(page: dict) -> int:
    """페이지를 청크로 분할 → 각 청크 임베딩 → Qdrant 저장
    Returns: 저장된 청크 수 (0이면 실패)
    """
    body = page["body"]
    if not body.strip():
        return 0
    meta = page["meta"]
    base_url = meta.get("notion_url") or page["file"]

    chunks = _make_chunks(body)
    stored = 0
    for i, chunk in enumerate(chunks):
        try:
            result = _embed_model.models.embed_content(
                model=EMBED_MODEL_NAME,
                contents=[chunk],
            )
            vec = result.embeddings[0].values
            # 청크별 고유 ID: 페이지 UUID + 청크 인덱스
            chunk_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{base_url}#chunk{i}"))
            payload = {
                "title":      meta.get("title", ""),
                "source_url": meta.get("notion_url", ""),
                "page_id":    meta.get("page_id", ""),
                "text":       chunk,
                "chunk_index": i,
                "chunk_total": len(chunks),
                "file":       page["file"],
            }
            _qdrant_store.insert_vectors(
                vectors=[vec],
                ids=[chunk_id],
                payloads=[payload],
            )
            stored += 1
        except Exception as e:
            print(f"     ⚠️  청크 {i} 저장 실패: {e}")
        time.sleep(0.1)   # Vertex AI API rate limit
    return stored


# ─── 그래프 저장 ─────────────────────────────────────────────────────────────
def store_graph(triplets: list, source_url: str) -> dict:
    """타입 트리플 → FalkorDB 노드/엣지 저장"""
    nodes_created = 0
    edges_created = 0

    # 노드 캐시 (세션 내 중복 호출 방지)
    node_cache = {}

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
        obj_id  = get_or_create_node(t["object"])
        if subj_id < 0 or obj_id < 0:
            continue

        nodes_created += 2 - list(node_cache.values()).count(subj_id) - list(node_cache.values()).count(obj_id)

        pred = t["predicate"]
        # FalkorDB rel_type은 ASCII만 허용 → "REL" 고정, 한국어 이름은 속성으로 저장
        rel_props = {"rel_name": pred["name"], "source_url": source_url}
        for k in ("condition", "order", "duration"):
            if k in pred:
                rel_props[k] = pred[k]

        try:
            # rel_props 키를 파라미터명 충돌 없이 SET으로 처리
            params = {"_s": subj_id, "_o": obj_id}
            set_parts = []
            for k, v in rel_props.items():
                pk = f"_p_{k}"
                params[pk] = v
                set_parts.append(f"r.{k} = ${pk}")
            set_clause = ("SET " + ", ".join(set_parts)) if set_parts else ""
            _falkordb.query(
                "MATCH (s) WHERE id(s) = $_s "
                "MATCH (o) WHERE id(o) = $_o "
                f"CREATE (s)-[r:REL]->(o) {set_clause}",
                params,
            )
            edges_created += 1

            # 의사결정 트리플이면 :Decision 노드로도 기록
            if is_decision_triplet(t):
                record_decision_node(_falkordb, t, source_url)
        except Exception as e:
            print(f"       엣지 생성 실패 ({pred['name']}): {e}")

    return {"nodes": len(node_cache), "edges": edges_created}


# ─── 페이지 인제스천 ─────────────────────────────────────────────────────────
def ingest_page(path: Path, dry_run: bool = False) -> dict:
    page = parse_md(path)
    meta = page["meta"]
    body = page["body"]
    word_count = len(body.split())

    print(f"\n  📄 {path.name}  ({word_count} 단어)")
    print(f"     URL: {meta.get('notion_url', '-')}")

    if word_count < 50:
        print(f"     ⚠️  텍스트 부족 — 건너뜀")
        return {"file": str(path), "skipped": True, "reason": "텍스트 부족"}

    result = {
        "file":       str(path),
        "title":      meta.get("title", ""),
        "source_url": meta.get("notion_url", ""),
        "word_count": word_count,
        "skipped":    False,
    }

    if not dry_run:
        # 1. 벡터 저장 (청킹)
        chunk_count = store_vector(page)
        result["vector_stored"] = chunk_count > 0
        result["chunk_count"]   = chunk_count
        print(f"     벡터: {'✅' if chunk_count > 0 else '❌'} {chunk_count}개 청크 저장")

        # 2. 트리플 추출 (LLM 우선 → 실패 시 Semantica fallback)
        triplets, src = extract_with_fallback(extract_triplets, body)
        result["triplet_count"] = len(triplets)
        print(f"     트리플: {len(triplets)}개 추출 [{src}]")

        # 3. 그래프 저장
        if triplets:
            stats = store_graph(triplets, meta.get("notion_url", ""))
            result["graph"] = stats
            print(f"     그래프: 노드 {stats['nodes']}개, 엣지 {stats['edges']}개 저장")
        else:
            result["graph"] = {"nodes": 0, "edges": 0}
            print(f"     그래프: 트리플 없음 — 건너뜀")

        # 4. 이벤트 저장 (DB 속성 우선 → 없으면 LLM 텍스트 추출)
        source_url  = meta.get("notion_url", "")
        db_props    = meta.get("db_properties", {})
        ev_stored   = 0
        skip_llm_ev = False

        # 4a. Notion DB 속성에서 직접 생성 (LLM 없이, 정확도 100%)
        if db_props:
            ev = _event_from_db_props(db_props, source_url, meta.get("title", ""))
            if ev:
                nid = upsert_event_node(_falkordb, ev)
                if nid >= 0:
                    ev_stored   += 1
                    skip_llm_ev  = True
                    print(f"     이벤트: DB 속성에서 직접 생성 ({ev['game']} / {ev['date']})")

        # 4b. DB 속성에 이벤트 없으면 LLM으로 텍스트 추출
        if not skip_llm_ev:
            events = extract_events_from_text(body)
            if events:
                for ev in events:
                    ev["source_url"] = source_url
                    nid = upsert_event_node(_falkordb, ev)
                    if nid >= 0:
                        ev_stored += 1
                if ev_stored:
                    print(f"     이벤트: {ev_stored}/{len(events)}개 :Event 노드 저장 (LLM 추출)")
            else:
                print(f"     이벤트: 없음 (날짜 명시 이벤트 미감지)")

        result["event_count"] = ev_stored

        # API 레이트 리밋 준수
        time.sleep(0.5)
    else:
        print(f"     [DRY-RUN] 저장 없이 확인만")

    return result


# ─── 메인 ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Semantica 인제스천 파이프라인")
    parser.add_argument("--dept",    default="",
                        help="본부 이름 (config/departments.yaml의 key). 미지정 시 legacy 모드(data/notion_samples)")
    parser.add_argument("--dry-run", action="store_true", help="연결 확인만 (저장 안 함)")
    parser.add_argument("--reset",   action="store_true", help="기존 데이터 삭제 후 재인제스천")
    args = parser.parse_args()

    # ── 본부 설정 로드 ──────────────────────────────────────────────────────
    global COLLECTION_NAME, GRAPH_NAME
    samples_dir = _LEGACY_SAMPLES_DIR

    if args.dept:
        sys.path.insert(0, str(Path(__file__).parent))
        from dept_config import load_dept
        dept_cfg = load_dept(args.dept)
        COLLECTION_NAME = dept_cfg["qdrant_collection"]
        GRAPH_NAME      = dept_cfg["falkordb_graph"]
        samples_dir     = dept_cfg["data_dir"] / "notion_pages"
        print(f"\n  본부: {dept_cfg['name']} ({args.dept})")
        print(f"  컬렉션: {COLLECTION_NAME}  그래프: {GRAPH_NAME}")
        print(f"  데이터: {samples_dir}")
    else:
        print(f"\n  ⚠️  --dept 없음 → legacy 모드 (data/notion_samples, joycity_pages)")


    print("\n" + "=" * 60)
    print("🚀 Semantica 인제스천 파이프라인")
    print("=" * 60)

    # ── 클라이언트 초기화 ───────────────────────────────────────────────────
    print("\n[1/4] 클라이언트 초기화")
    ok_llm    = init_llm()
    ok_embed  = init_embed()
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
    md_files = [f for f in samples_dir.glob("*.md")
                if f.name not in ("README.md", "golden_set.md", "fetch_summary.json")]
    md_files.sort()

    print(f"\n[2/4] 인제스천 대상: {len(md_files)}개 파일")
    print(f"       {samples_dir}")

    # ── 인제스천 실행 ────────────────────────────────────────────────────────
    print(f"\n[3/4] 페이지 인제스천 시작")
    results = []
    for f in md_files:
        r = ingest_page(f, dry_run=args.dry_run)
        results.append(r)

    # ── 결과 요약 ────────────────────────────────────────────────────────────
    print(f"\n[4/4] 결과 요약")
    print("=" * 60)
    stored      = [r for r in results if not r.get("skipped") and r.get("vector_stored")]
    skipped     = [r for r in results if r.get("skipped")]
    total_chunks= sum(r.get("chunk_count", 0) for r in results)
    total_tri   = sum(r.get("triplet_count", 0) for r in results)
    total_nod   = sum(r.get("graph", {}).get("nodes", 0) for r in results)
    total_edg   = sum(r.get("graph", {}).get("edges", 0) for r in results)
    total_ev    = sum(r.get("event_count", 0) for r in results)

    print(f"  페이지:  {len(stored)}/{len(md_files)} 저장 완료")
    print(f"  청크:    {total_chunks}개 벡터 저장 (800자 단위, 200자 겹침)")
    print(f"  건너뜀:  {len(skipped)}개 (텍스트 부족)")
    print(f"  트리플:  {total_tri}개 추출")
    print(f"  그래프:  노드 {total_nod}개 / 엣지 {total_edg}개 저장")
    print(f"  이벤트:  {total_ev}개 :Event 노드 저장")

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


if __name__ == "__main__":
    main()
