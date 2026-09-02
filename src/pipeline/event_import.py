"""
게임/서비스 시계열 이벤트 일괄 임포트
CSV 또는 JSON 파일 → FalkorDB(:Event) + Qdrant 벡터 저장

CSV 컬럼 (헤더 필수):
  game, event_type, date, title, description, target, manager, source_url

event_type 허용 값:
  client_update | server_update | user_event | season |
  content_release | maintenance | incident | kpi_milestone

사용법:
  # CSV 임포트
  python src/pipeline/event_import.py --dept strategic --file data/events/potc_2026.csv

  # JSON 임포트 (list of objects)
  python src/pipeline/event_import.py --dept strategic --file data/events/potc_2026.json

  # 단건 직접 입력
  python src/pipeline/event_import.py --dept strategic \\
      --game POTC --event-type client_update --date 2026-04-12 \\
      --title "클라이언트 업데이트" --description "UI 개편, 버그 수정"

  # 이벤트 목록 조회만
  python src/pipeline/event_import.py --dept strategic --list --game POTC
"""

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

# ─── .env 로드 ────────────────────────────────────────────────────────────────
_env_path = Path(__file__).parent.parent.parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# ─── 경로 설정 ────────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from semantica_helper import EVENT_TYPES, get_event_chain, upsert_event_node

ROOT_DIR  = Path(__file__).parent.parent.parent
LOGS_DIR  = ROOT_DIR / "data" / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# ─── 연결 설정 ────────────────────────────────────────────────────────────────
GCP_PROJECT     = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
LOCATION        = os.environ.get("VERTEX_AI_LOCATION", "us-east5")
EMBED_MODEL     = "text-multilingual-embedding-002"
QDRANT_URL      = os.environ.get("QDRANT_URL", "http://localhost:6333")
FALKORDB_HOST   = os.environ.get("FALKORDB_HOST", "localhost")
FALKORDB_PORT   = int(os.environ.get("FALKORDB_PORT", "6379"))

# ─── 기본값 (--dept 로 덮어씀) ────────────────────────────────────────────────
COLLECTION_NAME = "joycity_pages"
GRAPH_NAME      = "joycity_kg"

# ─── 클라이언트 (지연 초기화) ─────────────────────────────────────────────────
_falkordb_graph = None
_qdrant         = None
_embed_client   = None


def _get_falkordb():
    global _falkordb_graph
    if _falkordb_graph is None:
        import falkordb
        db = falkordb.FalkorDB(host=FALKORDB_HOST, port=FALKORDB_PORT)
        _falkordb_graph = db.select_graph(GRAPH_NAME)
    return _falkordb_graph


def _get_qdrant():
    global _qdrant
    if _qdrant is None:
        from qdrant_client import QdrantClient
        _qdrant = QdrantClient(url=QDRANT_URL)
    return _qdrant


def _get_embed():
    global _embed_client
    if _embed_client is None:
        from google import genai
        _embed_client = genai.Client(project=GCP_PROJECT, location=LOCATION, vertexai=True)
    return _embed_client


# ─── Qdrant 벡터 저장 ─────────────────────────────────────────────────────────
def _store_event_vector(event: dict):
    """이벤트 설명 텍스트를 Qdrant에 벡터로 저장합니다."""
    import uuid as _uuid
    try:
        text = (
            f"{event.get('game','')} {event.get('date','')} {event.get('title','')} "
            f"{event.get('description','')} {event.get('target','')}"
        ).strip()
        if not text:
            return False

        result = _get_embed().models.embed_content(
            model=EMBED_MODEL, contents=[text[:2000]]
        )
        vec = result.embeddings[0].values

        point_id = str(_uuid.uuid5(
            _uuid.NAMESPACE_URL,
            f"event|{event.get('game','')}|{event.get('event_type','')}|{event.get('date','')}",
        ))

        payload = {
            "title":      event.get("title", ""),
            "source_url": event.get("source_url", ""),
            "text":       text,
            "game":       event.get("game", ""),
            "event_type": event.get("event_type", ""),
            "date":       event.get("date", ""),
            "target":     event.get("target", ""),
            "doc_type":   "event",
        }
        qc = _get_qdrant()
        from qdrant_client.models import PointStruct
        qc.upsert(
            collection_name=COLLECTION_NAME,
            points=[PointStruct(id=point_id, vector=vec, payload=payload)],
        )
        return True
    except Exception as e:
        print(f"    ⚠️  Qdrant 벡터 저장 실패: {e}")
        return False


# ─── 이벤트 유효성 검사 ──────────────────────────────────────────────────────
def _validate_event(ev: dict, row_num: int) -> tuple[dict | None, str]:
    """이벤트 딕셔너리를 검증하고 정규화합니다. 실패 시 (None, 오류메시지) 반환."""
    errors = []

    game  = str(ev.get("game",  "")).strip()
    date  = str(ev.get("date",  "")).strip()
    title = str(ev.get("title", "")).strip()

    if not game:
        errors.append("game 필드 필수")
    if not date:
        errors.append("date 필드 필수 (YYYY-MM-DD)")
    if not title:
        errors.append("title 필드 필수")

    event_type = str(ev.get("event_type", "")).strip()
    if event_type and event_type not in EVENT_TYPES:
        errors.append(f"event_type 값 오류: '{event_type}' (허용: {sorted(EVENT_TYPES)})")

    if errors:
        return None, f"행 {row_num}: {', '.join(errors)}"

    return {
        "game":        game,
        "event_type":  event_type or "user_event",
        "date":        date,
        "title":       title,
        "description": str(ev.get("description", "")).strip(),
        "target":      str(ev.get("target",      "")).strip(),
        "manager":     str(ev.get("manager",     "")).strip(),
        "source_url":  str(ev.get("source_url",  "")).strip(),
    }, ""


# ─── 파일 로드 ────────────────────────────────────────────────────────────────
def load_events_from_file(file_path: Path) -> tuple[list[dict], list[str]]:
    """CSV 또는 JSON 파일에서 이벤트 목록을 로드합니다."""
    events  = []
    errors  = []
    raw_rows = []

    ext = file_path.suffix.lower()
    if ext == ".csv":
        with file_path.open(encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            raw_rows = list(reader)
    elif ext == ".json":
        data = json.loads(file_path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            raw_rows = data
        elif isinstance(data, dict) and "events" in data:
            raw_rows = data["events"]
        else:
            errors.append("JSON 형식 오류: 최상위가 배열이거나 {\"events\": [...]} 이어야 합니다")
            return [], errors
    else:
        errors.append(f"지원하지 않는 파일 형식: {ext} (지원: .csv, .json)")
        return [], errors

    for i, row in enumerate(raw_rows, start=1):
        cleaned, err = _validate_event(dict(row), i)
        if cleaned:
            events.append(cleaned)
        else:
            errors.append(err)

    return events, errors


# ─── 임포트 실행 ──────────────────────────────────────────────────────────────
def import_events(
    events:     list[dict],
    embed:      bool = True,
    dry_run:    bool = False,
) -> dict:
    """이벤트 목록을 FalkorDB + (선택) Qdrant에 저장합니다."""
    stored_graph  = 0
    stored_vector = 0
    failed        = 0

    for ev in events:
        label = f"{ev['game']} | {ev['date']} | {ev['title']}"
        if dry_run:
            print(f"  [DRY-RUN] {label}")
            continue

        # ── FalkorDB :Event 노드 ──
        graph = _get_falkordb()
        nid   = upsert_event_node(graph, ev)
        if nid >= 0:
            stored_graph += 1
            print(f"  ✅ 그래프 저장: {label}")
        else:
            failed += 1
            print(f"  ❌ 그래프 실패: {label}")
            continue

        # ── Qdrant 벡터 ──
        if embed:
            ok = _store_event_vector(ev)
            if ok:
                stored_vector += 1
            time.sleep(0.1)  # Vertex AI rate limit

    return {
        "total":         len(events),
        "stored_graph":  stored_graph,
        "stored_vector": stored_vector,
        "failed":        failed,
        "dry_run":       dry_run,
    }


# ─── 이벤트 목록 조회 ─────────────────────────────────────────────────────────
def list_events(
    game:       str,
    event_type: str | None = None,
    from_date:  str | None = None,
    to_date:    str | None = None,
    limit:      int = 50,
):
    """FalkorDB에서 이벤트를 조회해 출력합니다."""
    graph  = _get_falkordb()
    result = get_event_chain(
        graph, game=game, event_type=event_type,
        from_date=from_date, to_date=to_date, limit=limit,
    )

    if not result.get("found"):
        print(f"\n  게임 '{game}'의 이벤트를 찾을 수 없습니다.")
        return

    print(f"\n  게임: {result['game']}  총 {result['total']}건\n")
    print(f"  {'날짜':<12} {'유형':<17} {'제목'}")
    print(f"  {'-'*12} {'-'*17} {'-'*40}")
    for ev in result["events"]:
        print(f"  {ev['date']:<12} {ev['event_type']:<17} {ev['title']}")
        if ev.get("target"):
            print(f"  {'':12} {'':17} → 대상: {ev['target']}")

    print("\n  --- 타임라인 요약 ---")
    for line in result["timeline_summary"]:
        print(f"  {line}")


# ─── 메인 ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="시계열 이벤트 임포트 / 조회",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--dept",       default="",    help="본부 이름")
    parser.add_argument("--file",       default="",    help="임포트할 CSV/JSON 파일 경로")
    parser.add_argument("--no-embed",   action="store_true", help="Qdrant 벡터 저장 건너뜀")
    parser.add_argument("--dry-run",    action="store_true", help="저장 없이 확인만")
    parser.add_argument("--list",       action="store_true", help="이벤트 조회 모드")

    # 단건 직접 입력
    parser.add_argument("--game",        default="", help="게임명")
    parser.add_argument("--event-type",  default="", help="이벤트 유형")
    parser.add_argument("--date",        default="", help="날짜 (YYYY-MM-DD)")
    parser.add_argument("--title",       default="", help="이벤트 제목")
    parser.add_argument("--description", default="", help="이벤트 설명")
    parser.add_argument("--target",      default="", help="대상 유저 세그먼트")
    parser.add_argument("--manager",     default="", help="담당자/팀")
    parser.add_argument("--source-url",  default="", help="출처 URL")

    # 조회 필터
    parser.add_argument("--event-type-filter", default="", help="이벤트 유형 필터 (--list 전용)")
    parser.add_argument("--from-date",  default="", help="조회 시작 날짜")
    parser.add_argument("--to-date",    default="", help="조회 종료 날짜")
    parser.add_argument("--limit",      type=int, default=50, help="최대 조회 건수")

    args = parser.parse_args()

    # ── 본부 설정 로드 ──────────────────────────────────────────────────────
    global COLLECTION_NAME, GRAPH_NAME
    if args.dept:
        from dept_config import load_dept
        cfg = load_dept(args.dept)
        COLLECTION_NAME = cfg["qdrant_collection"]
        GRAPH_NAME      = cfg["falkordb_graph"]
        print(f"\n  본부: {cfg['name']} ({args.dept})")
        print(f"  컬렉션: {COLLECTION_NAME}  그래프: {GRAPH_NAME}")

    print("\n" + "=" * 60)
    print("📅 시계열 이벤트 임포트/조회")
    print("=" * 60)

    # ── 조회 모드 ────────────────────────────────────────────────────────────
    if args.list:
        if not args.game:
            parser.error("--list 모드에서는 --game 이 필요합니다")
        list_events(
            game       = args.game,
            event_type = args.event_type_filter or None,
            from_date  = args.from_date or None,
            to_date    = args.to_date   or None,
            limit      = args.limit,
        )
        return

    # ── 파일 임포트 모드 ──────────────────────────────────────────────────────
    if args.file:
        file_path = Path(args.file)
        if not file_path.exists():
            print(f"❌ 파일 없음: {file_path}")
            sys.exit(1)

        print(f"\n  파일: {file_path}")
        events, errors = load_events_from_file(file_path)

        if errors:
            print(f"\n  ⚠️  유효성 오류 {len(errors)}건:")
            for err in errors:
                print(f"     {err}")

        if not events:
            print("\n  임포트할 이벤트 없음")
            sys.exit(0)

        print(f"\n  임포트 대상: {len(events)}건")
        stats = import_events(events, embed=not args.no_embed, dry_run=args.dry_run)

    # ── 단건 직접 입력 모드 ────────────────────────────────────────────────────
    elif args.game and args.date and args.title:
        event = {
            "game":        args.game,
            "event_type":  args.event_type or "user_event",
            "date":        args.date,
            "title":       args.title,
            "description": args.description,
            "target":      args.target,
            "manager":     args.manager,
            "source_url":  args.source_url,
        }
        cleaned, err = _validate_event(event, 1)
        if not cleaned:
            print(f"❌ 유효성 오류: {err}")
            sys.exit(1)

        print(f"\n  이벤트: {cleaned['game']} | {cleaned['date']} | {cleaned['title']}")
        stats = import_events([cleaned], embed=not args.no_embed, dry_run=args.dry_run)

    else:
        parser.print_help()
        sys.exit(0)

    # ── 결과 요약 ────────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print("  결과 요약")
    print(f"  총 이벤트: {stats['total']}건")
    if not stats["dry_run"]:
        print(f"  그래프 저장: {stats['stored_graph']}건 (:Event/:Game 노드)")
        print(f"  벡터 저장:  {stats['stored_vector']}건 (Qdrant)")
        if stats["failed"]:
            print(f"  실패:       {stats['failed']}건")
    else:
        print("  [DRY-RUN] 실제 저장 없음")


if __name__ == "__main__":
    main()
