#!/usr/bin/env bash
# =============================================================================
# Semantica 백업 스크립트
# 대상: Qdrant (벡터 DB) + FalkorDB (그래프 DB) + PostgreSQL (운영 로그)
#
# 사용법:
#   bash scripts/backup.sh                  # 전체 백업
#   bash scripts/backup.sh --qdrant-only    # Qdrant 만
#   bash scripts/backup.sh --falkordb-only  # FalkorDB 만
#   bash scripts/backup.sh --postgres-only  # PostgreSQL 만
#   bash scripts/backup.sh --restore        # 복구 가이드 출력
#
# 설치 (cron):
#   0 3 * * * cd ~/sementica && bash scripts/backup.sh >> data/logs/backup.log 2>&1
#
# 환경변수 (.env):
#   QDRANT_URL=http://localhost:6333
#   QDRANT_COLLECTION=strategic_pages
#   FALKORDB_HOST=localhost
#   FALKORDB_PORT=6379
#   POSTGRES_URL=postgresql://user:pass@host:5432/dbname
#   GCS_BUCKET=gs://joycity-sementica-backup    # GCS 업로드 시 설정
#   BACKUP_RETENTION_DAYS=7                      # 로컬 보존 일수 (기본 7)
# =============================================================================

set -euo pipefail

# ── 설정 ──────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$ROOT_DIR/.env"

# .env 로드
if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    set -a; source "$ENV_FILE"; set +a
fi

QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"
FALKORDB_HOST="${FALKORDB_HOST:-localhost}"
FALKORDB_PORT="${FALKORDB_PORT:-6379}"
POSTGRES_URL="${POSTGRES_URL:-}"
GCS_BUCKET="${GCS_BUCKET:-}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"

BACKUP_BASE="$ROOT_DIR/data/backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="$BACKUP_BASE/$TIMESTAMP"

mkdir -p "$BACKUP_DIR"

# 로그 함수
log()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
ok()   { echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✅ $*"; }
warn() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] ⚠️  $*"; }
err()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] ❌ $*" >&2; }

# ── Qdrant 백업 ───────────────────────────────────────────────────────────────
backup_qdrant() {
    log "Qdrant 백업 시작..."
    local qdrant_dir="$BACKUP_DIR/qdrant"
    mkdir -p "$qdrant_dir"

    # 컬렉션 목록 조회
    local collections
    collections=$(curl -sf "$QDRANT_URL/collections" | python3 -c \
        "import sys,json; data=json.load(sys.stdin); \
         print('\n'.join(c['name'] for c in data['result']['collections']))" 2>/dev/null || echo "")

    if [[ -z "$collections" ]]; then
        warn "Qdrant 컬렉션 없음 또는 연결 실패"
        return 1
    fi

    local ok_count=0
    while IFS= read -r collection; do
        [[ -z "$collection" ]] && continue
        log "  컬렉션 스냅샷 생성: $collection"

        # 스냅샷 생성 요청
        local snap_resp
        snap_resp=$(curl -sf -X POST "$QDRANT_URL/collections/$collection/snapshots" \
            -H "Content-Type: application/json" 2>/dev/null || echo "")

        if [[ -z "$snap_resp" ]]; then
            err "  스냅샷 생성 실패: $collection"
            continue
        fi

        # 스냅샷 이름 추출
        local snap_name
        snap_name=$(echo "$snap_resp" | python3 -c \
            "import sys,json; print(json.load(sys.stdin)['result']['name'])" 2>/dev/null || echo "")

        if [[ -z "$snap_name" ]]; then
            err "  스냅샷 이름 파싱 실패: $collection"
            continue
        fi

        # 스냅샷 다운로드
        local out_file="$qdrant_dir/${collection}_${TIMESTAMP}.snapshot"
        curl -sf "$QDRANT_URL/collections/$collection/snapshots/$snap_name" \
            -o "$out_file" 2>/dev/null

        if [[ -f "$out_file" ]]; then
            local size
            size=$(du -sh "$out_file" | cut -f1)
            ok "  $collection → $out_file ($size)"
            ok_count=$((ok_count + 1))

            # 원격 스냅샷 정리 (서버 디스크 절약)
            curl -sf -X DELETE \
                "$QDRANT_URL/collections/$collection/snapshots/$snap_name" >/dev/null 2>&1 || true
        else
            err "  스냅샷 다운로드 실패: $collection"
        fi
    done <<< "$collections"

    ok "Qdrant 백업 완료 ($ok_count개 컬렉션)"
}

# ── FalkorDB 백업 ─────────────────────────────────────────────────────────────
backup_falkordb() {
    log "FalkorDB 백업 시작..."
    local falkor_dir="$BACKUP_DIR/falkordb"
    mkdir -p "$falkor_dir"

    # BGSAVE 트리거 (비동기 스냅샷)
    log "  BGSAVE 트리거..."
    redis-cli -h "$FALKORDB_HOST" -p "$FALKORDB_PORT" BGSAVE >/dev/null 2>&1 || {
        # Docker 컨테이너 내부에서 실행 시도
        docker exec falkordb redis-cli BGSAVE >/dev/null 2>&1 || {
            warn "  BGSAVE 실패 — redis-cli 또는 docker 접근 불가"
        }
    }

    # BGSAVE 완료 대기 (최대 60초)
    local wait=0
    while [[ $wait -lt 60 ]]; do
        local status
        status=$(redis-cli -h "$FALKORDB_HOST" -p "$FALKORDB_PORT" LASTSAVE 2>/dev/null || \
                 docker exec falkordb redis-cli LASTSAVE 2>/dev/null || echo "0")
        sleep 2
        local status2
        status2=$(redis-cli -h "$FALKORDB_HOST" -p "$FALKORDB_PORT" LASTSAVE 2>/dev/null || \
                  docker exec falkordb redis-cli LASTSAVE 2>/dev/null || echo "0")
        if [[ "$status2" != "$status" ]] || [[ $wait -gt 10 ]]; then
            break
        fi
        wait=$((wait + 2))
    done

    # dump.rdb 복사 (Docker 볼륨 or 직접 경로)
    local rdb_src=""
    # 1) Docker 컨테이너에서 직접 복사 시도
    if docker cp falkordb:/data/dump.rdb "$falkor_dir/dump_${TIMESTAMP}.rdb" 2>/dev/null; then
        rdb_src="docker"
    # 2) 볼륨 마운트 경로에서 복사 시도 (docker-compose 볼륨 기본 경로)
    elif [[ -f "/var/lib/docker/volumes/sementica_falkordb_data/_data/dump.rdb" ]]; then
        cp "/var/lib/docker/volumes/sementica_falkordb_data/_data/dump.rdb" \
           "$falkor_dir/dump_${TIMESTAMP}.rdb"
        rdb_src="volume"
    fi

    if [[ -f "$falkor_dir/dump_${TIMESTAMP}.rdb" ]]; then
        local size
        size=$(du -sh "$falkor_dir/dump_${TIMESTAMP}.rdb" | cut -f1)
        ok "FalkorDB dump.rdb 백업 완료 ($size) [소스: ${rdb_src:-unknown}]"
    else
        warn "FalkorDB RDB 파일 접근 실패 — AOF 활성화 여부 확인 필요"
        log "  → 수동 복사: docker cp falkordb:/data/dump.rdb $falkor_dir/"
    fi

    # AOF 파일도 백업 (있다면)
    docker cp falkordb:/data/appendonly.aof \
        "$falkor_dir/appendonly_${TIMESTAMP}.aof" 2>/dev/null && \
        ok "FalkorDB AOF 백업 완료" || true
}

# ── PostgreSQL 백업 ───────────────────────────────────────────────────────────
backup_postgres() {
    if [[ -z "$POSTGRES_URL" ]]; then
        warn "POSTGRES_URL 미설정 — PostgreSQL 백업 건너뜀"
        return 0
    fi

    log "PostgreSQL 백업 시작..."
    local pg_dir="$BACKUP_DIR/postgres"
    mkdir -p "$pg_dir"

    local out_file="$pg_dir/ops_log_${TIMESTAMP}.sql.gz"
    if pg_dump "$POSTGRES_URL" --no-owner --no-acl \
        -t mcp_request_log -t sync_log 2>/dev/null | gzip > "$out_file"; then
        local size
        size=$(du -sh "$out_file" | cut -f1)
        ok "PostgreSQL 백업 완료 → $out_file ($size)"
    else
        err "PostgreSQL pg_dump 실패"
        return 1
    fi
}

# ── GCS 업로드 ────────────────────────────────────────────────────────────────
upload_gcs() {
    if [[ -z "$GCS_BUCKET" ]]; then
        log "GCS_BUCKET 미설정 — 로컬 백업만 유지"
        return 0
    fi

    log "GCS 업로드 시작: $GCS_BUCKET/sementica/$TIMESTAMP/"
    if gsutil -m cp -r "$BACKUP_DIR/" "$GCS_BUCKET/sementica/$TIMESTAMP/" 2>/dev/null; then
        ok "GCS 업로드 완료"
    else
        warn "GCS 업로드 실패 (gsutil 미설치 또는 권한 없음)"
    fi
}

# ── 오래된 로컬 백업 정리 ────────────────────────────────────────────────────
cleanup_old_backups() {
    log "오래된 백업 정리 (${RETENTION_DAYS}일 초과)..."
    local removed=0
    while IFS= read -r -d '' dir; do
        rm -rf "$dir"
        removed=$((removed + 1))
    done < <(find "$BACKUP_BASE" -maxdepth 1 -mindepth 1 -type d \
             -mtime "+${RETENTION_DAYS}" -print0 2>/dev/null)
    ok "정리 완료 (${removed}개 디렉토리 삭제)"
}

# ── 백업 요약 저장 ────────────────────────────────────────────────────────────
save_summary() {
    local summary_file="$BACKUP_DIR/backup_summary.json"
    local total_size
    total_size=$(du -sh "$BACKUP_DIR" 2>/dev/null | cut -f1 || echo "unknown")
    python3 -c "
import json, datetime
summary = {
    'timestamp': '$TIMESTAMP',
    'backup_dir': '$BACKUP_DIR',
    'total_size': '$total_size',
    'gcs_bucket': '${GCS_BUCKET:-none}',
    'retention_days': $RETENTION_DAYS,
    'completed_at': datetime.datetime.now().isoformat(),
}
print(json.dumps(summary, ensure_ascii=False, indent=2))
" > "$summary_file"
    log "백업 요약: $summary_file"
}

# ── 복구 가이드 ───────────────────────────────────────────────────────────────
show_restore_guide() {
    cat <<'GUIDE'

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📦 복구 절차
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

■ Qdrant 복구
  1. 대상 백업 스냅샷 파일 확인:
     ls data/backups/*/qdrant/*.snapshot

  2. 컬렉션 복구 (스냅샷 업로드 후 복원):
     SNAP=data/backups/20260831_030000/qdrant/strategic_pages_20260831_030000.snapshot
     COLLECTION=strategic_pages

     # 스냅샷 업로드
     curl -X POST "http://localhost:6333/collections/$COLLECTION/snapshots/upload" \
       -H "Content-Type: multipart/form-data" \
       -F "snapshot=@$SNAP"

     # 스냅샷 이름으로 복원 (위 응답의 name 값 사용)
     curl -X PUT "http://localhost:6333/collections/$COLLECTION/snapshots/recover" \
       -H "Content-Type: application/json" \
       -d '{"location": "file:///qdrant/snapshots/SNAP_NAME"}'

■ FalkorDB 복구
  1. 서비스 중지:
     docker-compose stop falkordb

  2. dump.rdb 교체:
     BACKUP=data/backups/20260831_030000/falkordb/dump_20260831_030000.rdb
     docker cp $BACKUP falkordb:/data/dump.rdb

  3. 서비스 재시작:
     docker-compose start falkordb

■ PostgreSQL 복구
  BACKUP=data/backups/20260831_030000/postgres/ops_log_20260831_030000.sql.gz
  gunzip -c $BACKUP | psql $POSTGRES_URL

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GUIDE
}

# ── 메인 ──────────────────────────────────────────────────────────────────────
DO_QDRANT=true
DO_FALKORDB=true
DO_POSTGRES=true

for arg in "$@"; do
    case "$arg" in
        --qdrant-only)   DO_FALKORDB=false; DO_POSTGRES=false ;;
        --falkordb-only) DO_QDRANT=false;   DO_POSTGRES=false ;;
        --postgres-only) DO_QDRANT=false;   DO_FALKORDB=false ;;
        --restore)       show_restore_guide; exit 0 ;;
    esac
done

log "━━━━ Semantica 백업 시작 [$TIMESTAMP] ━━━━"
log "백업 위치: $BACKUP_DIR"

$DO_QDRANT   && backup_qdrant   || true
$DO_FALKORDB && backup_falkordb || true
$DO_POSTGRES && backup_postgres || true

upload_gcs
cleanup_old_backups
save_summary

log "━━━━ 백업 완료 ━━━━"
