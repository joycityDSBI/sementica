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

# ── REST API 기동 ──────────────────────────────────────────────
# systemd(sementica-rest)가 관리 중이면 건드리지 않습니다. 예전처럼 pkill 로
# 죽이면 systemd 가 Restart=always 로 즉시 되살려서, 이 스크립트가 띄운 것과
# 경합하며 포트를 서로 뺏습니다. deploy/README.md 참고.
REST_MANAGED=0
if systemctl is-active --quiet sementica-rest 2>/dev/null; then
    REST_MANAGED=1
fi

echo "[1/3] 기존 프로세스 정리..."
pkill -f "ngrok http" 2>/dev/null || true
if [ "$REST_MANAGED" -eq 1 ]; then
    echo "  REST API 는 systemd(sementica-rest)가 관리 중 — 그대로 둡니다"
else
    pkill -f "rest_api.py" 2>/dev/null || true
fi
sleep 1

if [ "$REST_MANAGED" -eq 1 ]; then
    echo "[2/3] REST API — systemd 관리 중이므로 기동 생략"
    REST_PID=""
else
    echo "[2/3] REST API 서버 시작 (포트 $REST_PORT)..."
    nohup python src/mcp/rest_api.py --dept "$DEPT" --port "$REST_PORT" \
        > logs/rest_api.log 2>&1 &
    REST_PID=$!
    echo "  PID: $REST_PID"
fi

# 헬스 체크 — 최대 30초까지 기다립니다.
# 예전에는 5초 뒤 한 번만 확인해서, 기동이 조금만 느려져도(임포트 추가·콜드
# 스타트) 정상 기동 중인 서버를 "실패"로 판정하고 종료했습니다.
# 프로세스가 이미 죽었으면 기다리지 않고 바로 로그를 보여줍니다.
HEALTH_OK=0
for i in $(seq 1 30); do
    # systemd 관리 중이면 PID 를 모르므로 헬스만 봅니다.
    if [ -n "$REST_PID" ] && ! kill -0 "$REST_PID" 2>/dev/null; then
        echo "  ❌ REST API 프로세스가 종료되었습니다 (${i}초)"
        break
    fi
    if curl -s --max-time 2 "http://localhost:$REST_PORT/rest/health" | grep -q '"ok"'; then
        HEALTH_OK=1
        echo "  ✅ REST API 정상 가동 (${i}초)"
        break
    fi
    sleep 1
done

if [ "$HEALTH_OK" -ne 1 ]; then
    echo "  ❌ REST API 시작 실패 — logs/rest_api.log 마지막 30줄:"
    echo "  ----------------------------------------------------------"
    tail -30 logs/rest_api.log | sed 's/^/  /'
    echo "  ----------------------------------------------------------"
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
    # 이 스크립트가 띄운 것만 정리합니다. systemd 가 관리 중인 서비스는
    # 터널을 못 여는 것과 무관하게 계속 떠 있어야 합니다.
    [ -n "$REST_PID" ] && kill "$REST_PID" 2>/dev/null
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
if [ "$REST_MANAGED" -eq 1 ]; then
    echo "종료하려면: pkill -f ngrok"
    echo "  (REST API 는 systemd 관리 — 내리려면 sudo systemctl stop sementica-rest)"
else
    echo "종료하려면: pkill -f rest_api.py && pkill -f ngrok"
fi
