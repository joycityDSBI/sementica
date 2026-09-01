#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Semantica → GCS 백업 스크립트
# 대상: FalkorDB(Redis) · Qdrant 스냅샷 · Notion .md 페이지 · 설정 파일
#
# 사용법:
#   bash scripts/backup_to_gcs.sh           # 일반 실행
#   bash scripts/backup_to_gcs.sh --dry-run # 업로드 없이 경로 확인만
#
# Cron 등록 (매일 새벽 3시):
#   0 3 * * * /home/ubuntu/sementica/scripts/backup_to_gcs.sh >> /home/ubuntu/sementica/data/logs/backup.log 2>&1
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── 설정 ─────────────────────────────────────────────────────────────────────
BUCKET="gs://sementica-backup"
PROJECT="datahub-478802"
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"    # 프로젝트 루트
BACKUP_TMP="/tmp/sementica_backup"
DATE="$(date +%Y%m%d_%H%M%S)"
PREFIX="${BUCKET}/${DATE}"

QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"
FALKORDB_HOST="${FALKORDB_HOST:-localhost}"
FALKORDB_PORT="${FALKORDB_PORT:-6379}"

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
upload() {
    local src="$1" dst="$2"
    if $DRY_RUN; then
        log "[DRY-RUN] $src → $dst"
    else
        gcloud storage cp --project="$PROJECT" -r "$src" "$dst"
        log "✅ 업로드 완료: $dst"
    fi
}

log "====== Semantica 백업 시작 (${DATE}) ======"
$DRY_RUN && log "[DRY-RUN 모드 — 실제 업로드 없음]"

mkdir -p "$BACKUP_TMP"
trap 'rm -rf "$BACKUP_TMP"' EXIT

# ── 1. FalkorDB (Redis) 백업 ──────────────────────────────────────────────────
log "[1/4] FalkorDB(Redis) 스냅샷 생성"
redis-cli -h "$FALKORDB_HOST" -p "$FALKORDB_PORT" BGSAVE
# BGSAVE가 완료될 때까지 대기 (최대 60초)
for i in $(seq 1 60); do
    status=$(redis-cli -h "$FALKORDB_HOST" -p "$FALKORDB_PORT" LASTSAVE)
    sleep 1
    new_status=$(redis-cli -h "$FALKORDB_HOST" -p "$FALKORDB_PORT" LASTSAVE)
    if [[ "$new_status" != "$status" ]] || [[ $i -eq 60 ]]; then
        break
    fi
done

# dump.rdb 위치 자동 탐색
REDIS_DIR=$(redis-cli -h "$FALKORDB_HOST" -p "$FALKORDB_PORT" CONFIG GET dir | tail -1)
RDB_FILE="${REDIS_DIR}/dump.rdb"
if [[ -f "$RDB_FILE" ]]; then
    cp "$RDB_FILE" "${BACKUP_TMP}/falkordb_dump.rdb"
    upload "${BACKUP_TMP}/falkordb_dump.rdb" "${PREFIX}/falkordb/falkordb_dump.rdb"
else
    log "⚠️  dump.rdb 없음 (경로: ${RDB_FILE}) — 건너뜀"
fi

# AOF 파일도 있으면 백업
AOF_FILE="${REDIS_DIR}/appendonly.aof"
if [[ -f "$AOF_FILE" ]]; then
    cp "$AOF_FILE" "${BACKUP_TMP}/appendonly.aof"
    upload "${BACKUP_TMP}/appendonly.aof" "${PREFIX}/falkordb/appendonly.aof"
fi

# ── 2. Qdrant 스냅샷 ─────────────────────────────────────────────────────────
log "[2/4] Qdrant 컬렉션 스냅샷 생성"
COLLECTIONS=$(curl -sf "${QDRANT_URL}/collections" | python3 -c \
    "import sys,json; data=json.load(sys.stdin); print('\n'.join(c['name'] for c in data['result']['collections']))" \
    2>/dev/null || echo "")

if [[ -z "$COLLECTIONS" ]]; then
    log "⚠️  Qdrant 컬렉션 없음 또는 접근 불가 — 건너뜀"
else
    for COLL in $COLLECTIONS; do
        log "   스냅샷: $COLL"
        SNAP_RESP=$(curl -sf -X POST "${QDRANT_URL}/collections/${COLL}/snapshots" 2>/dev/null || echo "{}")
        SNAP_NAME=$(echo "$SNAP_RESP" | python3 -c \
            "import sys,json; r=json.load(sys.stdin); print(r.get('result',{}).get('name',''))" 2>/dev/null || echo "")

        if [[ -n "$SNAP_NAME" ]]; then
            # 스냅샷 다운로드
            SNAP_LOCAL="${BACKUP_TMP}/qdrant_${COLL}_${DATE}.snapshot"
            curl -sf "${QDRANT_URL}/collections/${COLL}/snapshots/${SNAP_NAME}" \
                -o "$SNAP_LOCAL" 2>/dev/null && \
            upload "$SNAP_LOCAL" "${PREFIX}/qdrant/${COLL}_${DATE}.snapshot"
            # 서버 내 스냅샷 파일 정리 (최근 3개 유지)
            curl -sf -X DELETE "${QDRANT_URL}/collections/${COLL}/snapshots/${SNAP_NAME}" > /dev/null 2>&1 || true
        else
            log "   ⚠️  $COLL 스냅샷 생성 실패 — 건너뜀"
        fi
    done
fi

# ── 3. Notion 페이지 캐시 (.md) ──────────────────────────────────────────────
log "[3/4] Notion 페이지 캐시 업로드"
DATA_DIR="${ROOT_DIR}/data"
if [[ -d "$DATA_DIR" ]]; then
    # notion_pages 디렉토리만 선택적 업로드
    for PAGES_DIR in "${DATA_DIR}"/*/notion_pages; do
        [[ -d "$PAGES_DIR" ]] || continue
        DEPT=$(basename "$(dirname "$PAGES_DIR")")
        upload "${PAGES_DIR}/" "${PREFIX}/notion_pages/${DEPT}/"
        log "   본부: ${DEPT}"
    done
else
    log "⚠️  data/ 디렉토리 없음 — 건너뜀"
fi

# ── 4. 설정 파일 백업 ─────────────────────────────────────────────────────────
log "[4/4] 설정 파일 백업"
CONFIG_TMP="${BACKUP_TMP}/config"
mkdir -p "$CONFIG_TMP"

# .env는 민감 정보 포함 → 키 목록만 기록 (값 제외)
if [[ -f "${ROOT_DIR}/.env" ]]; then
    grep -E '^[A-Z_]+=?' "${ROOT_DIR}/.env" | sed 's/=.*/=<REDACTED>/' \
        > "${CONFIG_TMP}/env_keys_only.txt"
    upload "${CONFIG_TMP}/env_keys_only.txt" "${PREFIX}/config/env_keys_only.txt"
fi

# departments.yaml, systemd 서비스 파일
for f in \
    "${ROOT_DIR}/config/departments.yaml" \
    "/etc/systemd/system/sementica-mcp.service"; do
    [[ -f "$f" ]] && cp "$f" "${CONFIG_TMP}/" && \
        upload "${CONFIG_TMP}/$(basename "$f")" "${PREFIX}/config/$(basename "$f")"
done

# ── 완료 ─────────────────────────────────────────────────────────────────────
log "====== 백업 완료 → ${PREFIX} ======"
$DRY_RUN || log "GCS 확인: gcloud storage ls --project=${PROJECT} ${PREFIX}/"
