#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Sementica — Ubuntu GCP 서버 초기 셋업 스크립트
# 실행: bash setup.sh
# ─────────────────────────────────────────────────────────────────────────────
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🚀 Sementica 서버 셋업"
echo "   디렉토리: $PROJECT_DIR"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ── 1. Python 확인 ────────────────────────────────────────────────────────────
echo ""
echo "[1/5] Python 환경 확인"
PYTHON=$(command -v python3 || command -v python)
PY_VER=$($PYTHON --version 2>&1)
echo "  Python: $PY_VER  ($PYTHON)"

# ── 2. pip 의존성 설치 ────────────────────────────────────────────────────────
echo ""
echo "[2/5] Python 패키지 설치"
$PYTHON -m pip install --upgrade pip --quiet
$PYTHON -m pip install -r "$PROJECT_DIR/requirements.txt"
echo "  ✅ 패키지 설치 완료"

# ── 3. .env 파일 확인 ─────────────────────────────────────────────────────────
echo ""
echo "[3/5] .env 파일 확인"
if [ ! -f "$PROJECT_DIR/.env" ]; then
    echo "  ⚠️  .env 파일이 없습니다."
    echo "  아래 명령으로 생성하세요:"
    echo ""
    echo "    cp $PROJECT_DIR/.env.example $PROJECT_DIR/.env"
    echo "    nano $PROJECT_DIR/.env"
    echo ""
    echo "  필수 값:"
    echo "    GOOGLE_CLOUD_PROJECT=datahub-478802"
    echo "    VERTEX_AI_LOCATION=us-east5"
    echo "    VERTEX_AI_MODEL=claude-sonnet-4-6@default"
    echo "    GOOGLE_APPLICATION_CREDENTIALS=/opt/sementica/service-account-key.json"
    echo "    NOTION_TOKEN=ntn_..."
else
    echo "  ✅ .env 존재"
fi

# ── 4. 서비스 계정 키 확인 ────────────────────────────────────────────────────
echo ""
echo "[4/5] GCP 서비스 계정 키 확인"
KEY_PATH=$(grep GOOGLE_APPLICATION_CREDENTIALS "$PROJECT_DIR/.env" 2>/dev/null | cut -d= -f2 | tr -d '"' | tr -d "'")
if [ -z "$KEY_PATH" ]; then
    KEY_PATH="/opt/sementica/service-account-key.json"
fi
if [ -f "$KEY_PATH" ]; then
    echo "  ✅ 서비스 계정 키 존재: $KEY_PATH"
else
    echo "  ⚠️  서비스 계정 키 없음: $KEY_PATH"
    echo "  로컬에서 SCP로 업로드:"
    echo ""
    echo "    scp C:\\sementica\\service-account-key.json ubuntu@<서버IP>:$KEY_PATH"
    echo ""
fi

# ── 5. Docker 컨테이너 시작 ───────────────────────────────────────────────────
echo ""
echo "[5/5] Docker 컨테이너 시작"
if command -v docker-compose &>/dev/null; then
    COMPOSE_CMD="docker-compose"
elif docker compose version &>/dev/null 2>&1; then
    COMPOSE_CMD="docker compose"
else
    echo "  ❌ docker-compose 를 찾을 수 없습니다."
    echo "  설치: sudo apt-get install docker-compose-plugin"
    exit 1
fi

cd "$PROJECT_DIR"
$COMPOSE_CMD up -d

echo ""
echo "  ⏳ 컨테이너 준비 대기 (5초)..."
sleep 5

# 연결 확인
$PYTHON - <<'EOF'
import urllib.request, sys
try:
    urllib.request.urlopen("http://localhost:6333/collections", timeout=5)
    print("  ✅ Qdrant 응답 확인 (localhost:6333)")
except Exception as e:
    print(f"  ⚠️  Qdrant 미응답: {e}")

try:
    import redis
    r = redis.Redis(host="localhost", port=6379, socket_timeout=3)
    r.ping()
    print("  ✅ FalkorDB 응답 확인 (localhost:6379)")
except Exception as e:
    print(f"  ⚠️  FalkorDB 미응답: {e}")
EOF

# ── 완료 ──────────────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ 셋업 완료"
echo ""
echo "다음 명령으로 인제스천을 실행하세요:"
echo ""
echo "  # 연결 확인만"
echo "  python3 src/pipeline/ingest.py --dry-run"
echo ""
echo "  # 실제 인제스천"
echo "  python3 src/pipeline/ingest.py"
echo ""
echo "  # 기존 데이터 초기화 후 재인제스천"
echo "  python3 src/pipeline/ingest.py --reset"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
