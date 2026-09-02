"""
Semantica 웹 운영 대시보드

기능:
  - 인제스션 현황 / Qdrant·FalkorDB 통계 / sync 이력
  - 수동 배치 실행 (수집/인제스천/동기화/삭제정리) + 실시간 출력
  - 시맨틱 검색 품질 테스트

실행:
  python src/ops/web_app.py              # 기본 8080 포트
  python src/ops/web_app.py --port 9090  # 포트 지정

사전 설치:
  pip install fastapi uvicorn
"""

import json
import os
import subprocess
import sys
import threading
import uuid
from datetime import UTC, datetime
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
sys.path.insert(0, str(ROOT / "src" / "pipeline"))
sys.path.insert(0, str(ROOT / "src" / "ops"))

POSTGRES_URL  = os.environ.get("POSTGRES_URL", "")
QDRANT_URL    = os.environ.get("QDRANT_URL", "http://localhost:6333")
FALKORDB_HOST = os.environ.get("FALKORDB_HOST", "localhost")
FALKORDB_PORT = int(os.environ.get("FALKORDB_PORT", "6379"))

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import HTMLResponse, JSONResponse
    from pydantic import BaseModel
except ImportError:
    raise SystemExit("pip install fastapi uvicorn") from None

app = FastAPI(title="Semantica Ops", version="1.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

# ─── 잡 저장소 ────────────────────────────────────────────────────────────────
_jobs: dict[str, dict] = {}
_JOBS_LOG = ROOT / "data" / "logs" / "batch_jobs.json"


def _persist_job(job: dict) -> None:
    """완료된 잡 메타데이터를 JSON 파일에 저장 (proc·lines 제외, 최대 100건 보존)."""
    try:
        history: list = []
        if _JOBS_LOG.exists():
            try:
                history = json.loads(_JOBS_LOG.read_text(encoding="utf-8"))
            except Exception:
                history = []
        # 동일 job_id 중복 제거 후 최신 항목을 앞에 삽입
        history = [h for h in history if h.get("job_id") != job.get("job_id")]
        entry = {k: v for k, v in job.items() if k not in ("proc", "lines")}
        history.insert(0, entry)
        history = history[:100]
        _JOBS_LOG.parent.mkdir(parents=True, exist_ok=True)
        _JOBS_LOG.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _load_jobs_history() -> None:
    """앱 시작 시 이전 잡 이력을 _jobs에 복원.
    재시작으로 중단된 running 상태는 error로 변경."""
    if not _JOBS_LOG.exists():
        return
    try:
        for entry in json.loads(_JOBS_LOG.read_text(encoding="utf-8")):
            jid = entry.get("job_id")
            if not jid or jid in _jobs:
                continue
            if entry.get("status") == "running":
                entry["status"]     = "error"   # 재시작으로 인한 강제 중단
                entry["returncode"] = -1
            entry.setdefault("proc",  None)
            entry.setdefault("lines", [])
            _jobs[jid] = entry
    except Exception:
        pass


_load_jobs_history()   # 앱 기동 시 이전 이력 복원


# ─── 유틸 ─────────────────────────────────────────────────────────────────────
def _pg_conn():
    if not POSTGRES_URL:
        return None
    try:
        import psycopg2
        return psycopg2.connect(POSTGRES_URL)
    except Exception:
        return None


def _rows(cur) -> list[dict]:
    cols = [d[0] for d in cur.description]
    result = []
    for row in cur.fetchall():
        d = {}
        for k, v in zip(cols, row):
            if hasattr(v, "isoformat"):
                v = v.isoformat()
            elif hasattr(v, "__float__"):
                try:
                    v = float(v)
                except Exception:
                    v = str(v)
            d[k] = v
        result.append(d)
    return result


# ─── 본부 목록 ────────────────────────────────────────────────────────────────
@app.get("/api/depts")
def api_depts():
    try:
        from dept_config import list_depts
        return {"depts": list_depts()}
    except Exception:
        return {"depts": ["strategic"]}


# ─── 시스템 상태 ──────────────────────────────────────────────────────────────
@app.get("/api/status")
def api_status():
    out: dict = {
        "postgres": False, "qdrant": False, "falkordb": False,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    conn = _pg_conn()
    if conn:
        out["postgres"] = True
        conn.close()
    try:
        from qdrant_client import QdrantClient
        cols = QdrantClient(url=QDRANT_URL, timeout=3).get_collections().collections
        out["qdrant"] = True
        out["qdrant_collections"] = [c.name for c in cols]
    except Exception:
        pass
    try:
        import redis as _r
        _r.Redis(host=FALKORDB_HOST, port=FALKORDB_PORT, socket_timeout=3).ping()
        out["falkordb"] = True
    except Exception:
        pass
    return out


# ─── 페이지 현황 ──────────────────────────────────────────────────────────────
@app.get("/api/pages")
def api_pages(
    dept:     str = "strategic",
    page:     int = 1,
    per_page: int = 200,
    search:   str = "",
):
    conn = _pg_conn()
    if not conn:
        return {"error": "PostgreSQL 없음", "pages": [], "stats": {}, "totals": {},
                "total_count": 0, "page": page, "per_page": per_page}
    try:
        offset = (max(page, 1) - 1) * per_page
        like   = f"%{search}%" if search else None

        # 상태별 집계 (검색어 무관 — 전체 기준)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, COUNT(*) FROM notion_pages WHERE dept=%s GROUP BY status",
                (dept,),
            )
            stats = {r[0]: r[1] for r in cur.fetchall()}

        # 청크 합계 (검색어 무관)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT SUM(chunk_count), SUM(triplet_count), SUM(event_count) "
                "FROM notion_pages WHERE dept=%s AND status='ok'",
                (dept,),
            )
            row = cur.fetchone()
            totals = {
                "chunks":   int(row[0] or 0),
                "triplets": int(row[1] or 0),
                "events":   int(row[2] or 0),
            }

        # 검색 조건
        if like:
            where  = "dept=%s AND title ILIKE %s"
            p_cnt  = (dept, like)
            p_list = (dept, like, per_page, offset)
        else:
            where  = "dept=%s"
            p_cnt  = (dept,)
            p_list = (dept, per_page, offset)

        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM notion_pages WHERE {where}", p_cnt)
            total_count = cur.fetchone()[0]

        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT page_id, title, notion_url, last_edited_time,
                           last_ingested_at, word_count, chunk_count,
                           triplet_count, event_count, is_db_item, status
                    FROM notion_pages WHERE {where}
                    ORDER BY last_ingested_at DESC LIMIT %s OFFSET %s""",
                p_list,
            )
            pages = _rows(cur)

        conn.close()
        return {
            "pages":       pages,
            "stats":       stats,
            "totals":      totals,
            "total_count": total_count,
            "page":        page,
            "per_page":    per_page,
        }
    except Exception as e:
        conn.close()
        return {"error": str(e), "pages": [], "stats": {}, "totals": {},
                "total_count": 0, "page": page, "per_page": per_page}


# ─── sync 이력 ────────────────────────────────────────────────────────────────
@app.get("/api/sync-log")
def api_sync_log(dept: str = "strategic", limit: int = 20):
    conn = _pg_conn()
    if not conn:
        return {"error": "PostgreSQL 없음", "logs": []}
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, dept, since_time, modified_found, processed,
                          skipped, errors, new_chunks, new_triplets,
                          duration_sec, status, created_at
                   FROM sync_log WHERE dept=%s
                   ORDER BY created_at DESC LIMIT %s""",
                (dept, limit),
            )
            logs = _rows(cur)
        conn.close()
        return {"logs": logs}
    except Exception as e:
        conn.close()
        return {"error": str(e), "logs": []}


# ─── MCP 로그 ─────────────────────────────────────────────────────────────────
@app.get("/api/mcp-log")
def api_mcp_log(dept: str = "strategic", limit: int = 50):
    conn = _pg_conn()
    if not conn:
        return {"error": "PostgreSQL 없음", "stats": [], "recent": []}
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT tool, COUNT(*) AS cnt,
                          ROUND(AVG(duration_ms)) AS avg_ms,
                          ROUND(AVG(result_count), 1) AS avg_results
                   FROM mcp_request_log WHERE dept=%s
                   GROUP BY tool ORDER BY cnt DESC""",
                (dept,),
            )
            stats = _rows(cur)
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, tool, query, result_count, duration_ms, error, created_at
                   FROM mcp_request_log WHERE dept=%s
                   ORDER BY created_at DESC LIMIT %s""",
                (dept, limit),
            )
            recent = _rows(cur)
        conn.close()
        return {"stats": stats, "recent": recent}
    except Exception as e:
        conn.close()
        return {"error": str(e), "stats": [], "recent": []}


# ─── Qdrant 통계 ──────────────────────────────────────────────────────────────
@app.get("/api/qdrant-stats")
def api_qdrant_stats():
    try:
        from qdrant_client import QdrantClient
        qc = QdrantClient(url=QDRANT_URL, timeout=5)
        result = []
        for c in qc.get_collections().collections:
            info = qc.get_collection(c.name)
            result.append({
                "name": c.name,
                "points": info.points_count or 0,
                "vectors": info.vectors_count or 0,
                "status": str(info.status),
            })
        return {"collections": result}
    except Exception as e:
        return {"error": str(e), "collections": []}


# ─── FalkorDB 통계 ────────────────────────────────────────────────────────────
@app.get("/api/graph-stats")
def api_graph_stats(dept: str = "strategic"):
    try:
        from dept_config import load_dept
        graph_name = load_dept(dept)["falkordb_graph"]
        import falkordb as fdb
        g = fdb.FalkorDB(host=FALKORDB_HOST, port=FALKORDB_PORT).select_graph(graph_name)
        def _q(cypher):
            r = g.query(cypher)
            return r.result_set[0][0] if r.result_set else 0
        return {
            "graph":   graph_name,
            "nodes":   _q("MATCH (n) RETURN count(n)"),
            "edges":   _q("MATCH ()-[r]->() RETURN count(r)"),
            "events":  _q("MATCH (e:Event) RETURN count(e)"),
            "games":   _q("MATCH (g:Game) RETURN count(g)"),
            "decisions": _q("MATCH (d:Decision) RETURN count(d)"),
        }
    except Exception as e:
        return {"error": str(e)}


# ─── 배치 실행 ────────────────────────────────────────────────────────────────
_BATCH_CMDS = {
    "fetch":        ("src/pipeline/notion_fetch.py", []),
    "ingest":       ("src/pipeline/ingest.py",       []),
    "ingest_reset": ("src/pipeline/ingest.py",       ["--reset"]),
    "sync":         ("src/pipeline/sync.py",         []),
    "sync_full":    ("src/pipeline/sync.py",         ["--full"]),
    "sync_dry":     ("src/pipeline/sync.py",         ["--dry-run"]),
    "reconcile":    ("src/pipeline/sync.py",         ["--reconcile"]),
}
_BATCH_LABELS = {
    "fetch":        "Notion 전체 수집",
    "ingest":       "인제스천",
    "ingest_reset": "인제스천 (전체 초기화)",
    "sync":         "증분 동기화",
    "sync_full":    "전체 재동기화",
    "sync_dry":     "동기화 Dry-run",
    "reconcile":    "삭제 페이지 정리",
}


class BatchRequest(BaseModel):
    dept: str = "strategic"
    type: str


@app.post("/api/batch/run")
def batch_run(req: BatchRequest):
    running = [j for j in _jobs.values() if j["status"] == "running"]
    if running:
        return JSONResponse(
            {"error": "이미 실행 중인 배치가 있습니다.", "job_id": running[0]["job_id"]},
            status_code=409,
        )
    if req.type not in _BATCH_CMDS:
        raise HTTPException(400, f"알 수 없는 배치 타입: {req.type}")

    script, extra = _BATCH_CMDS[req.type]
    venv_py = ROOT / ".venv" / "bin" / "python"
    python  = str(venv_py) if venv_py.exists() else sys.executable
    cmd     = [python, script, "--dept", req.dept] + extra

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=str(ROOT),
        )
    except Exception as e:
        raise HTTPException(500, str(e)) from e

    job_id = str(uuid.uuid4())[:8]
    job = {
        "job_id":     job_id,
        "proc":       proc,
        "lines":      [],
        "status":     "running",
        "started_at": datetime.now(UTC).isoformat(),
        "cmd":        " ".join(cmd),
        "type":       req.type,
        "label":      _BATCH_LABELS.get(req.type, req.type),
        "dept":       req.dept,
    }
    _jobs[job_id] = job

    def _read():
        for line in proc.stdout:
            _jobs[job_id]["lines"].append(line.rstrip("\n"))
        proc.wait()
        rc = proc.returncode
        _jobs[job_id]["status"]      = "done" if rc == 0 else "error"
        _jobs[job_id]["returncode"]  = rc
        _jobs[job_id]["finished_at"] = datetime.now(UTC).isoformat()
        _persist_job(_jobs[job_id])   # 완료 즉시 파일에 영속화

    threading.Thread(target=_read, daemon=True).start()
    return {"job_id": job_id, "label": job["label"]}


@app.get("/api/batch/{job_id}/output")
def batch_output(job_id: str, from_line: int = 0):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "잡을 찾을 수 없습니다.")
    return {
        "lines":      job["lines"][from_line:],
        "total":      len(job["lines"]),
        "status":     job["status"],
        "returncode": job.get("returncode"),
    }


@app.post("/api/batch/{job_id}/cancel")
def batch_cancel(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    if job["status"] == "running":
        job["proc"].terminate()
        job["status"] = "cancelled"
        _persist_job(job)   # 취소 이력도 영속화
    return {"status": job["status"]}


@app.get("/api/jobs")
def list_jobs():
    return [
        {k: v for k, v in j.items() if k != "proc"}
        for j in sorted(_jobs.values(), key=lambda x: x["started_at"], reverse=True)
        if j["started_at"]
    ]


# ─── 검색 테스트 ──────────────────────────────────────────────────────────────
class SearchRequest(BaseModel):
    query: str
    dept:  str = "strategic"
    limit: int = 5


@app.post("/api/search/test")
def search_test(req: SearchRequest):
    try:
        from dept_config import load_dept
        collection = load_dept(req.dept)["qdrant_collection"]

        from google import genai
        embed_client = genai.Client(
            project=os.environ.get("GOOGLE_CLOUD_PROJECT", ""),
            location=os.environ.get("VERTEX_AI_LOCATION", "us-east5"),
            vertexai=True,
        )
        res = embed_client.models.embed_content(
            model="text-multilingual-embedding-002", contents=[req.query]
        )
        vec = list(res.embeddings[0].values)

        from qdrant_client import QdrantClient
        result = QdrantClient(url=QDRANT_URL).query_points(
            collection_name=collection,
            query=vec,
            limit=req.limit,
            with_payload=True,
        )
        hits = result.points
        return {
            "query":   req.query,
            "results": [
                {
                    "score":       round(h.score, 4),
                    "title":       h.payload.get("title", ""),
                    "source_url":  h.payload.get("source_url", ""),
                    "text":        h.payload.get("text", "")[:400],
                    "chunk_index": h.payload.get("chunk_index", 0),
                    "chunk_total": h.payload.get("chunk_total", 0),
                }
                for h in hits
            ],
        }
    except Exception as e:
        return {"error": str(e), "results": []}


# ─── 골든셋 ──────────────────────────────────────────────────────────────────
class GoldenItem(BaseModel):
    dept:     str
    query:    str
    expected: list[str]          # 기대 문서 제목 목록
    top_k:    int = 5
    notes:    str = ""


@app.get("/api/golden")
def golden_list(dept: str = "strategic"):
    conn = _pg_conn()
    if not conn:
        return {"error": "PostgreSQL 없음", "items": []}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, dept, query, expected, top_k, notes, created_at "
                "FROM search_golden_set WHERE dept=%s ORDER BY id",
                (dept,),
            )
            rows = _rows(cur)
        conn.close()
        return {"items": rows}
    except Exception as e:
        conn.close()
        return {"error": str(e), "items": []}


@app.post("/api/golden")
def golden_create(item: GoldenItem):
    conn = _pg_conn()
    if not conn:
        raise HTTPException(503, "PostgreSQL 없음")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO search_golden_set (dept, query, expected, top_k, notes) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (item.dept, item.query, item.expected, item.top_k, item.notes or None),
            )
            new_id = cur.fetchone()[0]
        conn.commit()
        conn.close()
        return {"id": new_id}
    except Exception as e:
        conn.rollback()
        conn.close()
        raise HTTPException(500, str(e)) from e


@app.delete("/api/golden/{item_id}")
def golden_delete(item_id: int):
    conn = _pg_conn()
    if not conn:
        raise HTTPException(503, "PostgreSQL 없음")
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM search_golden_set WHERE id=%s", (item_id,))
        conn.commit()
        conn.close()
        return {"deleted": item_id}
    except Exception as e:
        conn.rollback()
        conn.close()
        raise HTTPException(500, str(e)) from e


def _embed_query(query: str) -> list[float]:
    from google import genai
    client = genai.Client(
        project=os.environ.get("GOOGLE_CLOUD_PROJECT", ""),
        location=os.environ.get("VERTEX_AI_LOCATION", "us-east5"),
        vertexai=True,
    )
    res = client.models.embed_content(
        model="text-multilingual-embedding-002", contents=[query]
    )
    return list(res.embeddings[0].values)


def _qdrant_search(collection: str, vec: list[float], top_k: int):
    from qdrant_client import QdrantClient
    result = QdrantClient(url=QDRANT_URL).query_points(
        collection_name=collection,
        query=vec,
        limit=top_k,
        with_payload=True,
    )
    return result.points   # ScoredPoint 리스트 반환 (deprecated .search() 대체)


@app.post("/api/golden/run")
def golden_run(dept: str = "strategic"):
    conn = _pg_conn()
    if not conn:
        raise HTTPException(503, "PostgreSQL 없음")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, query, expected, top_k FROM search_golden_set WHERE dept=%s ORDER BY id",
                (dept,),
            )
            items = cur.fetchall()
    except Exception as e:
        conn.close()
        raise HTTPException(500, str(e)) from e

    if not items:
        conn.close()
        return {"total": 0, "passed": 0, "failed": 0, "avg_score": None, "detail": []}

    try:
        from dept_config import load_dept
        collection = load_dept(dept)["qdrant_collection"]
    except Exception as e:
        conn.close()
        raise HTTPException(500, f"dept_config 오류: {e}") from e

    detail = []
    scores = []
    for (gid, query, expected, top_k) in items:
        try:
            vec  = _embed_query(query)
            hits = _qdrant_search(collection, vec, top_k)
            result_titles = [h.payload.get("title", "") for h in hits]
            result_scores = [round(h.score, 4) for h in hits]

            # 기대 제목 중 하나라도 결과 제목에 포함되면 Pass (부분 매칭)
            matched = [
                exp for exp in expected
                if any(exp.lower() in rt.lower() or rt.lower() in exp.lower()
                       for rt in result_titles)
            ]
            passed      = len(matched) > 0
            best_score  = result_scores[0] if result_scores else 0.0
            scores.append(best_score)
            detail.append({
                "golden_id": gid,
                "query":     query,
                "passed":    passed,
                "score":     best_score,
                "matched":   matched,
                "expected":  expected,
                "results":   [
                    {"title": t, "score": s}
                    for t, s in zip(result_titles, result_scores)
                ],
            })
        except Exception as ex:
            detail.append({
                "golden_id": gid,
                "query":     query,
                "passed":    False,
                "score":     0.0,
                "matched":   [],
                "expected":  expected,
                "error":     str(ex),
                "results":   [],
            })

    total  = len(detail)
    passed = sum(1 for d in detail if d["passed"])
    failed = total - passed
    avg_score = round(sum(scores) / len(scores), 4) if scores else None

    # 실행 이력 저장
    try:
        import json as _json
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO golden_run_log (dept, total, passed, failed, avg_score, detail) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (dept, total, passed, failed, avg_score, _json.dumps(detail, ensure_ascii=False)),
            )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()

    return {"total": total, "passed": passed, "failed": failed,
            "avg_score": avg_score, "detail": detail}


@app.post("/api/golden/generate")
def golden_generate(dept: str = "strategic", count: int = 5):
    """인제스트된 페이지 제목을 기반으로 골든셋 케이스를 LLM이 자동 생성."""
    conn = _pg_conn()
    if not conn:
        raise HTTPException(503, "PostgreSQL 없음")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT page_id, title, notion_url FROM notion_pages "
                "WHERE dept=%s AND status='ok' AND word_count >= 30 AND title IS NOT NULL "
                "ORDER BY RANDOM() LIMIT %s",
                (dept, min(count, 30)),
            )
            pages = _rows(cur)
        conn.close()
    except Exception as e:
        conn.close()
        raise HTTPException(500, str(e)) from e

    if not pages:
        return {"cases": [], "error": "인제스트된 페이지가 없습니다. (ingest 먼저 실행하세요)"}

    page_list = "\n".join(
        f"{i + 1}. {p['title']}" for i, p in enumerate(pages)
    )
    prompt = f"""아래는 사내 업무 문서 목록입니다.
각 문서를 찾기 위해 실무자가 실제로 검색창에 입력할 법한 자연스러운 한국어 검색 쿼리를 하나씩 생성하세요.

생성 규칙:
- 문서 제목을 그대로 복사하지 말 것 (다른 표현으로 바꿀 것)
- 실무자가 궁금해할 내용을 구체적인 질문이나 핵심 키워드로 표현
- 5~30자 이내의 짧고 명확한 표현
- 한국어로만 작성

문서 목록:
{page_list}

반드시 JSON 배열 형식으로만 응답하세요 (마크다운 코드블록, 설명 텍스트 없이):
[{{"index": 1, "query": "..."}}, {{"index": 2, "query": "..."}}, ...]"""

    try:
        import anthropic as _anthropic
        import json as _json

        _model = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
        ac = _anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
        msg = ac.messages.create(
            model=_model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        # 마크다운 코드 펜스 제거
        if text.startswith("```"):
            text = "\n".join(text.split("\n")[1:])
        if text.endswith("```"):
            text = "\n".join(text.split("\n")[:-1])
        generated = _json.loads(text.strip())
    except Exception as e:
        return {"cases": [], "error": f"LLM 생성 실패: {e}"}

    cases = []
    for item in generated:
        idx = int(item.get("index", 1)) - 1
        if 0 <= idx < len(pages):
            p = pages[idx]
            cases.append({
                "query":      item.get("query", "").strip(),
                "expected":   [p["title"]],
                "top_k":      5,
                "notes":      f"자동생성 ← {p['title']}",
                "title":      p["title"],
                "notion_url": p.get("notion_url", ""),
            })

    return {"cases": cases}


@app.get("/api/golden/history")
def golden_history(dept: str = "strategic", limit: int = 10):
    conn = _pg_conn()
    if not conn:
        return {"error": "PostgreSQL 없음", "logs": []}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, dept, total, passed, failed, avg_score, created_at "
                "FROM golden_run_log WHERE dept=%s ORDER BY created_at DESC LIMIT %s",
                (dept, limit),
            )
            logs = _rows(cur)
        conn.close()
        return {"logs": logs}
    except Exception as e:
        conn.close()
        return {"error": str(e), "logs": []}


# ─── HTML ─────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(_HTML)


_HTML = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Semantica Ops</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {
  --bg:      #080d1a;
  --surface: #0d1424;
  --card:    #111827;
  --border:  #1e2d45;
  --border2: #263350;
  --text:    #d1dbe8;
  --muted:   #5b7194;
  --blue:    #3b82f6;
  --blue-d:  #1d4ed8;
  --green:   #22c55e;
  --amber:   #f59e0b;
  --red:     #ef4444;
  --term-bg: #020408;
  --term-fg: #c8d8e8;
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; background: var(--bg); color: var(--text); font-family: 'Inter', system-ui, sans-serif; font-size: 14px; line-height: 1.5; }
a { color: var(--blue); text-decoration: none; }
a:hover { text-decoration: underline; }

/* ── Header ── */
header {
  position: sticky; top: 0; z-index: 100;
  display: flex; align-items: center; gap: 16px;
  padding: 0 24px; height: 52px;
  background: var(--surface); border-bottom: 1px solid var(--border);
}
.logo { font-weight: 700; font-size: 16px; letter-spacing: -.3px; color: #fff; }
.logo span { color: var(--blue); }
.status-dots { display: flex; gap: 12px; margin-left: 8px; }
.dot { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted); font-family: 'JetBrains Mono', monospace; }
.dot::before { content: ''; width: 7px; height: 7px; border-radius: 50%; background: var(--border); flex-shrink: 0; }
.dot.ok::before  { background: var(--green); box-shadow: 0 0 6px var(--green); }
.dot.err::before { background: var(--red); }
.header-right { margin-left: auto; display: flex; align-items: center; gap: 10px; }
.ts { font-size: 11px; color: var(--muted); font-family: 'JetBrains Mono', monospace; }

/* ── Tabs ── */
.tabs { display: flex; padding: 0 24px; background: var(--surface); border-bottom: 1px solid var(--border); }
.tab-btn {
  padding: 10px 18px; font-size: 13px; font-weight: 500;
  color: var(--muted); background: none; border: none; border-bottom: 2px solid transparent;
  cursor: pointer; transition: color .15s, border-color .15s; white-space: nowrap;
}
.tab-btn:hover { color: var(--text); }
.tab-btn.active { color: var(--blue); border-bottom-color: var(--blue); }

/* ── Layout ── */
.tab-content { display: none; padding: 24px; max-width: 1400px; margin: 0 auto; }
.tab-content.active { display: block; }
#tab-batch.active { display: flex; flex-direction: column; }

/* ── Cards ── */
.section-title { font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: .07em; color: var(--muted); margin-bottom: 12px; }
.stats-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 12px; margin-bottom: 24px; }
.stat-card {
  background: var(--card); border: 1px solid var(--border);
  border-radius: 6px; padding: 16px;
}
.stat-card .label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .06em; margin-bottom: 8px; }
.stat-card .value { font-size: 26px; font-weight: 700; font-family: 'JetBrains Mono', monospace; color: #fff; line-height: 1; }
.stat-card .sub   { font-size: 11px; color: var(--muted); margin-top: 4px; }

/* ── Status row ── */
.sys-status { display: flex; gap: 10px; margin-bottom: 24px; flex-wrap: wrap; }
.svc-pill {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 14px; border-radius: 20px; font-size: 12px; font-weight: 500;
  border: 1px solid var(--border); background: var(--card);
}
.svc-pill .ind { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.svc-pill.ok  .ind { background: var(--green); box-shadow: 0 0 5px var(--green); }
.svc-pill.err .ind { background: var(--red); }
.svc-pill.chk .ind { background: var(--amber); }

/* ── Tables ── */
.table-wrap { overflow-x: auto; border: 1px solid var(--border); border-radius: 6px; margin-bottom: 24px; }
table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
th { padding: 8px 12px; text-align: left; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); background: var(--card); border-bottom: 1px solid var(--border); white-space: nowrap; }
td { padding: 8px 12px; border-bottom: 1px solid var(--border); vertical-align: top; font-family: 'JetBrains Mono', monospace; font-size: 12px; }
tr:last-child td { border-bottom: none; }
tr:hover td { background: rgba(59,130,246,.04); }
.badge {
  display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 11px; font-weight: 600; font-family: 'Inter', sans-serif;
}
.badge-ok      { background: rgba(34,197,94,.12);  color: #4ade80; }
.badge-error   { background: rgba(239,68,68,.12);  color: #f87171; }
.badge-skipped { background: rgba(245,158,11,.12); color: #fbbf24; }
.badge-deleted { background: rgba(91,113,148,.15); color: var(--muted); }
.badge-success { background: rgba(34,197,94,.12);  color: #4ade80; }
.badge-failed  { background: rgba(239,68,68,.12);  color: #f87171; }
.badge-partial { background: rgba(245,158,11,.12); color: #fbbf24; }
.badge-dry_run { background: rgba(59,130,246,.12); color: #60a5fa; }

/* ── Buttons ── */
.btn {
  display: inline-flex; align-items: center; gap: 7px;
  padding: 7px 14px; border-radius: 5px; font-size: 13px; font-weight: 500;
  border: 1px solid var(--border); background: var(--card); color: var(--text);
  cursor: pointer; transition: background .12s, border-color .12s;
}
.btn:hover { background: var(--surface); border-color: var(--border2); }
.btn:disabled { opacity: .4; cursor: not-allowed; }
.btn-primary { background: var(--blue); border-color: var(--blue-d); color: #fff; }
.btn-primary:hover { background: #2563eb; }
.btn-danger  { background: rgba(239,68,68,.12); border-color: rgba(239,68,68,.3); color: #f87171; }
.btn-danger:hover { background: rgba(239,68,68,.2); }
.btn-sm { padding: 4px 10px; font-size: 12px; }

/* ── Batch tab ── */
.batch-controls {
  background: var(--card); border: 1px solid var(--border); border-radius: 6px;
  padding: 16px; margin-bottom: 12px;
}
.batch-top { display: flex; align-items: center; gap: 12px; margin-bottom: 14px; flex-wrap: wrap; }
.batch-top label { font-size: 12px; color: var(--muted); }
select {
  background: var(--surface); border: 1px solid var(--border); border-radius: 4px;
  color: var(--text); padding: 5px 10px; font-size: 13px; font-family: inherit;
}
.job-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(195px, 1fr)); gap: 8px; }
.job-btn {
  display: flex; align-items: flex-start; gap: 9px; text-align: left;
  padding: 10px 12px; border-radius: 5px; border: 1px solid var(--border);
  background: var(--surface); color: var(--text); cursor: pointer;
  transition: border-color .12s, background .12s;
}
.job-btn:hover:not(:disabled) { border-color: var(--blue); background: rgba(59,130,246,.06); }
.job-btn:disabled { opacity: .4; cursor: not-allowed; }
.job-btn .icon { font-size: 18px; line-height: 1; flex-shrink: 0; margin-top: 1px; }
.job-btn .info .name { font-size: 12.5px; font-weight: 600; }
.job-btn .info .desc { font-size: 11px; color: var(--muted); margin-top: 2px; }

/* ── Terminal ── */
.terminal-wrap {
  flex: 1; display: flex; flex-direction: column; min-height: 0;
  background: var(--term-bg); border: 1px solid var(--border); border-radius: 6px; overflow: hidden;
}
.terminal-bar {
  display: flex; align-items: center; gap: 10px; padding: 8px 14px;
  background: #0a1020; border-bottom: 1px solid var(--border); flex-shrink: 0;
}
.terminal-bar .tty-dots { display: flex; gap: 6px; }
.tty-dots span { width: 10px; height: 10px; border-radius: 50%; background: var(--border); }
.term-status { font-size: 11px; color: var(--muted); font-family: 'JetBrains Mono', monospace; margin-left: auto; }
.term-status.running { color: var(--amber); }
.term-status.done    { color: var(--green); }
.term-status.error   { color: var(--red); }
#terminal {
  flex: 1; overflow-y: auto; padding: 14px 16px;
  font-family: 'JetBrains Mono', monospace; font-size: 12.5px; line-height: 1.7;
  color: var(--term-fg); white-space: pre-wrap; word-break: break-all;
  min-height: 300px; max-height: 60vh;
}
#terminal .ln-ok   { color: #4ade80; }
#terminal .ln-err  { color: #f87171; }
#terminal .ln-warn { color: #fbbf24; }
#terminal .ln-dim  { color: #384860; }
.no-output { color: var(--muted); font-style: italic; }

/* ── Job history ── */
.job-history { margin-top: 16px; }
.job-row {
  display: flex; align-items: center; gap: 12px; padding: 8px 12px;
  border: 1px solid var(--border); border-radius: 5px; margin-bottom: 6px;
  background: var(--card); font-size: 12px;
}
.job-row .j-label { font-weight: 600; flex: 1; }
.job-row .j-meta  { color: var(--muted); font-family: 'JetBrains Mono', monospace; font-size: 11px; }
.status-badge { padding: 2px 8px; border-radius: 3px; font-size: 11px; font-weight: 600; }
.s-running  { background: rgba(245,158,11,.12); color: #fbbf24; }
.s-done     { background: rgba(34,197,94,.12);  color: #4ade80; }
.s-error    { background: rgba(239,68,68,.12);  color: #f87171; }
.s-cancelled{ background: rgba(91,113,148,.12); color: var(--muted); }

/* ── Search ── */
.search-form { display: flex; gap: 10px; margin-bottom: 20px; flex-wrap: wrap; align-items: flex-end; }
.search-form label { font-size: 12px; color: var(--muted); display: block; margin-bottom: 4px; }
.search-form input[type=text] {
  flex: 1; min-width: 260px; background: var(--surface); border: 1px solid var(--border);
  border-radius: 5px; color: var(--text); padding: 8px 12px; font-size: 13px; font-family: inherit;
}
.search-form input:focus { outline: none; border-color: var(--blue); }
.result-card {
  background: var(--card); border: 1px solid var(--border); border-radius: 6px;
  padding: 14px 16px; margin-bottom: 10px;
}
.result-card .r-header { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.score-bar { display: flex; align-items: center; gap: 8px; }
.score-val { font-family: 'JetBrains Mono', monospace; font-size: 13px; font-weight: 600; color: var(--blue); min-width: 46px; }
.score-track { width: 80px; height: 5px; background: var(--border); border-radius: 3px; overflow: hidden; }
.score-fill { height: 100%; background: var(--blue); border-radius: 3px; }
.r-title { font-weight: 600; font-size: 13.5px; flex: 1; }
.r-meta  { font-size: 11px; color: var(--muted); font-family: 'JetBrains Mono', monospace; margin-bottom: 8px; }
.r-text  { font-size: 12.5px; color: var(--muted); line-height: 1.6; border-left: 2px solid var(--border); padding-left: 10px; }

/* ── Misc ── */
.row-flex { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; margin-bottom: 16px; }
.empty { color: var(--muted); font-size: 13px; padding: 20px 0; }
.err-box { background: rgba(239,68,68,.08); border: 1px solid rgba(239,68,68,.2); border-radius: 5px; padding: 10px 14px; color: #f87171; font-size: 12.5px; margin-bottom: 12px; }
.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
@media (max-width: 900px) { .two-col { grid-template-columns: 1fr; } }
</style>
</head>
<body>

<header>
  <div class="logo">Semantica<span>.</span>Ops</div>
  <div class="status-dots">
    <span class="dot" id="dot-pg" title="PostgreSQL">PG</span>
    <span class="dot" id="dot-qd" title="Qdrant">QD</span>
    <span class="dot" id="dot-fk" title="FalkorDB">FK</span>
  </div>
  <div class="header-right">
    <span class="ts" id="ts-label">—</span>
    <button class="btn btn-sm" onclick="loadStatus()">⟳ 새로고침</button>
  </div>
</header>

<nav class="tabs">
  <button class="tab-btn active" onclick="showTab('dashboard')">📊 현황</button>
  <button class="tab-btn"       onclick="showTab('batch')">⚡ 배치 실행</button>
  <button class="tab-btn"       onclick="showTab('search')">🔍 검색 테스트</button>
  <button class="tab-btn"       onclick="showTab('golden')">🎯 골든셋</button>
</nav>

<!-- ═══════════════ 현황 탭 ═══════════════ -->
<section class="tab-content active" id="tab-dashboard">

  <div class="sys-status" id="sys-status">
    <div class="svc-pill chk"><div class="ind"></div>PostgreSQL</div>
    <div class="svc-pill chk"><div class="ind"></div>Qdrant</div>
    <div class="svc-pill chk"><div class="ind"></div>FalkorDB</div>
  </div>

  <div class="row-flex">
    <label style="font-size:12px;color:var(--muted)">본부:</label>
    <select id="dash-dept" onchange="loadDashboard()"></select>
  </div>

  <div class="stats-grid" id="stats-grid">
    <div class="stat-card"><div class="label">전체 페이지</div><div class="value" id="s-pages">—</div><div class="sub">notion_pages</div></div>
    <div class="stat-card"><div class="label">벡터 청크</div><div class="value" id="s-chunks">—</div><div class="sub">Qdrant points</div></div>
    <div class="stat-card"><div class="label">그래프 노드</div><div class="value" id="s-nodes">—</div><div class="sub">FalkorDB</div></div>
    <div class="stat-card"><div class="label">그래프 엣지</div><div class="value" id="s-edges">—</div><div class="sub">FalkorDB</div></div>
    <div class="stat-card"><div class="label">이벤트</div><div class="value" id="s-events">—</div><div class="sub">:Event 노드</div></div>
    <div class="stat-card"><div class="label">게임</div><div class="value" id="s-games">—</div><div class="sub">:Game 노드</div></div>
  </div>

  <div class="two-col">
    <div>
      <div class="section-title">페이지 상태</div>
      <div id="page-status-cards" style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:20px;"></div>

      <div class="section-title">동기화 이력 (최근 10회)</div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>시각</th><th>수정감지</th><th>처리</th><th>청크+</th><th>소요</th><th>상태</th></tr></thead>
          <tbody id="sync-tbody"><tr><td colspan="6" class="empty">로딩 중...</td></tr></tbody>
        </table>
      </div>
    </div>

    <div>
      <div class="section-title">MCP 도구 사용량</div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>도구</th><th>호출수</th><th>평균ms</th><th>평균결과</th></tr></thead>
          <tbody id="mcp-tbody"><tr><td colspan="4" class="empty">로딩 중...</td></tr></tbody>
        </table>
      </div>

      <div class="section-title" style="margin-top:16px">Qdrant 컬렉션</div>
      <div id="qdrant-info" class="empty">로딩 중...</div>
    </div>
  </div>

  <div style="display:flex;align-items:center;gap:12px;margin-bottom:10px;flex-wrap:wrap">
    <div class="section-title" style="margin-bottom:0">인제스트 페이지 목록</div>
    <div style="position:relative;flex:1;min-width:200px;max-width:360px">
      <input type="text" id="page-search" placeholder="제목 검색 (LIKE)..."
        oninput="debouncedPageSearch()"
        style="width:100%;background:var(--surface);border:1px solid var(--border);border-radius:4px;
               color:var(--text);padding:6px 30px 6px 10px;font-size:13px;font-family:inherit">
      <span id="page-search-clear" onclick="clearPageSearch()"
        style="display:none;position:absolute;right:8px;top:50%;transform:translateY(-50%);
               cursor:pointer;color:var(--muted);font-size:14px">✕</span>
    </div>
    <span id="page-count-label" style="font-size:12px;color:var(--muted);white-space:nowrap"></span>
  </div>
  <div class="table-wrap" style="margin-bottom:8px">
    <table>
      <thead><tr><th>제목</th><th>단어</th><th>청크</th><th>트리플</th><th>이벤트</th><th>마지막수정</th><th>상태</th></tr></thead>
      <tbody id="pages-tbody"><tr><td colspan="7" class="empty">로딩 중...</td></tr></tbody>
    </table>
  </div>
  <div id="pages-pagination" style="display:flex;align-items:center;gap:6px;justify-content:center;margin-bottom:24px;flex-wrap:wrap"></div>
</section>

<!-- ═══════════════ 배치 탭 ═══════════════ -->
<section class="tab-content" id="tab-batch">

  <div class="batch-controls">
    <div class="batch-top">
      <div>
        <label>본부</label>
        <select id="batch-dept"></select>
      </div>
      <div id="running-info" style="display:none;font-size:12px;color:var(--amber);">⏳ 배치 실행 중 — 완료 후 다른 작업을 실행할 수 있습니다.</div>
      <button class="btn btn-danger btn-sm" id="cancel-btn" onclick="cancelJob()" style="display:none;margin-left:auto">✕ 중단</button>
    </div>

    <div class="job-grid">
      <button class="job-btn" data-type="fetch" onclick="runBatch('fetch')">
        <span class="icon">📥</span>
        <span class="info"><span class="name">Notion 전체 수집</span><span class="desc">notion_fetch.py — 모든 페이지 .md 저장</span></span>
      </button>
      <button class="job-btn" data-type="ingest" onclick="runBatch('ingest')">
        <span class="icon">⚡</span>
        <span class="info"><span class="name">인제스천</span><span class="desc">ingest.py — 벡터·그래프 저장</span></span>
      </button>
      <button class="job-btn" data-type="ingest_reset" onclick="runBatch('ingest_reset')">
        <span class="icon">🔁</span>
        <span class="info"><span class="name">인제스천 (전체 초기화)</span><span class="desc">기존 데이터 삭제 후 재인제스천</span></span>
      </button>
      <button class="job-btn" data-type="sync" onclick="runBatch('sync')">
        <span class="icon">🔄</span>
        <span class="info"><span class="name">증분 동기화</span><span class="desc">sync.py — 수정된 페이지만 처리</span></span>
      </button>
      <button class="job-btn" data-type="sync_full" onclick="runBatch('sync_full')">
        <span class="icon">🔃</span>
        <span class="info"><span class="name">전체 재동기화</span><span class="desc">sync.py --full — 모든 페이지 재처리</span></span>
      </button>
      <button class="job-btn" data-type="sync_dry" onclick="runBatch('sync_dry')">
        <span class="icon">👁</span>
        <span class="info"><span class="name">Dry-run 동기화</span><span class="desc">sync.py --dry-run — 저장 없이 확인</span></span>
      </button>
      <button class="job-btn" data-type="reconcile" onclick="runBatch('reconcile')">
        <span class="icon">🗑</span>
        <span class="info"><span class="name">삭제 페이지 정리</span><span class="desc">--reconcile — Notion 404 페이지 제거</span></span>
      </button>
    </div>
  </div>

  <div class="terminal-wrap">
    <div class="terminal-bar">
      <div class="tty-dots"><span></span><span></span><span></span></div>
      <span style="font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--muted);margin-left:8px" id="term-title">터미널</span>
      <span class="term-status" id="term-status">대기 중</span>
    </div>
    <div id="terminal"><span class="no-output">작업을 선택하면 여기에 실시간 출력이 표시됩니다.</span></div>
  </div>

  <div class="job-history">
    <div class="section-title" style="margin-top:16px">실행 이력</div>
    <div id="job-list"><span class="empty">아직 실행한 작업이 없습니다.</span></div>
  </div>
</section>

<!-- ═══════════════ 검색 탭 ═══════════════ -->
<section class="tab-content" id="tab-search">

  <div class="search-form">
    <div>
      <label>본부</label>
      <select id="search-dept"></select>
    </div>
    <div style="flex:1">
      <label>검색어</label>
      <input type="text" id="search-query" placeholder="예: 점검 프로세스 담당자" onkeydown="if(event.key==='Enter')runSearch()">
    </div>
    <div>
      <label>결과 수</label>
      <select id="search-limit">
        <option value="5">5개</option>
        <option value="10">10개</option>
        <option value="20">20개</option>
      </select>
    </div>
    <div style="align-self:flex-end">
      <button class="btn btn-primary" onclick="runSearch()">검색</button>
    </div>
  </div>

  <div id="search-time" style="font-size:11px;color:var(--muted);margin-bottom:12px;font-family:'JetBrains Mono',monospace;"></div>
  <div id="search-results"></div>
</section>

<!-- ═══════════════ 골든셋 탭 ═══════════════ -->
<section class="tab-content" id="tab-golden">

  <div class="row-flex" style="margin-bottom:16px;align-items:flex-end;gap:10px;flex-wrap:wrap">
    <div>
      <label style="font-size:12px;color:var(--muted);display:block;margin-bottom:4px">본부</label>
      <select id="golden-dept" onchange="loadGolden()"></select>
    </div>
    <button class="btn" onclick="toggleAddForm()">+ 직접 추가</button>
    <button class="btn" style="background:rgba(139,92,246,.12);border-color:rgba(139,92,246,.3);color:#a78bfa" onclick="toggleGenPanel()">🤖 자동 생성</button>
    <button class="btn" id="golden-run-btn" onclick="runGolden()" style="margin-left:auto">▶ 전체 실행</button>
  </div>

  <!-- 자동 생성 패널 -->
  <div id="golden-gen-panel" style="display:none;background:var(--card);border:1px solid rgba(139,92,246,.3);border-radius:6px;padding:16px;margin-bottom:16px">
    <div style="font-size:13px;font-weight:600;color:#a78bfa;margin-bottom:12px">🤖 골든셋 자동 생성</div>
    <div style="font-size:12px;color:var(--muted);margin-bottom:14px">
      인제스트된 페이지를 무작위로 샘플링하여 Gemini가 각 문서에 대한 검색 쿼리를 생성합니다.<br>
      생성 결과를 검토하고 원하는 케이스만 선택해 저장하세요.
    </div>
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px;flex-wrap:wrap">
      <label style="font-size:12px;color:var(--muted)">생성 개수</label>
      <select id="gen-count" style="background:var(--surface);border:1px solid var(--border);border-radius:4px;color:var(--text);padding:5px 10px;font-size:13px">
        <option value="3">3개</option>
        <option value="5" selected>5개</option>
        <option value="10">10개</option>
        <option value="20">20개</option>
      </select>
      <button class="btn" id="gen-btn" onclick="generateGolden()"
        style="background:rgba(139,92,246,.15);border-color:rgba(139,92,246,.4);color:#a78bfa">
        🤖 생성하기
      </button>
      <button class="btn btn-sm" onclick="toggleGenPanel()">닫기</button>
    </div>

    <!-- 생성 결과 미리보기 -->
    <div id="gen-preview" style="display:none">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
        <span id="gen-preview-label" style="font-size:12px;color:var(--muted)"></span>
        <button class="btn btn-sm" onclick="selectAllGen(true)">전체 선택</button>
        <button class="btn btn-sm" onclick="selectAllGen(false)">전체 해제</button>
        <button class="btn btn-primary btn-sm" style="margin-left:auto" onclick="saveGeneratedCases()">✅ 선택 항목 저장</button>
      </div>
      <div id="gen-list"></div>
    </div>
  </div>

  <!-- 직접 추가 폼 (접이식) -->
  <div id="golden-add-form" style="display:none;background:var(--card);border:1px solid var(--border);border-radius:6px;padding:16px;margin-bottom:16px">
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px">
      <div>
        <label style="font-size:12px;color:var(--muted);display:block;margin-bottom:4px">테스트 쿼리 *</label>
        <input type="text" id="gf-query" placeholder="예: 점검 프로세스 담당자"
          style="width:100%;background:var(--surface);border:1px solid var(--border);border-radius:4px;color:var(--text);padding:7px 10px;font-size:13px;font-family:inherit">
      </div>
      <div>
        <label style="font-size:12px;color:var(--muted);display:block;margin-bottom:4px">Top-K</label>
        <select id="gf-topk" style="background:var(--surface);border:1px solid var(--border);border-radius:4px;color:var(--text);padding:7px 10px;font-size:13px">
          <option value="3">3</option>
          <option value="5" selected>5</option>
          <option value="10">10</option>
        </select>
      </div>
    </div>
    <div style="margin-bottom:12px">
      <label style="font-size:12px;color:var(--muted);display:block;margin-bottom:4px">기대 문서 제목 * (한 줄에 하나, 부분 매칭)</label>
      <textarea id="gf-expected" rows="3" placeholder="점검 가이드 v2&#10;서버 점검 정책"
        style="width:100%;background:var(--surface);border:1px solid var(--border);border-radius:4px;color:var(--text);padding:7px 10px;font-size:13px;font-family:inherit;resize:vertical"></textarea>
    </div>
    <div style="margin-bottom:14px">
      <label style="font-size:12px;color:var(--muted);display:block;margin-bottom:4px">메모 (선택)</label>
      <input type="text" id="gf-notes" placeholder="예: 신규 점검 정책 적용 후 테스트"
        style="width:100%;background:var(--surface);border:1px solid var(--border);border-radius:4px;color:var(--text);padding:7px 10px;font-size:13px;font-family:inherit">
    </div>
    <div style="display:flex;gap:8px">
      <button class="btn btn-primary" onclick="saveGolden()">저장</button>
      <button class="btn" onclick="toggleAddForm()">취소</button>
    </div>
  </div>

  <!-- 케이스 목록 -->
  <div class="table-wrap" style="margin-bottom:20px">
    <table>
      <thead><tr><th>#</th><th>쿼리</th><th>기대 문서</th><th>Top-K</th><th>메모</th><th></th></tr></thead>
      <tbody id="golden-tbody"><tr><td colspan="6" class="empty">로딩 중...</td></tr></tbody>
    </table>
  </div>

  <!-- 실행 결과 -->
  <div id="golden-result" style="display:none">
    <div class="section-title">실행 결과</div>
    <div id="golden-summary" style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px"></div>
    <div id="golden-detail"></div>
  </div>

  <!-- 실행 이력 -->
  <div style="margin-top:20px">
    <div class="section-title">실행 이력</div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>시각</th><th>총</th><th>Pass</th><th>Fail</th><th>avg score</th></tr></thead>
        <tbody id="golden-history-tbody"><tr><td colspan="5" class="empty">이력 없음</td></tr></tbody>
      </table>
    </div>
  </div>
</section>

<script>
// ── 상태 ─────────────────────────────────────────────────────────────────────
let currentJobId = null;
let pollTimer    = null;
let pollFrom     = 0;

// ── 탭 전환 ──────────────────────────────────────────────────────────────────
function showTab(name) {
  document.querySelectorAll('.tab-content').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  event.currentTarget.classList.add('active');
  if (name === 'dashboard') loadDashboard();
  if (name === 'batch')     { loadDepts('batch-dept'); loadJobs(); }
  if (name === 'search')    loadDepts('search-dept');
  if (name === 'golden')    { loadDepts('golden-dept').then(() => loadGolden()); }
}

// ── 본부 목록 로드 ────────────────────────────────────────────────────────────
async function loadDepts(selId) {
  const sel = document.getElementById(selId);
  if (sel.options.length > 0) return;
  const d = await fetch('/api/depts').then(r => r.json()).catch(() => ({depts:['strategic']}));
  (d.depts || ['strategic']).forEach(dep => {
    const o = document.createElement('option'); o.value = o.text = dep; sel.appendChild(o);
  });
}

// ── 시스템 상태 ───────────────────────────────────────────────────────────────
async function loadStatus() {
  const d = await fetch('/api/status').then(r => r.json()).catch(() => ({}));
  const setDot = (id, ok) => {
    document.getElementById(id).className = 'dot ' + (ok ? 'ok' : 'err');
  };
  setDot('dot-pg', d.postgres);
  setDot('dot-qd', d.qdrant);
  setDot('dot-fk', d.falkordb);
  document.getElementById('ts-label').textContent = d.timestamp ? d.timestamp.slice(0,19).replace('T',' ') : '—';
}

// ── 대시보드 로드 ─────────────────────────────────────────────────────────────
let _pageCurrent = 1;
let _pageSearch  = '';
let _pageDebounceTimer = null;

async function loadDashboard() {
  const dept = document.getElementById('dash-dept').value || 'strategic';
  loadStatus();
  loadDepts('dash-dept');

  _pageCurrent = 1;
  _pageSearch  = '';
  document.getElementById('page-search').value = '';
  document.getElementById('page-search-clear').style.display = 'none';

  const [pages, syncLog, mcpLog, qdrant, graph] = await Promise.all([
    fetch(`/api/pages?dept=${dept}&page=1&per_page=200`).then(r => r.json()).catch(() => ({})),
    fetch(`/api/sync-log?dept=${dept}&limit=10`).then(r => r.json()).catch(() => ({})),
    fetch(`/api/mcp-log?dept=${dept}`).then(r => r.json()).catch(() => ({})),
    fetch('/api/qdrant-stats').then(r => r.json()).catch(() => ({})),
    fetch(`/api/graph-stats?dept=${dept}`).then(r => r.json()).catch(() => ({})),
  ]);

  // 통계 카드
  const stats  = pages.stats || {};
  const totals = pages.totals || {};
  const totalPages = Object.values(stats).reduce((a, b) => a + b, 0);
  document.getElementById('s-pages').textContent  = totalPages;
  document.getElementById('s-chunks').textContent  = fmt(totals.chunks || 0);
  document.getElementById('s-nodes').textContent   = fmt(graph.nodes || 0);
  document.getElementById('s-edges').textContent   = fmt(graph.edges || 0);
  document.getElementById('s-events').textContent  = fmt(graph.events || 0);
  document.getElementById('s-games').textContent   = fmt(graph.games || 0);

  // 페이지 상태 뱃지
  const psc = document.getElementById('page-status-cards');
  psc.innerHTML = '';
  Object.entries(stats).forEach(([st, cnt]) => {
    psc.innerHTML += `<div class="stat-card" style="min-width:100px;padding:10px 14px">
      <div class="label">${st}</div><div class="value" style="font-size:20px">${cnt}</div></div>`;
  });

  // sync 이력
  const sb   = document.getElementById('sync-tbody');
  const logs = syncLog.logs || [];
  sb.innerHTML = logs.length ? logs.map(l => `<tr>
    <td>${fmtDt(l.created_at)}</td>
    <td>${l.modified_found ?? '—'}</td>
    <td>${l.processed ?? '—'}</td>
    <td>+${l.new_chunks ?? 0}</td>
    <td>${l.duration_sec ?? '—'}s</td>
    <td><span class="badge badge-${l.status}">${l.status}</span></td>
  </tr>`).join('') : '<tr><td colspan="6" class="empty">이력 없음</td></tr>';

  // MCP 통계
  const mb     = document.getElementById('mcp-tbody');
  const mstats = mcpLog.stats || [];
  mb.innerHTML = mstats.length ? mstats.map(s => `<tr>
    <td>${s.tool}</td><td>${s.cnt}</td><td>${s.avg_ms ?? '—'}</td><td>${s.avg_results ?? '—'}</td>
  </tr>`).join('') : '<tr><td colspan="4" class="empty">MCP 사용 기록 없음</td></tr>';

  // Qdrant
  const qi   = document.getElementById('qdrant-info');
  const cols = qdrant.collections || [];
  qi.innerHTML = cols.length ? cols.map(c => `
    <div style="display:flex;gap:12px;align-items:center;padding:8px 0;border-bottom:1px solid var(--border)">
      <span style="font-family:'JetBrains Mono',monospace;font-size:12px;flex:1">${c.name}</span>
      <span style="color:var(--blue);font-family:'JetBrains Mono',monospace;font-size:12px">${fmt(c.points)}</span>
      <span style="color:var(--muted);font-size:11px">points</span>
    </div>`).join('') : '<div class="empty">컬렉션 없음</div>';

  // 페이지 목록 (첫 로드)
  renderPageRows(pages);
}

// 검색 디바운스 (300ms)
function debouncedPageSearch() {
  clearTimeout(_pageDebounceTimer);
  _pageDebounceTimer = setTimeout(() => {
    _pageCurrent = 1;
    _pageSearch  = document.getElementById('page-search').value.trim();
    document.getElementById('page-search-clear').style.display = _pageSearch ? '' : 'none';
    loadPages();
  }, 300);
}

function clearPageSearch() {
  document.getElementById('page-search').value = '';
  document.getElementById('page-search-clear').style.display = 'none';
  _pageSearch  = '';
  _pageCurrent = 1;
  loadPages();
}

async function loadPages(page) {
  if (page !== undefined) _pageCurrent = page;
  const dept = document.getElementById('dash-dept').value || 'strategic';
  const url  = `/api/pages?dept=${dept}&page=${_pageCurrent}&per_page=200`
             + (_pageSearch ? `&search=${encodeURIComponent(_pageSearch)}` : '');
  const data = await fetch(url).then(r => r.json()).catch(() => ({}));
  renderPageRows(data);
}

function renderPageRows(data) {
  const pg          = data.pages || [];
  const totalCount  = data.total_count ?? pg.length;
  const perPage     = data.per_page    ?? 200;
  const curPage     = data.page        ?? 1;
  const totalPages  = Math.max(1, Math.ceil(totalCount / perPage));

  // 카운트 레이블
  const from  = (curPage - 1) * perPage + 1;
  const to    = Math.min(curPage * perPage, totalCount);
  const label = totalCount > 0 ? `${from}–${to} / ${totalCount}개` : '0개';
  document.getElementById('page-count-label').textContent = label;

  // 행 렌더링
  const pb = document.getElementById('pages-tbody');
  pb.innerHTML = pg.length ? pg.map(p => `<tr>
    <td><a href="${p.notion_url||'#'}" target="_blank" title="${escHtml(p.notion_url||'')}">${escHtml(p.title||p.page_id||'—')}</a></td>
    <td>${p.word_count ?? '—'}</td>
    <td>${p.chunk_count ?? '—'}</td>
    <td>${p.triplet_count ?? '—'}</td>
    <td>${p.event_count ?? '—'}</td>
    <td>${fmtDt(p.last_edited_time)}</td>
    <td><span class="badge badge-${p.status}">${p.status}</span></td>
  </tr>`).join('')
  : `<tr><td colspan="7" class="empty">${_pageSearch ? '검색 결과 없음' : '페이지 없음 (ingest를 실행하세요)'}</td></tr>`;

  // 페이지네이션
  renderPagination(curPage, totalPages);
}

function renderPagination(cur, total) {
  const el = document.getElementById('pages-pagination');
  if (total <= 1) { el.innerHTML = ''; return; }

  // 표시할 페이지 번호 범위 계산 (최대 7개)
  let start = Math.max(1, cur - 3);
  let end   = Math.min(total, start + 6);
  if (end - start < 6) start = Math.max(1, end - 6);

  const btn = (label, pg, disabled = false, active = false) =>
    `<button class="btn btn-sm${active?' btn-primary':''}"
      style="min-width:32px;${active?'':''}padding:4px 9px"
      onclick="loadPages(${pg})"
      ${disabled?'disabled':''}>
      ${label}
    </button>`;

  let html = btn('‹', cur - 1, cur === 1) + ' ';
  if (start > 1) html += btn('1', 1) + (start > 2 ? '<span style="color:var(--muted);padding:0 4px">…</span>' : '');
  for (let p = start; p <= end; p++) html += btn(p, p, false, p === cur);
  if (end < total) html += (end < total - 1 ? '<span style="color:var(--muted);padding:0 4px">…</span>' : '') + btn(total, total);
  html += ' ' + btn('›', cur + 1, cur === total);

  el.innerHTML = html;
}

// ── 배치 실행 ─────────────────────────────────────────────────────────────────
async function runBatch(type) {
  const dept = document.getElementById('batch-dept').value || 'strategic';

  const resp = await fetch('/api/batch/run', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({type, dept}),
  });
  const data = await resp.json();

  if (!resp.ok) {
    if (data.job_id) {
      alert('이미 실행 중인 배치가 있습니다.\n현재 Job ID: ' + data.job_id);
    } else {
      alert('실행 실패: ' + (data.error || resp.statusText));
    }
    return;
  }

  currentJobId = data.job_id;
  pollFrom = 0;

  const term = document.getElementById('terminal');
  term.innerHTML = '';
  document.getElementById('term-title').textContent = `[${dept}] ${data.label}`;
  setTermStatus('running');
  setJobBtnsDisabled(true);
  document.getElementById('cancel-btn').style.display = '';
  document.getElementById('running-info').style.display = '';

  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(() => pollOutput(data.job_id), 400);
}

async function pollOutput(jobId) {
  if (jobId !== currentJobId) { clearInterval(pollTimer); return; }
  const d = await fetch(`/api/batch/${jobId}/output?from_line=${pollFrom}`)
    .then(r => r.json()).catch(() => null);
  if (!d) return;

  const term = document.getElementById('terminal');
  (d.lines || []).forEach(line => {
    const div = document.createElement('div');
    div.innerHTML = colorize(escHtml(line));
    term.appendChild(div);
    pollFrom++;
  });
  // auto-scroll
  term.scrollTop = term.scrollHeight;

  if (d.status !== 'running') {
    clearInterval(pollTimer);
    pollTimer = null;
    setTermStatus(d.status, d.returncode);
    setJobBtnsDisabled(false);
    document.getElementById('cancel-btn').style.display = 'none';
    document.getElementById('running-info').style.display = 'none';
    loadJobs();
    currentJobId = null;
  }
}

function setTermStatus(status, rc) {
  const el = document.getElementById('term-status');
  const map = {running: '⏳ 실행 중', done: '✅ 완료', error: '❌ 오류', cancelled: '⚠️ 취소됨'};
  el.textContent = map[status] || status;
  el.className = 'term-status ' + status;
}

function setJobBtnsDisabled(disabled) {
  document.querySelectorAll('.job-btn').forEach(b => b.disabled = disabled);
}

async function cancelJob() {
  if (!currentJobId) return;
  await fetch(`/api/batch/${currentJobId}/cancel`, {method:'POST'});
}

async function loadJobs() {
  const d = await fetch('/api/jobs').then(r => r.json()).catch(() => []);
  const el = document.getElementById('job-list');
  if (!d.length) { el.innerHTML = '<span class="empty">아직 실행한 작업이 없습니다.</span>'; return; }
  el.innerHTML = d.slice(0, 10).map(j => `
    <div class="job-row">
      <span class="status-badge s-${j.status}">${j.status}</span>
      <span class="j-label">[${j.dept}] ${j.label || j.type}</span>
      <span class="j-meta">${fmtDt(j.started_at)} ${j.finished_at ? '→ ' + elapsed(j.started_at, j.finished_at) : ''}</span>
      <button class="btn btn-sm" onclick="replayJob('${j.job_id}')">로그 보기</button>
    </div>`).join('');
}

async function replayJob(jobId) {
  const d = await fetch(`/api/batch/${jobId}/output?from_line=0`).then(r => r.json()).catch(() => null);
  if (!d) return;
  const term = document.getElementById('terminal');
  term.innerHTML = '';
  (d.lines || []).forEach(line => {
    const div = document.createElement('div');
    div.innerHTML = colorize(escHtml(line));
    term.appendChild(div);
  });
  term.scrollTop = term.scrollHeight;
  setTermStatus(d.status, d.returncode);
}

// ── 검색 ──────────────────────────────────────────────────────────────────────
async function runSearch() {
  const query = document.getElementById('search-query').value.trim();
  if (!query) return;
  const dept  = document.getElementById('search-dept').value || 'strategic';
  const limit = parseInt(document.getElementById('search-limit').value) || 5;

  document.getElementById('search-results').innerHTML = '<div class="empty">검색 중...</div>';
  document.getElementById('search-time').textContent = '';

  const t0 = Date.now();
  const d = await fetch('/api/search/test', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({query, dept, limit}),
  }).then(r => r.json()).catch(e => ({error: String(e)}));
  const elapsed_ms = Date.now() - t0;

  const el = document.getElementById('search-results');
  if (d.error) { el.innerHTML = `<div class="err-box">${escHtml(d.error)}</div>`; return; }

  document.getElementById('search-time').textContent =
    `${d.results?.length || 0}개 결과 · ${elapsed_ms}ms`;

  const res = d.results || [];
  el.innerHTML = res.length ? res.map(r => `
    <div class="result-card">
      <div class="r-header">
        <div class="score-bar">
          <div class="score-val">${(r.score * 100).toFixed(1)}%</div>
          <div class="score-track"><div class="score-fill" style="width:${r.score*100}%"></div></div>
        </div>
        <div class="r-title">${escHtml(r.title || '제목 없음')}</div>
        <span style="font-size:11px;color:var(--muted);font-family:'JetBrains Mono',monospace">
          ${r.chunk_index+1}/${r.chunk_total}
        </span>
      </div>
      <div class="r-meta"><a href="${r.source_url||'#'}" target="_blank">${escHtml(r.source_url||'—')}</a></div>
      <div class="r-text">${escHtml(r.text || '')}</div>
    </div>`).join('')
  : '<div class="empty">검색 결과가 없습니다.</div>';
}

// ── 유틸 ──────────────────────────────────────────────────────────────────────
function escHtml(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function fmt(n) {
  return Number(n).toLocaleString('ko-KR');
}
function fmtDt(iso) {
  if (!iso) return '—';
  return iso.slice(0,16).replace('T',' ');
}
function elapsed(start, end) {
  const s = Math.round((new Date(end) - new Date(start)) / 1000);
  return s >= 60 ? `${Math.floor(s/60)}m ${s%60}s` : `${s}s`;
}
function colorize(line) {
  if (line.includes('✅') || line.includes('완료') || line.includes('done'))  return `<span class="ln-ok">${line}</span>`;
  if (line.includes('❌') || line.includes('오류') || line.includes('실패'))   return `<span class="ln-err">${line}</span>`;
  if (line.includes('⚠️') || line.includes('경고') || line.includes('건너뜀')) return `<span class="ln-warn">${line}</span>`;
  if (line.startsWith('  ') && !line.trim())                                  return `<span class="ln-dim">${line}</span>`;
  return line;
}

// ── 골든셋 ────────────────────────────────────────────────────────────────────
let _goldenItems = [];

function toggleAddForm() {
  const f = document.getElementById('golden-add-form');
  const isOpen = f.style.display !== 'none';
  f.style.display = isOpen ? 'none' : '';
  // 자동생성 패널과 상호 배타
  if (!isOpen) {
    document.getElementById('golden-gen-panel').style.display = 'none';
    document.getElementById('gf-query').focus();
  }
}

function toggleGenPanel() {
  const p = document.getElementById('golden-gen-panel');
  const isOpen = p.style.display !== 'none';
  p.style.display = isOpen ? 'none' : '';
  // 직접 추가 폼과 상호 배타
  if (!isOpen) document.getElementById('golden-add-form').style.display = 'none';
}

let _generatedCases = [];

async function generateGolden() {
  const dept  = document.getElementById('golden-dept').value || 'strategic';
  const count = document.getElementById('gen-count').value;
  const btn   = document.getElementById('gen-btn');

  btn.disabled     = true;
  btn.textContent  = '⏳ 생성 중...';
  document.getElementById('gen-preview').style.display = 'none';

  const d = await fetch(`/api/golden/generate?dept=${dept}&count=${count}`, {method: 'POST'})
    .then(r => r.json()).catch(e => ({error: String(e)}));

  btn.disabled    = false;
  btn.textContent = '🤖 생성하기';

  if (d.error) { alert('오류: ' + d.error); return; }

  _generatedCases = (d.cases || []).filter(c => c.query);
  if (!_generatedCases.length) { alert('생성된 케이스가 없습니다.'); return; }

  document.getElementById('gen-preview-label').textContent = `${_generatedCases.length}개 생성됨 — 검토 후 저장하세요`;
  document.getElementById('gen-list').innerHTML = _generatedCases.map((c, i) => `
    <div style="display:flex;align-items:flex-start;gap:10px;padding:10px 12px;
                background:var(--surface);border:1px solid var(--border);border-radius:5px;margin-bottom:6px">
      <input type="checkbox" id="gen-chk-${i}" checked style="margin-top:4px;flex-shrink:0;accent-color:var(--blue)">
      <div style="flex:1;min-width:0">
        <div style="margin-bottom:6px">
          <label style="font-size:11px;color:var(--muted);display:block;margin-bottom:2px">쿼리 (수정 가능)</label>
          <input type="text" id="gen-q-${i}" value="${escHtml(c.query)}"
            style="width:100%;background:var(--card);border:1px solid var(--border);border-radius:3px;
                   color:var(--text);padding:5px 8px;font-size:13px;font-family:inherit">
        </div>
        <div style="display:flex;gap:16px;font-size:11px;color:var(--muted)">
          <span>기대 문서: <span style="color:#60a5fa">${escHtml(c.title || '')}</span></span>
          <span>Top-K: ${c.top_k}</span>
        </div>
      </div>
    </div>`).join('');
  document.getElementById('gen-preview').style.display = '';
}

function selectAllGen(val) {
  _generatedCases.forEach((_, i) => {
    const el = document.getElementById(`gen-chk-${i}`);
    if (el) el.checked = val;
  });
}

async function saveGeneratedCases() {
  const dept     = document.getElementById('golden-dept').value || 'strategic';
  const selected = _generatedCases
    .map((c, i) => ({
      ...c,
      query:   (document.getElementById(`gen-q-${i}`)?.value || '').trim(),
      checked: document.getElementById(`gen-chk-${i}`)?.checked ?? false,
    }))
    .filter(c => c.checked && c.query);

  if (!selected.length) { alert('저장할 케이스를 선택하세요.'); return; }

  const btn = document.querySelector('#gen-preview .btn-primary');
  if (btn) { btn.disabled = true; btn.textContent = '저장 중...'; }

  for (const c of selected) {
    await fetch('/api/golden', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({dept, query: c.query, expected: c.expected, top_k: c.top_k, notes: c.notes}),
    });
  }

  if (btn) { btn.disabled = false; btn.textContent = '✅ 선택 항목 저장'; }
  document.getElementById('golden-gen-panel').style.display = 'none';
  document.getElementById('gen-preview').style.display = 'none';
  _generatedCases = [];
  loadGolden();
  alert(`${selected.length}개 저장 완료`);
}

async function loadGolden() {
  const dept = document.getElementById('golden-dept').value || 'strategic';
  const d = await fetch(`/api/golden?dept=${dept}`).then(r => r.json()).catch(() => ({items:[]}));
  _goldenItems = d.items || [];
  renderGoldenTable();
  loadGoldenHistory();
}

function renderGoldenTable() {
  const tb = document.getElementById('golden-tbody');
  if (!_goldenItems.length) {
    tb.innerHTML = '<tr><td colspan="6" class="empty">케이스가 없습니다. + 케이스 추가를 눌러 시작하세요.</td></tr>';
    return;
  }
  tb.innerHTML = _goldenItems.map(item => `<tr>
    <td style="color:var(--muted)">${item.id}</td>
    <td style="max-width:220px">${escHtml(item.query)}</td>
    <td style="max-width:220px;color:var(--muted);font-size:11px">${(item.expected||[]).map(e=>`<span style="display:inline-block;background:rgba(59,130,246,.1);color:#60a5fa;padding:1px 6px;border-radius:3px;margin:1px">${escHtml(e)}</span>`).join(' ')}</td>
    <td>${item.top_k}</td>
    <td style="color:var(--muted);font-size:11px">${escHtml(item.notes||'')}</td>
    <td><button class="btn btn-danger btn-sm" onclick="deleteGolden(${item.id})">🗑</button></td>
  </tr>`).join('');
}

async function saveGolden() {
  const dept     = document.getElementById('golden-dept').value || 'strategic';
  const query    = document.getElementById('gf-query').value.trim();
  const expected = document.getElementById('gf-expected').value.split('\n').map(s=>s.trim()).filter(Boolean);
  const top_k    = parseInt(document.getElementById('gf-topk').value);
  const notes    = document.getElementById('gf-notes').value.trim();

  if (!query)           { alert('쿼리를 입력하세요.'); return; }
  if (!expected.length) { alert('기대 문서 제목을 최소 1개 입력하세요.'); return; }

  const resp = await fetch('/api/golden', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({dept, query, expected, top_k, notes}),
  });
  if (!resp.ok) { alert('저장 실패: ' + (await resp.text())); return; }

  // 폼 초기화
  document.getElementById('gf-query').value    = '';
  document.getElementById('gf-expected').value = '';
  document.getElementById('gf-notes').value    = '';
  document.getElementById('golden-add-form').style.display = 'none';
  loadGolden();
}

async function deleteGolden(id) {
  if (!confirm(`케이스 #${id}를 삭제하시겠습니까?`)) return;
  await fetch(`/api/golden/${id}`, {method: 'DELETE'});
  loadGolden();
}

async function runGolden() {
  const dept = document.getElementById('golden-dept').value || 'strategic';
  if (!_goldenItems.length) { alert('테스트 케이스가 없습니다.'); return; }

  const btn = document.getElementById('golden-run-btn');
  btn.disabled = true;
  btn.textContent = '⏳ 실행 중...';
  document.getElementById('golden-result').style.display = 'none';

  const d = await fetch(`/api/golden/run?dept=${dept}`, {method:'POST'})
    .then(r => r.json()).catch(e => ({error: String(e)}));

  btn.disabled = false;
  btn.textContent = '▶ 전체 실행';

  if (d.error) { alert('오류: ' + d.error); return; }

  // 요약
  const passRate = d.total > 0 ? Math.round(d.passed / d.total * 100) : 0;
  document.getElementById('golden-summary').innerHTML = `
    <div class="stat-card" style="min-width:110px;padding:12px 16px">
      <div class="label">총 케이스</div><div class="value" style="font-size:22px">${d.total}</div>
    </div>
    <div class="stat-card" style="min-width:110px;padding:12px 16px;border-color:rgba(34,197,94,.3)">
      <div class="label">Pass ✅</div><div class="value" style="font-size:22px;color:#4ade80">${d.passed}</div>
    </div>
    <div class="stat-card" style="min-width:110px;padding:12px 16px;border-color:rgba(239,68,68,.3)">
      <div class="label">Fail ❌</div><div class="value" style="font-size:22px;color:#f87171">${d.failed}</div>
    </div>
    <div class="stat-card" style="min-width:110px;padding:12px 16px">
      <div class="label">통과율</div><div class="value" style="font-size:22px">${passRate}%</div>
    </div>
    <div class="stat-card" style="min-width:110px;padding:12px 16px">
      <div class="label">avg score</div><div class="value" style="font-size:22px">${d.avg_score != null ? (d.avg_score*100).toFixed(1)+'%' : '—'}</div>
    </div>`;

  // 상세
  const det = document.getElementById('golden-detail');
  det.innerHTML = (d.detail || []).map(item => `
    <div style="background:var(--card);border:1px solid ${item.passed?'rgba(34,197,94,.25)':'rgba(239,68,68,.25)'};border-radius:6px;padding:14px 16px;margin-bottom:10px">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
        <span style="font-size:18px">${item.passed?'✅':'❌'}</span>
        <span style="font-weight:600;flex:1">${escHtml(item.query)}</span>
        <span style="font-family:'JetBrains Mono',monospace;font-size:12px;color:var(--blue)">${item.score != null ? (item.score*100).toFixed(1)+'%' : '—'}</span>
      </div>
      ${item.error ? `<div class="err-box" style="margin-bottom:8px">${escHtml(item.error)}</div>` : ''}
      <div style="font-size:11px;color:var(--muted);margin-bottom:6px">
        기대: ${(item.expected||[]).map(e=>`<span style="padding:1px 6px;border-radius:3px;background:${(item.matched||[]).includes(e)?'rgba(34,197,94,.15)':'rgba(239,68,68,.1)'};color:${(item.matched||[]).includes(e)?'#4ade80':'#f87171'}">${escHtml(e)}</span>`).join(' ')}
      </div>
      <div style="font-size:11px;color:var(--muted)">검색 결과:
        ${(item.results||[]).slice(0,5).map((r,i)=>`<span style="margin-right:8px">${i+1}. ${escHtml(r.title)} <span style="color:var(--blue)">${(r.score*100).toFixed(1)}%</span></span>`).join('')}
      </div>
    </div>`).join('');

  document.getElementById('golden-result').style.display = '';
  loadGoldenHistory();
}

async function loadGoldenHistory() {
  const dept = document.getElementById('golden-dept').value || 'strategic';
  const d = await fetch(`/api/golden/history?dept=${dept}`).then(r=>r.json()).catch(()=>({logs:[]}));
  const tb = document.getElementById('golden-history-tbody');
  const logs = d.logs || [];
  tb.innerHTML = logs.length ? logs.map(l => `<tr>
    <td>${fmtDt(l.created_at)}</td>
    <td>${l.total}</td>
    <td style="color:#4ade80">${l.passed}</td>
    <td style="color:#f87171">${l.failed}</td>
    <td style="color:var(--blue)">${l.avg_score != null ? (parseFloat(l.avg_score)*100).toFixed(1)+'%' : '—'}</td>
  </tr>`).join('') : '<tr><td colspan="5" class="empty">이력 없음</td></tr>';
}

// ── 초기화 ────────────────────────────────────────────────────────────────────
(async () => {
  await loadDepts('dash-dept');
  loadStatus();
  loadDashboard();
  setInterval(loadStatus, 30000);
})();
</script>
</body>
</html>"""


# ─── 엔트리포인트 ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="Semantica 웹 운영 대시보드")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    print(f"\n🌐  Semantica Ops Dashboard — http://{args.host}:{args.port}\n")
    uvicorn.run(
        "web_app:app" if args.reload else app,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
