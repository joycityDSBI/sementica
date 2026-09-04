"""
Semantica REST API 서버 — Snowflake External Function 호환
=====================================================

MCP 서버(port 8765)와 별도로 실행되는 순수 REST JSON API 서버입니다.
Snowflake External Function, curl, 일반 HTTP 클라이언트에서 호출할 수 있습니다.

실행:
    python src/mcp/rest_api.py --dept strategic --port 8766

엔드포인트:
    GET  /rest/health              — 헬스 체크
    POST /rest/search              — 벡터 의미 검색
    POST /rest/graph               — 그래프 엔티티 탐색
    POST /rest/events              — 시계열 이벤트 이력
    POST /rest/hybrid              — 벡터+그래프 통합 검색

    POST /snowflake/search         — Snowflake External Function 형식 (벡터)
    POST /snowflake/events         — Snowflake External Function 형식 (이벤트)
    POST /snowflake/hybrid         — Snowflake External Function 형식 (통합)

인증:
    SNOWFLAKE_REST_TOKEN 환경변수 설정 시 Authorization: Bearer <token> 검증.
    미설정 시 인증 없이 동작 (개발/사내망 전용).

Snowflake External Function 입출력 형식:
    요청: {"data": [[row_index, param1, param2, ...]]}
    응답: {"data": [[row_index, {결과 객체}]]}
"""

import argparse
import os
import sys
from pathlib import Path

# ─── 경로 설정 ────────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))                          # server.py
sys.path.insert(0, str(_HERE.parent / "pipeline"))      # dept_config 등
sys.path.insert(0, str(_HERE.parent / "ops"))           # db_logger 등

# ─── .env 로드 ────────────────────────────────────────────────────────────────
_env_path = _HERE.parent.parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# ─── server.py에서 핵심 기능 재사용 ──────────────────────────────────────────
# server.py를 import하면 FastMCP 인스턴스는 생성되지만 서버는 시작되지 않음.
# 모든 검색 함수는 일반 Python 함수이므로 그대로 사용 가능.
import server as _srv  # noqa: E402 (경로 설정 후 import)

# ─── 환경변수 ─────────────────────────────────────────────────────────────────
_REST_TOKEN = os.environ.get("SNOWFLAKE_REST_TOKEN", "")

# ─── Starlette (FastMCP 의존성으로 항상 설치됨) ───────────────────────────────
from starlette.applications import Starlette  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402


# ─── 인증 헬퍼 ────────────────────────────────────────────────────────────────
def _ok(request: Request) -> bool:
    """Bearer 토큰 검증. SNOWFLAKE_REST_TOKEN 미설정 시 항상 통과."""
    if not _REST_TOKEN:
        return True
    return request.headers.get("Authorization", "") == f"Bearer {_REST_TOKEN}"


def _deny() -> JSONResponse:
    return JSONResponse({"error": "Unauthorized"}, status_code=401)


# ─── 헬스 체크 ────────────────────────────────────────────────────────────────
async def health(request: Request):
    return JSONResponse({
        "status":     "ok",
        "dept":       _srv.DEPT_NAME,
        "collection": _srv.COLLECTION_NAME,
        "graph":      _srv.GRAPH_NAME,
    })


# ─── /rest/* 일반 REST 핸들러 ─────────────────────────────────────────────────

async def rest_search(request: Request):
    """
    벡터 의미 검색.
    요청: {"query": "POTC 마케팅 이력", "limit": 5}
    응답: {"results": [...], "count": 5}
    """
    if not _ok(request):
        return _deny()
    try:
        body  = await request.json()
        query = str(body.get("query", "")).strip()
        limit = int(body.get("limit", 5))
        if not query:
            return JSONResponse({"error": "query 파라미터가 필요합니다"}, status_code=400)
        results = _srv.semantic_search(query=query, limit=limit)
        return JSONResponse({"results": results, "count": len(results)})
    except Exception as e:
        return JSONResponse({"error": str(e), "results": [], "count": 0}, status_code=500)


async def rest_graph(request: Request):
    """
    그래프 엔티티 관계 탐색.
    요청: {"entity": "DI팀", "depth": 1}
    응답: {entity, type, found, outgoing:[...], incoming:[...]}
    """
    if not _ok(request):
        return _deny()
    try:
        body   = await request.json()
        entity = str(body.get("entity", "")).strip()
        depth  = int(body.get("depth", 1))
        if not entity:
            return JSONResponse({"error": "entity 파라미터가 필요합니다"}, status_code=400)
        result = _srv.graph_search(entity=entity, depth=depth)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e), "found": False}, status_code=500)


async def rest_events(request: Request):
    """
    시계열 이벤트 이력 조회.
    요청: {"game": "POTC", "event_type": "ua_budget",
           "from_date": "2026-08-01", "to_date": "2026-08-31", "limit": 20}
    응답: {game, total, events:[{date, event_type, title, ...}]}
    """
    if not _ok(request):
        return _deny()
    try:
        body       = await request.json()
        game       = str(body.get("game",       "")).strip()
        event_type = str(body.get("event_type", "")).strip()
        from_date  = str(body.get("from_date",  "")).strip()
        to_date    = str(body.get("to_date",    "")).strip()
        limit      = int(body.get("limit", 20))
        if not game:
            return JSONResponse({"error": "game 파라미터가 필요합니다"}, status_code=400)
        result = _srv.timeline_search(
            game=game, event_type=event_type,
            from_date=from_date, to_date=to_date, limit=limit,
        )
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def rest_hybrid(request: Request):
    """
    벡터 + 그래프 통합 검색.
    요청: {"query": "DS 매출 감소 원인", "limit": 8}
    응답: {semantic_results:[...], graph_results:[...], sub_queries:[...]}
    """
    if not _ok(request):
        return _deny()
    try:
        body  = await request.json()
        query = str(body.get("query", "")).strip()
        limit = int(body.get("limit", 8))
        if not query:
            return JSONResponse({"error": "query 파라미터가 필요합니다"}, status_code=400)
        result = _srv.hybrid_search(query=query, limit=limit)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ─── /snowflake/* Snowflake External Function 형식 핸들러 ─────────────────────
# 요청: {"data": [[row_index, param1, param2, ...]]}
# 응답: {"data": [[row_index, {결과}]]}

async def sf_search(request: Request):
    """
    Snowflake External Function — 벡터 검색.

    Snowflake SQL 예:
        CREATE EXTERNAL FUNCTION search_ontology(query VARCHAR, lim NUMBER)
          RETURNS VARIANT ...
          AS 'http://서버IP:8766/snowflake/search';

        SELECT search_ontology('POTC 마케팅', 5) FROM ...
    """
    if not _ok(request):
        return _deny()
    try:
        body = await request.json()
        rows = body.get("data", [])
        out  = []
        for row in rows:
            idx   = row[0]
            query = str(row[1]).strip() if len(row) > 1 else ""
            limit = int(row[2])         if len(row) > 2 else 5
            try:
                results = _srv.semantic_search(query=query, limit=limit)
                out.append([idx, {"results": results, "count": len(results)}])
            except Exception as e:
                out.append([idx, {"error": str(e), "results": [], "count": 0}])
        return JSONResponse({"data": out})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def sf_events(request: Request):
    """
    Snowflake External Function — 시계열 이벤트 이력.

    Snowflake SQL 예:
        CREATE EXTERNAL FUNCTION get_game_events(
            game VARCHAR, etype VARCHAR, from_dt VARCHAR, to_dt VARCHAR, lim NUMBER)
          RETURNS VARIANT ...
          AS 'http://서버IP:8766/snowflake/events';

        SELECT get_game_events('POTC','ua_budget','2026-08-01','2026-08-31',20)
    """
    if not _ok(request):
        return _deny()
    try:
        body = await request.json()
        rows = body.get("data", [])
        out  = []
        for row in rows:
            idx        = row[0]
            game       = str(row[1]).strip() if len(row) > 1 else ""
            event_type = str(row[2]).strip() if len(row) > 2 else ""
            from_date  = str(row[3]).strip() if len(row) > 3 else ""
            to_date    = str(row[4]).strip() if len(row) > 4 else ""
            limit      = int(row[5])         if len(row) > 5 else 20
            try:
                result = _srv.timeline_search(
                    game=game, event_type=event_type,
                    from_date=from_date, to_date=to_date, limit=limit,
                )
                out.append([idx, result])
            except Exception as e:
                out.append([idx, {"error": str(e)}])
        return JSONResponse({"data": out})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def sf_hybrid(request: Request):
    """
    Snowflake External Function — 통합 검색.

    Snowflake SQL 예:
        CREATE EXTERNAL FUNCTION search_ontology_hybrid(query VARCHAR, lim NUMBER)
          RETURNS VARIANT ...
          AS 'http://서버IP:8766/snowflake/hybrid';

        SELECT search_ontology_hybrid('DS 매출 감소 원인', 8)
    """
    if not _ok(request):
        return _deny()
    try:
        body = await request.json()
        rows = body.get("data", [])
        out  = []
        for row in rows:
            idx   = row[0]
            query = str(row[1]).strip() if len(row) > 1 else ""
            limit = int(row[2])         if len(row) > 2 else 8
            try:
                result = _srv.hybrid_search(query=query, limit=limit)
                out.append([idx, result])
            except Exception as e:
                out.append([idx, {"error": str(e)}])
        return JSONResponse({"data": out})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ─── Starlette 앱 ─────────────────────────────────────────────────────────────
app = Starlette(routes=[
    # 헬스 체크
    Route("/rest/health",      health,      methods=["GET"]),
    # 일반 REST
    Route("/rest/search",      rest_search, methods=["POST"]),
    Route("/rest/graph",       rest_graph,  methods=["POST"]),
    Route("/rest/events",      rest_events, methods=["POST"]),
    Route("/rest/hybrid",      rest_hybrid, methods=["POST"]),
    # Snowflake External Function 형식
    Route("/snowflake/search", sf_search,   methods=["POST"]),
    Route("/snowflake/events", sf_events,   methods=["POST"]),
    Route("/snowflake/hybrid", sf_hybrid,   methods=["POST"]),
])


# ─── 실행 ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description="Semantica REST API 서버")
    parser.add_argument("--dept", default="", help="본부 이름 (config/departments.yaml의 key)")
    parser.add_argument("--host", default="0.0.0.0", help="바인딩 호스트 (기본: 0.0.0.0)")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("SNOWFLAKE_REST_PORT", "8766")),
                        help="포트 (기본: 8766 또는 SNOWFLAKE_REST_PORT 환경변수)")
    args = parser.parse_args()

    # 본부 설정 적용 (COLLECTION_NAME, GRAPH_NAME, DEPT_NAME 갱신)
    if args.dept:
        _srv._load_dept_config(args.dept)

    base = f"http://{args.host}:{args.port}"
    print("=" * 56)
    print("🌐 Semantica REST API 서버 시작")
    print(f"   본부: {_srv.DEPT_NAME}  컬렉션: {_srv.COLLECTION_NAME}")
    print(f"   포트: {args.port}")
    print("")
    print(f"   헬스:  GET  {base}/rest/health")
    print(f"   검색:  POST {base}/rest/search")
    print(f"   그래프: POST {base}/rest/graph")
    print(f"   이벤트: POST {base}/rest/events")
    print(f"   통합:  POST {base}/rest/hybrid")
    print("")
    print(f"   Snowflake: POST {base}/snowflake/{{search|events|hybrid}}")
    print(f"   인증: {'Bearer 토큰 활성화' if _REST_TOKEN else '없음 (SNOWFLAKE_REST_TOKEN 미설정)'}")
    print("=" * 56)

    uvicorn.run(app, host=args.host, port=args.port)
