"""
증분 동기화 스크립트 (Incremental Sync)
Notion에서 마지막 동기화 이후 수정된 페이지만 Qdrant + FalkorDB에 업데이트합니다.

동작 원리:
  1. data/{dept}/sync_state.json 에서 last_sync_time 읽기
  2. Notion API에서 last_edited_time 내림차순으로 페이지 조회
  3. last_sync_time 이후 수정된 페이지만 처리:
     a. Qdrant: source_url 필터로 기존 벡터 전체 삭제 후 재삽입
     b. FalkorDB: source_url 필터로 기존 엣지 전체 삭제 후 재삽입
  4. sync_state.json 갱신

사용법:
  python src/pipeline/sync.py --dept strategic          # 증분 동기화
  python src/pipeline/sync.py --dept strategic --full   # 강제 전체 재동기화
  python src/pipeline/sync.py --dept strategic --dry-run # 확인만

Cron 등록 (매일 새벽 2시):
  0 2 * * * cd ~/sementica && .venv/bin/python src/pipeline/sync.py --dept strategic >> data/logs/sync.log 2>&1
"""

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

# ─── .env 로드 ────────────────────────────────────────────────────────────────
_env_path = Path(__file__).parent.parent.parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(Path(__file__).parent))

try:
    import httpx
except ImportError:
    raise SystemExit("httpx가 필요합니다: pip install httpx")

from dept_config import load_dept
from notion_fetch import (
    notion_headers, fetch_blocks_recursive, page_title,
    RATE_LIMIT_DELAY,
)

# ─── 설정 ─────────────────────────────────────────────────────────────────────
GCP_PROJECT      = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
LOCATION         = os.environ.get("VERTEX_AI_LOCATION", "us-east5")
EMBED_MODEL_NAME = "text-multilingual-embedding-002"
QDRANT_URL       = os.environ.get("QDRANT_URL", "http://localhost:6333")
FALKORDB_HOST    = os.environ.get("FALKORDB_HOST", "localhost")
FALKORDB_PORT    = int(os.environ.get("FALKORDB_PORT", "6379"))
CHUNK_SIZE       = 800
CHUNK_OVERLAP    = 200

# Claude 트리플 추출 프롬프트 (ingest.py와 동일)
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
    "object":    {{"name": "엔티티B", "type": "Process"}}
  }}
]

조건/순서/소요시간이 없으면 해당 키를 생략하세요.
트리플이 없으면 빈 배열 [] 반환."""


# ─── 청킹 ────────────────────────────────────────────────────────────────────
def _make_chunks(text: str) -> list[str]:
    chunks, start = [], 0
    while start < len(text):
        chunk = text[start:start + CHUNK_SIZE].strip()
        if chunk:
            chunks.append(chunk)
        if start + CHUNK_SIZE >= len(text):
            break
        start = start + CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


# ─── Qdrant 벡터 삭제 (source_url 필터) ──────────────────────────────────────
def delete_page_vectors(qc, collection_name: str, source_url: str) -> int:
    """source_url이 일치하는 벡터 전체 삭제. 삭제된 수 반환."""
    from qdrant_client.models import Filter, FieldCondition, MatchValue
    try:
        result = qc.delete(
            collection_name=collection_name,
            points_selector=Filter(
                must=[FieldCondition(key="source_url", match=MatchValue(value=source_url))]
            ),
        )
        return getattr(result, 'deleted', 0) or 0
    except Exception as e:
        print(f"    ⚠️  벡터 삭제 실패: {e}")
        return 0


# ─── FalkorDB 엣지 삭제 (source_url 필터) ────────────────────────────────────
def delete_page_edges(graph, source_url: str) -> int:
    """source_url이 일치하는 엣지 전체 삭제. 삭제된 수 반환."""
    try:
        res = graph.query(
            "MATCH ()-[r:REL {source_url: $url}]->() DELETE r RETURN count(r) AS cnt",
            {"url": source_url}
        )
        return res.result_set[0][0] if res.result_set else 0
    except Exception as e:
        print(f"    ⚠️  엣지 삭제 실패: {e}")
        return 0


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


def extract_triplets(llm_client, text: str) -> list:
    try:
        resp = llm_client.messages.create(
            model=os.environ.get("VERTEX_AI_MODEL", "claude-sonnet-4-6@default"),
            max_tokens=2048,
            messages=[{"role": "user", "content": EXTRACT_PROMPT.format(text=text[:3000])}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1][4:] if len(parts) > 1 else raw
        parsed = json.loads(raw.strip())
        return [
            {
                "subject":   _norm_node(t.get("subject", "")),
                "predicate": _norm_pred(t.get("predicate", "")),
                "object":    _norm_node(t.get("object", "")),
            }
            for t in parsed if isinstance(t, dict)
        ]
    except Exception:
        return []


# ─── 페이지 동기화 (핵심 함수) ───────────────────────────────────────────────
def sync_page(
    notion_client, token: str, page_meta: dict,
    qc, embed_client, llm_client, graph,
    collection_name: str, dry_run: bool = False,
) -> dict:
    """
    단일 페이지를 Notion에서 가져와 Qdrant + FalkorDB 업데이트
    """
    page_id    = page_meta["id"].replace("-", "")
    title      = page_title(page_meta)
    source_url = page_meta.get("url", "")

    result = {
        "page_id":    page_id,
        "title":      title,
        "source_url": source_url,
        "deleted_vectors": 0,
        "deleted_edges":   0,
        "new_chunks":      0,
        "new_triplets":    0,
    }

    print(f"  🔄 {title[:60]}")
    print(f"     {source_url}")

    if dry_run:
        print(f"     [DRY-RUN] 건너뜀")
        return result

    # 1. Notion에서 본문 가져오기
    try:
        body = fetch_blocks_recursive(notion_client, token, page_id)
    except Exception as e:
        print(f"     ❌ 본문 조회 실패: {e}")
        result["error"] = str(e)
        return result

    if len(body.split()) < 50:
        print(f"     ⚠️  텍스트 부족 ({len(body.split())} 단어) — 건너뜀")
        result["skipped"] = True
        return result

    # 2. 기존 벡터 삭제
    deleted_v = delete_page_vectors(qc, collection_name, source_url)
    result["deleted_vectors"] = deleted_v
    print(f"     벡터 삭제: {deleted_v}개")

    # 3. 기존 엣지 삭제
    deleted_e = delete_page_edges(graph, source_url)
    result["deleted_edges"] = deleted_e
    print(f"     엣지 삭제: {deleted_e}개")

    # 4. 청킹 + 임베딩 + Qdrant 저장
    chunks = _make_chunks(body)
    new_chunks = 0
    for i, chunk in enumerate(chunks):
        try:
            res = embed_client.models.embed_content(
                model=EMBED_MODEL_NAME, contents=[chunk]
            )
            vec      = list(res.embeddings[0].values)
            chunk_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source_url}#chunk{i}"))
            qc.upsert(
                collection_name=collection_name,
                points=[{
                    "id":      chunk_id,
                    "vector":  vec,
                    "payload": {
                        "title":       title,
                        "source_url":  source_url,
                        "page_id":     page_id,
                        "text":        chunk,
                        "chunk_index": i,
                        "chunk_total": len(chunks),
                    },
                }],
            )
            new_chunks += 1
        except Exception as e:
            print(f"     ⚠️  청크 {i} 저장 실패: {e}")
        time.sleep(0.1)

    result["new_chunks"] = new_chunks
    print(f"     벡터 저장: {new_chunks}개 청크")

    # 5. 트리플 추출 + FalkorDB 저장
    triplets = extract_triplets(llm_client, body)
    node_cache = {}

    def get_or_create_node(entity: dict) -> int:
        key = (entity["name"], entity["type"])
        if key in node_cache:
            return node_cache[key]
        try:
            r = graph.create_node(
                labels=[entity["type"]],
                properties={"name": entity["name"], "source_url": source_url},
            )
            nid = r.get("id") or r.get("node_id") or 0
            node_cache[key] = nid
            return nid
        except Exception:
            return -1

    edges_created = 0
    for t in triplets:
        sid = get_or_create_node(t["subject"])
        oid = get_or_create_node(t["object"])
        if sid < 0 or oid < 0:
            continue
        pred = t["predicate"]
        props = {"rel_name": pred["name"], "source_url": source_url}
        for k in ("condition", "order", "duration"):
            if k in pred:
                props[k] = pred[k]
        try:
            graph.create_relationship(
                start_node_id=sid, end_node_id=oid,
                rel_type="REL", properties=props,
            )
            edges_created += 1
        except Exception:
            pass

    result["new_triplets"] = edges_created
    print(f"     그래프: {len(node_cache)}개 노드 / {edges_created}개 엣지")

    return result


# ─── 동기화 상태 관리 ─────────────────────────────────────────────────────────
def load_sync_state(state_path: Path) -> dict:
    if state_path.exists():
        return json.loads(state_path.read_text(encoding="utf-8"))
    # 초기 상태: 1970-01-01 (전체 동기화)
    return {"last_sync_time": "1970-01-01T00:00:00.000Z", "total_synced": 0}


def save_sync_state(state_path: Path, state: dict):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ─── Notion 수정 페이지 조회 ──────────────────────────────────────────────────
def fetch_modified_pages(client, token: str, since_iso: str) -> list:
    """last_edited_time > since_iso 인 페이지만 반환 (최신순 정렬)"""
    modified = []
    cursor   = None
    while True:
        body = {
            "filter": {"value": "page", "property": "object"},
            "sort":   {"direction": "descending", "timestamp": "last_edited_time"},
            "page_size": 100,
        }
        if cursor:
            body["start_cursor"] = cursor
        resp = client.post(
            "https://api.notion.com/v1/search",
            headers=notion_headers(token),
            json=body,
        )
        resp.raise_for_status()
        time.sleep(RATE_LIMIT_DELAY)
        data    = resp.json()
        results = data.get("results", [])

        stop = False
        for page in results:
            last_edited = page.get("last_edited_time", "")
            if last_edited <= since_iso:
                stop = True
                break
            modified.append(page)

        if stop or not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    return modified


# ─── 메인 ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Semantica 증분 동기화")
    parser.add_argument("--dept",    required=True, help="본부 이름 (config/departments.yaml)")
    parser.add_argument("--full",    action="store_true", help="전체 재동기화 (last_sync_time 무시)")
    parser.add_argument("--dry-run", action="store_true", help="변경 내용 확인만 (저장 안 함)")
    args = parser.parse_args()

    # ── 본부 설정 로드 ──────────────────────────────────────────────────────
    dept_cfg        = load_dept(args.dept)
    token           = dept_cfg["notion_token"]
    collection_name = dept_cfg["qdrant_collection"]
    graph_name      = dept_cfg["falkordb_graph"]
    data_dir        = dept_cfg["data_dir"]
    state_path      = data_dir / "sync_state.json"
    log_dir         = ROOT / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    now_iso   = datetime.now(timezone.utc).isoformat()
    state     = load_sync_state(state_path)
    since_iso = "1970-01-01T00:00:00.000Z" if args.full else state["last_sync_time"]

    print("=" * 60)
    print(f"🔄 증분 동기화 — {dept_cfg['name']} ({args.dept})")
    print(f"   컬렉션: {collection_name}  그래프: {graph_name}")
    print(f"   기준:   {since_iso[:19]} 이후 수정된 페이지")
    print(f"   시작:   {now_iso[:19]}")
    if args.dry_run:
        print("   [DRY-RUN 모드]")
    print("=" * 60)

    # ── 클라이언트 초기화 ──────────────────────────────────────────────────
    print("\n[1/4] 클라이언트 초기화")
    from google import genai
    from qdrant_client import QdrantClient
    import falkordb as fdb
    from anthropic import AnthropicVertex

    embed_client = genai.Client(project=GCP_PROJECT, location=LOCATION, vertexai=True)
    qc           = QdrantClient(url=QDRANT_URL)
    db           = fdb.FalkorDB(host=FALKORDB_HOST, port=FALKORDB_PORT)
    graph        = db.select_graph(graph_name)
    llm_client   = AnthropicVertex(project_id=GCP_PROJECT, region=LOCATION)
    print("  ✅ 완료")

    # ── 수정 페이지 조회 ───────────────────────────────────────────────────
    print(f"\n[2/4] Notion에서 수정 페이지 조회 중...")
    with httpx.Client(timeout=60) as notion_client:
        modified_pages = fetch_modified_pages(notion_client, token, since_iso)
        print(f"  📋 {len(modified_pages)}개 수정 페이지 발견")

        if not modified_pages:
            print("\n  ✅ 새로 수정된 페이지 없음 — 동기화 완료")
            state["last_sync_time"] = now_iso
            state["last_check"]     = now_iso
            if not args.dry_run:
                save_sync_state(state_path, state)
            return

        # ── 동기화 실행 ─────────────────────────────────────────────────
        print(f"\n[3/4] 페이지 동기화 시작")
        results = []
        for i, page in enumerate(modified_pages, 1):
            print(f"\n  [{i:03d}/{len(modified_pages):03d}]")
            r = sync_page(
                notion_client, token, page,
                qc, embed_client, llm_client, graph,
                collection_name, dry_run=args.dry_run,
            )
            results.append(r)
            time.sleep(0.5)

    # ── 결과 요약 ─────────────────────────────────────────────────────────
    success  = [r for r in results if not r.get("error") and not r.get("skipped")]
    errors   = [r for r in results if r.get("error")]
    skipped  = [r for r in results if r.get("skipped")]
    total_v  = sum(r.get("new_chunks", 0) for r in success)
    total_e  = sum(r.get("new_triplets", 0) for r in success)

    print(f"\n[4/4] 동기화 결과")
    print("=" * 60)
    print(f"  처리: {len(success)}개 / 건너뜀: {len(skipped)}개 / 오류: {len(errors)}개")
    print(f"  신규 벡터 청크: {total_v}개")
    print(f"  신규 트리플:    {total_e}개")

    # ── 상태 저장 ─────────────────────────────────────────────────────────
    if not args.dry_run:
        state["last_sync_time"]  = now_iso
        state["last_sync_count"] = len(success)
        state["total_synced"]    = state.get("total_synced", 0) + len(success)
        save_sync_state(state_path, state)
        print(f"\n  💾 sync_state.json 갱신: {state_path}")

    # ── 로그 저장 ─────────────────────────────────────────────────────────
    log = {
        "dept": args.dept, "since": since_iso, "now": now_iso,
        "modified": len(modified_pages), "success": len(success),
        "skipped": len(skipped), "errors": len(errors),
        "new_chunks": total_v, "new_triplets": total_e,
        "results": results,
    }
    log_path = log_dir / f"sync_{args.dept}_{now_iso[:10].replace('-', '')}.json"
    log_path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  📄 로그: {log_path}")
    print(f"\n  {'✅ 동기화 완료' if not errors else f'⚠️  {len(errors)}개 오류 발생'}")


if __name__ == "__main__":
    main()
