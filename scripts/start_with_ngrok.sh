#!/usr/bin/env bash
# ================================================================
# Semantica REST API + ngrok HTTPS 터널 동시 시작
# ================================================================
# 사용법:
#   chmod +x scripts/start_with_ngrok.sh
#   ./scripts/start_with_ngrok.sh
#
# 전제 조건:
#   1. ngrok 설치: https://ngrok.com/download
#      wget https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.tgz
#      tar xzf ngrok-v3-stable-linux-amd64.tgz && sudo mv ngrok /usr/local/bin/
#
#   2. ngrok 인증 토큰 등록 (ngrok.com 가입 후 발급):
#      ngrok config add-authtoken <YOUR_TOKEN>
#
# ngrok URL 확인 후 Snowflake SQL 파일에 반영하세요:
#   snowflake/01_api_integration.sql — NGROK_URL_HERE 교체
#   snowflake/02_external_functions.sql — NGROK_URL_HERE 교체
# ================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
DEPT="${DEPT:-strategic}"
REST_PORT="${SNOWFLAKE_REST_PORT:-8766}"

cd "$PROJECT_ROOT"

# ── logs 디렉토리 생성 ─────────────────────────────────────────
mkdir -p logs

# rest_api.py 는 .env 를 직접 읽으므로, 아래 토큰 검사도 같은 값을 봐야 합니다.
if [ -z "${SNOWFLAKE_REST_TOKEN:-}" ] && [ -f .env ]; then
    SNOWFLAKE_REST_TOKEN="$(grep -E '^SNOWFLAKE_REST_TOKEN=' .env | tail -1 | cut -d= -f2- | tr -d ' \r')"
fi

echo "================================================"
echo "  Semantica REST API + ngrok 시작"
echo "  본부: $DEPT  포트: $REST_PORT"
echo "================================================"

# ── 기존 프로세스 정리 ─────────────────────────────────────────
echo "[1/3] 기존 프로세스 정리..."
pkill -f "rest_api.py" 2>/dev/null || true
pkill -f "ngrok http"  2>/dev/null || true
sleep 1

# ── REST API 백그라운드 시작 ───────────────────────────────────
echo "[2/3] REST API 서버 시작 (포트 $REST_PORT)..."
nohup python src/mcp/rest_api.py --dept "$DEPT" --port "$REST_PORT" \
    > logs/rest_api.log 2>&1 &
REST_PID=$!
echo "  PID: $REST_PID"
sleep 5

# 헬스 체크
if curl -s "http://localhost:$REST_PORT/rest/health" | grep -q '"ok"'; then
    echo "  ✅ REST API 정상 가동"
else
    echo "  ❌ REST API 시작 실패 — logs/rest_api.log 확인"
    exit 1
fi

# ── ngrok HTTPS 터널 시작 ─────────────────────────────────────
# 공개 인터넷에 여는 것이므로 토큰이 없으면 여기서 멈춥니다.
# 의도적으로 무인증 공개가 필요하면 ALLOW_UNAUTHENTICATED_NGROK=1 로 실행하세요.
if [ -z "${SNOWFLAKE_REST_TOKEN:-}" ] && [ -z "${ALLOW_UNAUTHENTICATED_NGROK:-}" ]; then
    echo "  ❌ SNOWFLAKE_REST_TOKEN 이 없습니다."
    echo "     ngrok 은 공개 URL 이라 토큰 없이 열면 사내 문서 전체가 무인증 공개됩니다."
    echo "     .env 에 SNOWFLAKE_REST_TOKEN 을 설정하고,"
    echo "     snowflake/01_network_access.sql 의 SECRET 에도 같은 값을 넣으세요."
    echo "     (그래도 열려면 ALLOW_UNAUTHENTICATED_NGROK=1)"
    kill "$REST_PID" 2>/dev/null || true
    exit 1
fi

echo "[3/3] ngrok HTTPS 터널 시작..."
nohup ngrok http "$REST_PORT" \
    --log=stdout \
    > logs/ngrok.log 2>&1 &
NGROK_PID=$!
echo "  PID: $NGROK_PID"
sleep 3

# ngrok URL 추출
NGROK_URL=$(curl -s http://localhost:4040/api/tunnels \
    | python3 -c "import sys,json; t=json.load(sys.stdin)['tunnels']; print([x['public_url'] for x in t if x['proto']=='https'][0])" 2>/dev/null || echo "")

echo ""
echo "================================================"
if [ -n "$NGROK_URL" ]; then
    echo "  ✅ ngrok HTTPS URL:"
    echo "     $NGROK_URL"
    echo ""
    echo "  📋 Snowflake SQL 파일 수정이 필요합니다:"
    echo "     snowflake/01_api_integration.sql"
    echo "     → NGROK_URL_HERE 를 아래 값으로 교체:"
    echo "        ${NGROK_URL#https://}"
    echo ""
    echo "  헬스 확인:"
    echo "     curl $NGROK_URL/rest/health"
else
    echo "  ⚠ ngrok URL을 자동 감지하지 못했습니다."
    echo "  logs/ngrok.log 또는 http://localhost:4040 에서 확인하세요."
fi
echo "================================================"
echo ""
echo "종료하려면: pkill -f rest_api.py && pkill -f ngrok"
