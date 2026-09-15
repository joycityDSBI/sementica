#!/usr/bin/env bash
# =============================================================================
# Semantica 작업 실행기 — 허용된 작업만 이름으로 실행합니다
#
# 두 가지 역할을 합니다:
#
#  ① **작업 정의를 한곳에** — Airflow DAG, cron, 손으로 돌리는 것이 모두 같은
#     명령을 쓰게 합니다. DAG 에 명령 문자열이 흩어져 있으면 여기만 고쳐도
#     DAG 이 옛 명령을 계속 쓰게 됩니다.
#
#  ② **SSH 강제 명령(forced command)** — Airflow 서버에서 이 VM 으로 SSH 를
#     열 때, 그 키로는 **아래 목록의 작업만** 실행할 수 있게 합니다.
#     방화벽으로 접속 주소를 좁히는 것과 별개로, 접속한 뒤에 무엇을 할 수
#     있는지도 좁혀야 합니다. Airflow 서버가 털리면 그 키로 VM 에서 임의
#     명령을 실행할 수 있게 되는 상황을 막습니다.
#
# 사용:
#   bash scripts/run_job.sh sync
#   bash scripts/run_job.sh --list
#
# SSH 강제 명령으로 쓰려면 ~/.ssh/authorized_keys 에:
#   command="/home/seongin/sementica/scripts/run_job.sh",no-port-forwarding,\
#   no-agent-forwarding,no-X11-forwarding,no-pty ssh-ed25519 AAAA... airflow@corp
#
#   → 이 키로 접속하면 무엇을 보내든 이 스크립트가 실행되고, 원래 명령은
#     SSH_ORIGINAL_COMMAND 로 전달됩니다. 목록에 없으면 거부합니다.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_PY="$ROOT_DIR/.venv/bin/python"
DEPT="${SEMANTICA_DEPT:-strategic}"

cd "$ROOT_DIR"

# SSH 강제 명령으로 불린 경우 원래 명령이 여기 들어옵니다.
JOB="${1:-${SSH_ORIGINAL_COMMAND:-}}"
JOB="$(echo "$JOB" | tr -d '\r' | xargs || true)"   # 개행·여분 공백 제거

usage() {
    cat <<'USAGE'
허용된 작업:
  sync         Notion 증분 동기화 (재시도 안전 — content_hash 비교)
  backup       Qdrant·FalkorDB·PostgreSQL·Notion 캐시·설정 파일
  glossary     용어집 스냅샷 갱신 (VM 은 API 차단 상태라 실패할 수 있음)
  eval-dev     dev 골든셋 평가

일부러 뺀 것:
  reset        ingest.py --reset 은 그래프와 벡터를 통째로 지웁니다.
               자동 실행에 올릴 종류가 아닙니다.
  eval-holdout holdout 을 정기 실행하면 그 순간 holdout 이 dev 가 되고,
               일반화를 잴 수단이 사라집니다. 사람이 직접 돌리세요.
USAGE
}

case "$JOB" in
    sync)
        exec "$VENV_PY" "$ROOT_DIR/src/pipeline/sync.py" --dept "$DEPT"
        ;;
    backup)
        exec bash "$ROOT_DIR/scripts/backup.sh"
        ;;
    glossary)
        exec "$VENV_PY" "$ROOT_DIR/tools/fetch_glossary_snapshot.py"
        ;;
    eval-dev)
        exec "$VENV_PY" "$ROOT_DIR/src/eval/evaluate.py" \
            --dept "$DEPT" --golden "$ROOT_DIR/data/eval/golden_v2_dev.json"
        ;;
    --list|list|help|--help|-h)
        usage
        ;;
    "")
        echo "❌ 작업 이름이 없습니다." >&2
        usage >&2
        exit 2
        ;;
    *)
        # 거부된 시도는 기록에 남깁니다. SSH 키가 유출되면 이 로그가 첫 단서입니다.
        echo "❌ 허용되지 않은 작업: ${JOB}" >&2
        logger -t semantica-run-job "거부: ${JOB} (from ${SSH_CONNECTION:-local})" 2>/dev/null || true
        usage >&2
        exit 2
        ;;
esac
