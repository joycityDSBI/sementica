#!/bin/bash
# Semantica 자동 동기화 Cron 등록 스크립트
# 사용법: bash setup_cron.sh [--dept strategic] [--hour 2] [--search "프로세스"]

DEPT="strategic"
HOUR="2"
SEARCH=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"
SYNC_SCRIPT="$SCRIPT_DIR/src/pipeline/sync.py"
LOG_FILE="$SCRIPT_DIR/data/logs/sync_cron.log"

# 인수 파싱
while [[ $# -gt 0 ]]; do
  case $1 in
    --dept)   DEPT="$2";   shift 2 ;;
    --hour)   HOUR="$2";   shift 2 ;;
    --search) SEARCH="$2"; shift 2 ;;
    *) shift ;;
  esac
done

# sync.py 명령 조립
SYNC_ARGS="--dept ${DEPT}"
if [[ -n "$SEARCH" ]]; then
  SYNC_ARGS="${SYNC_ARGS} --search '${SEARCH}'"
fi

CRON_CMD="0 ${HOUR} * * * cd ${SCRIPT_DIR} && ${VENV_PYTHON} ${SYNC_SCRIPT} ${SYNC_ARGS} >> ${LOG_FILE} 2>&1"

echo "========================================"
echo "  Semantica Cron 등록"
echo "========================================"
echo "  본부:   $DEPT"
echo "  실행:   매일 새벽 ${HOUR}시"
if [[ -n "$SEARCH" ]]; then
  echo "  필터:   제목에 '${SEARCH}' 포함된 페이지만"
fi
echo "  명령:   $CRON_CMD"
echo ""

# 기존 cron에서 sementica sync 항목 제거 후 새로 추가
(crontab -l 2>/dev/null | grep -v "sync.py --dept ${DEPT}"; echo "$CRON_CMD") | crontab -

echo "  ✅ Cron 등록 완료"
echo ""
echo "  확인: crontab -l"
echo "  로그: tail -f $LOG_FILE"
echo ""
echo "  수동 실행 테스트:"
if [[ -n "$SEARCH" ]]; then
  echo "    $VENV_PYTHON $SYNC_SCRIPT --dept $DEPT --search '${SEARCH}' --dry-run"
else
  echo "    $VENV_PYTHON $SYNC_SCRIPT --dept $DEPT --dry-run"
fi
echo "========================================"
