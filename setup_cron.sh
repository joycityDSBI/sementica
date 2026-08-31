#!/bin/bash
# Semantica 자동 동기화 + 백업 Cron 등록 스크립트
# 사용법:
#   bash setup_cron.sh [--dept strategic] [--hour 2] [--search "프로세스"]
#   bash setup_cron.sh --backup-only   # 백업 cron만 등록
#   bash setup_cron.sh --no-backup     # 동기화 cron만 등록 (백업 제외)

DEPT="strategic"
HOUR="2"
BACKUP_HOUR="3"   # 백업 실행 시각 (동기화 1시간 뒤)
SEARCH=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"
SYNC_SCRIPT="$SCRIPT_DIR/src/pipeline/sync.py"
BACKUP_SCRIPT="$SCRIPT_DIR/scripts/backup.sh"
LOG_FILE="$SCRIPT_DIR/data/logs/sync_cron.log"
BACKUP_LOG="$SCRIPT_DIR/data/logs/backup.log"

DO_SYNC=true
DO_BACKUP=true

# 인수 파싱
while [[ $# -gt 0 ]]; do
  case $1 in
    --dept)         DEPT="$2";        shift 2 ;;
    --hour)         HOUR="$2";        shift 2 ;;
    --backup-hour)  BACKUP_HOUR="$2"; shift 2 ;;
    --search)       SEARCH="$2";      shift 2 ;;
    --backup-only)  DO_SYNC=false;    shift ;;
    --no-backup)    DO_BACKUP=false;  shift ;;
    *) shift ;;
  esac
done

# ── 동기화 Cron 구성 ──────────────────────────────────────────────────────────
SYNC_ARGS="--dept ${DEPT}"
if [[ -n "$SEARCH" ]]; then
  SYNC_ARGS="${SYNC_ARGS} --search '${SEARCH}'"
fi
SYNC_CRON="0 ${HOUR} * * * cd ${SCRIPT_DIR} && ${VENV_PYTHON} ${SYNC_SCRIPT} ${SYNC_ARGS} >> ${LOG_FILE} 2>&1"

# ── 백업 Cron 구성 ────────────────────────────────────────────────────────────
BACKUP_CRON="0 ${BACKUP_HOUR} * * * cd ${SCRIPT_DIR} && bash ${BACKUP_SCRIPT} >> ${BACKUP_LOG} 2>&1"

echo "========================================"
echo "  Semantica Cron 등록"
echo "========================================"

# 현재 crontab 로드
CURRENT_CRON=$(crontab -l 2>/dev/null || echo "")

NEW_CRON="$CURRENT_CRON"

if $DO_SYNC; then
  echo "  [동기화]"
  echo "  본부:   $DEPT"
  echo "  실행:   매일 새벽 ${HOUR}시"
  if [[ -n "$SEARCH" ]]; then
    echo "  필터:   '${SEARCH}' 포함 페이지만"
  fi
  echo "  명령:   $SYNC_CRON"
  echo ""
  # 기존 sync 항목 제거 후 추가
  NEW_CRON=$(echo "$NEW_CRON" | grep -v "sync.py --dept ${DEPT}")
  NEW_CRON="${NEW_CRON}"$'\n'"${SYNC_CRON}"
fi

if $DO_BACKUP; then
  echo "  [백업]"
  echo "  실행:   매일 새벽 ${BACKUP_HOUR}시 (동기화 완료 후)"
  echo "  대상:   Qdrant + FalkorDB + PostgreSQL"
  echo "  보존:   7일 (로컬) / GCS_BUCKET 설정 시 30일"
  echo "  명령:   $BACKUP_CRON"
  echo ""
  # 기존 backup.sh 항목 제거 후 추가
  NEW_CRON=$(echo "$NEW_CRON" | grep -v "backup.sh")
  NEW_CRON="${NEW_CRON}"$'\n'"${BACKUP_CRON}"
fi

# crontab 적용 (빈 줄 정리)
echo "$NEW_CRON" | grep -v '^$' | crontab -

echo "  ✅ Cron 등록 완료"
echo ""
echo "  확인:     crontab -l"
echo "  동기화 로그: tail -f $LOG_FILE"
echo "  백업 로그:  tail -f $BACKUP_LOG"
echo ""
echo "  수동 실행:"
if [[ -n "$SEARCH" ]]; then
  echo "    # 동기화 (dry-run)"
  echo "    $VENV_PYTHON $SYNC_SCRIPT --dept $DEPT --search '${SEARCH}' --dry-run"
fi
echo "    # 백업 즉시 실행"
echo "    bash $BACKUP_SCRIPT"
echo "    # 복구 절차 확인"
echo "    bash $BACKUP_SCRIPT --restore"
echo "========================================"
