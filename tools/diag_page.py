#!/usr/bin/env python3
"""
한 페이지를 파일부터 저장소까지 추적 — "왜 이 문서가 검색되지 않는가"
─────────────────────────────────────────────────────────────────────────────
인제스트는 페이지 단위로 여러 단계를 거치고, 중간에 조용히 멈춰도 요약 로그에는
드러나지 않습니다. 실측 사례: "GBTW 감액 및 ROAS KPI 검토"(6,887단어)가
`status='ok', chunk_count=0` 으로 기록된 채 벡터가 없어, 트리플로는 답이 나오는데
문서 검색으로는 잡히지 않았습니다.

단계별로 어디서 끊겼는지 봅니다:
    ① .md 파일          존재 / 크기
    ② parse_md          frontmatter(page_id·notion_url) · 본문 길이
    ③ 경로 분류          core / defer / excluded
    ④ 청킹              청크 수
    ⑤ 임베딩            실제로 호출해 성공 여부 (--embed)
    ⑥ Qdrant            저장된 청크
    ⑦ FalkorDB          트리플 · 이벤트
    ⑧ notion_pages      레지스트리 기록

실행:
    python tools/diag_page.py --dept strategic --page-id 3c7ea67a568180b4b288fab957019624
    python tools/diag_page.py --dept strategic --title "GBTW 감액"
    python tools/diag_page.py --dept strategic --page-id ... --embed   # 임베딩까지 시도
"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "pipeline"))
sys.path.insert(0, str(ROOT / "src" / "ops"))

_env = ROOT / ".env"
if _env.exists():
    for raw in _env.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
FALKORDB_HOST = os.environ.get("FALKORDB_HOST", "localhost")
FALKORDB_PORT = int(os.environ.get("FALKORDB_PORT", "6379"))
POSTGRES_URL = os.environ.get("POSTGRES_URL", "")


def main() -> int:
    ap = argparse.ArgumentParser(description="페이지 단위 인제스트 추적")
    ap.add_argument("--dept", default="strategic")
    ap.add_argument("--page-id", default="", help="Notion page_id (대시 없는 32자)")
    ap.add_argument("--title", default="", help="제목 일부로 찾기")
    ap.add_argument("--embed", action="store_true", help="임베딩을 실제로 호출해 확인")
    args = ap.parse_args()
    if not args.page_id and not args.title:
        ap.error("--page-id 또는 --title 중 하나가 필요합니다")

    import ingest as ing

    from dept_config import load_dept

    cfg = load_dept(args.dept)
    data_dir = cfg["data_dir"] / "notion_pages"

    # ── ① 파일 찾기 ──────────────────────────────────────────────────────
    print("■ ① .md 파일")
    files = sorted(data_dir.glob("*.md"))
    hits = []
    for f in files:
        meta = ing.parse_md(f)["meta"]
        if (args.page_id and meta.get("page_id") == args.page_id) or (
            args.title and args.title.lower() in (meta.get("title", "") + f.name).lower()
        ):
            hits.append((f, meta))

    if not hits:
        print(f"  ❌ 해당 페이지의 .md 파일이 없습니다 ({data_dir}, 전체 {len(files)}개)")
        print("     → 수집되지 않았거나 삭제되었습니다. notion_fetch 를 다시 돌리세요.")
        return 1
    if len(hits) > 1:
        print(f"  ⚠️  {len(hits)}개 파일이 매칭됩니다 (중복 가능):")
        for f, _m in hits:
            print(f"       {f.name}")
    path, meta = hits[0]
    print(f"  ✅ {path.name}  ({path.stat().st_size:,} 바이트)")

    # ── ② 파싱 ───────────────────────────────────────────────────────────
    page = ing.parse_md(path)
    body = page["body"]
    meta = page["meta"]
    print("\n■ ② parse_md")
    print(f"  page_id     {meta.get('page_id') or '❌ 없음'}")
    print(f"  notion_url  {meta.get('notion_url') or '❌ 없음'}")
    print(f"  title       {meta.get('title', '')[:60]}")
    print(f"  본문        {len(body):,}자 / {len(body.split()):,}단어")
    print(f"  db_properties {list((meta.get('db_properties') or {}).keys()) or '없음'}")
    if not meta.get("page_id"):
        print("  ❌ page_id 가 없으면 레지스트리에 기록되지 않고 검색에서도 제외됩니다")
        print("     → frontmatter 가 깨졌을 수 있습니다 (제목에 '---' 포함 등)")

    # ── ③ 경로 분류 ──────────────────────────────────────────────────────
    wc = len(body.split())
    route = ing.classify_page(body, meta, wc)
    print(f"\n■ ③ 경로 분류: {route}")
    if route == "excluded":
        print("  ❌ excluded — 벡터·트리플 모두 만들지 않습니다")

    # ── ④ 청킹 ───────────────────────────────────────────────────────────
    chunks = ing._make_chunks(body)
    print(f"\n■ ④ 청킹: {len(chunks)}개")
    if not chunks:
        print("  ❌ 청크가 0개 — 본문이 비어 있습니다")
    else:
        print(f"  첫 청크: {chunks[0][:70]!r}")
        print(f"  최장 청크: {max(len(c) for c in chunks):,}자")

    # ── ⑤ 임베딩 ─────────────────────────────────────────────────────────
    if args.embed and chunks:
        print(f"\n■ ⑤ 임베딩 시도 ({len(chunks)}개 청크)")
        try:
            from google import genai

            client = genai.Client(
                project=os.environ.get("GOOGLE_CLOUD_PROJECT", ""),
                location=os.environ.get("VERTEX_AI_LOCATION", "us-east5"),
                vertexai=True,
            )
            titled = [ing._with_title(c, meta.get("title", "")) for c in chunks]
            ok = 0
            for i in range(0, len(titled), ing.EMBED_BATCH_SIZE):
                batch = titled[i : i + ing.EMBED_BATCH_SIZE]
                try:
                    r = client.models.embed_content(model=ing.EMBED_MODEL_NAME, contents=batch)
                    ok += len(r.embeddings)
                except Exception as e:
                    print(
                        f"  ❌ 배치 {i // ing.EMBED_BATCH_SIZE + 1} 실패: {type(e).__name__}: {e}"
                    )
                    # 어느 청크가 문제인지 좁힙니다
                    for j, one in enumerate(batch):
                        try:
                            client.models.embed_content(model=ing.EMBED_MODEL_NAME, contents=[one])
                        except Exception as e2:
                            print(f"       청크 #{i + j} ({len(one)}자) 실패: {e2}")
                            print(f"       내용: {one[:120]!r}")
                    break
            if ok == len(titled):
                print(f"  ✅ {ok}개 전부 성공 — 임베딩은 문제가 아닙니다")
        except Exception as e:
            print(f"  ❌ 클라이언트 초기화 실패: {type(e).__name__}: {e}")

    # ── ⑥ Qdrant ────────────────────────────────────────────────────────
    print("\n■ ⑥ Qdrant")
    try:
        from qdrant_client import QdrantClient
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        qc = QdrantClient(url=QDRANT_URL)
        pts, _ = qc.scroll(
            collection_name=cfg["qdrant_collection"],
            scroll_filter=Filter(
                must=[
                    FieldCondition(key="page_id", match=MatchValue(value=meta.get("page_id", "")))
                ]
            ),
            limit=200,
            with_payload=["chunk_index"],
        )
        if pts:
            print(f"  ✅ 청크 {len(pts)}개 저장됨")
        else:
            print("  ❌ 저장된 청크 없음 — 이 문서는 벡터 검색에 나오지 않습니다")
    except Exception as e:
        print(f"  ⚠️  조회 실패: {type(e).__name__}: {e}")

    # ── ⑦ FalkorDB ──────────────────────────────────────────────────────
    print("\n■ ⑦ FalkorDB")
    try:
        import falkordb

        g = falkordb.FalkorDB(host=FALKORDB_HOST, port=FALKORDB_PORT).select_graph(
            cfg["falkordb_graph"]
        )
        url = meta.get("notion_url", "")
        n_rel = g.query(
            "MATCH ()-[r:REL]->() WHERE r.source_url = $u RETURN count(r)", {"u": url}
        ).result_set[0][0]
        n_ev = g.query(
            "MATCH (e:Event) WHERE e.source_url = $u RETURN count(e)", {"u": url}
        ).result_set[0][0]
        print(f"  트리플 {n_rel}개 / 이벤트 {n_ev}개")
        if n_rel and not pts:
            print("  ⚠️  트리플은 있는데 벡터가 없습니다 — 임베딩·저장 단계에서 끊겼습니다")
    except Exception as e:
        print(f"  ⚠️  조회 실패: {type(e).__name__}: {e}")

    # ── ⑧ notion_pages ──────────────────────────────────────────────────
    print("\n■ ⑧ notion_pages")
    if not POSTGRES_URL:
        print("  (POSTGRES_URL 없음)")
        return 0
    try:
        import psycopg2

        conn = psycopg2.connect(POSTGRES_URL)
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT status, route, word_count, chunk_count, triplet_count, event_count,"
                " content_hash, left(error_msg, 200), last_ingested_at"
                " FROM notion_pages WHERE page_id = %s AND dept = %s",
                (meta.get("page_id", ""), args.dept),
            )
            row = cur.fetchone()
        conn.close()
        if not row:
            print("  ❌ 레지스트리에 없음 — page_id 누락으로 기록이 생략됐을 수 있습니다")
        else:
            st, rt, wc_, cc, tc, ec, ch, err, ts = row
            print(f"  status={st} route={rt} 단어={wc_} 청크={cc} 트리플={tc} 이벤트={ec}")
            print(f"  해시={'있음' if ch else '없음(다음 sync 재처리)'}  최종={ts}")
            if err:
                print(f"  ❌ error_msg: {err}")
    except Exception as e:
        print(f"  ⚠️  조회 실패: {type(e).__name__}: {e}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
