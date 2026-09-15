#!/usr/bin/env bash
#
# systemd 유닛 설치 — 계정·경로를 설치 시점에 채웁니다
# ─────────────────────────────────────────────────────────────────────────────
# 레포의 유닛 파일은 `User=seongin` / `/home/seongin/sementica` 를 박아두고
# 있었습니다. 2026-09-15 라이브 서버(`devadmin`)로 옮기면서 그대로 복사하면
# 기동에 실패한다는 것이 드러났고, 손으로 고치면 다음 서버에서 또 같은 일이
# 반복됩니다. 이 스크립트가 레포 위치와 소유 계정을 읽어 채워 넣습니다.
#
# 사용법:
#     sudo bash deploy/install.sh ops
#     sudo bash deploy/install.sh ops rest
#     sudo bash deploy/install.sh --dry-run ops     # 설치될 내용만 출력
#
# 인자를 주지 않으면 현재 상태만 보여주고 아무것도 설치하지 않습니다.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SYSTEMD_DIR=/etc/systemd/system
DRY_RUN=0

# 소유 계정은 `whoami` 로 알 수 없습니다 — 이 스크립트는 sudo 로 도니까요.
# 레포 디렉터리의 소유자가 서비스를 돌려야 할 계정입니다.
OWNER="$(stat -c %U "$ROOT")"

declare -A UNITS=(
    [ops]=sementica-ops.service
    [rest]=sementica-rest.service
    [mcp]=sementica-mcp@.service
)

# logrotate 는 systemd 유닛이 아니라 설치 위치도 치환 대상도 다릅니다.
LOGROTATE_SRC=logrotate-sementica
LOGROTATE_DST=/etc/logrotate.d/sementica

info()  { printf '  %s\n' "$*"; }
warn()  { printf '  ⚠️  %s\n' "$*" >&2; }
die()   { printf '  ❌ %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'

  사용법: sudo bash deploy/install.sh [--dry-run] <유닛...>

    ops        Ops 대시보드 (8080, 127.0.0.1 바인딩)
    rest       REST API     (8766, Snowflake UDF 가 호출)
    mcp        MCP 템플릿   (sementica-mcp@<부서>)
    logrotate  cron 로그 회전 (/etc/logrotate.d/sementica)

  예:
    sudo bash deploy/install.sh ops
    sudo bash deploy/install.sh --dry-run ops rest logrotate

EOF
}

# ── 현재 상태 ────────────────────────────────────────────────────────────────
show_state() {
    printf '\n  레포   %s\n' "$ROOT"
    printf '  계정   %s\n\n' "$OWNER"
    printf '  %-28s %-12s %s\n' "유닛" "설치" "상태"
    printf '  %s\n' "$(printf '─%.0s' {1..58})"
    for key in ops rest mcp; do
        local unit="${UNITS[$key]}"
        local installed="—" state="—"
        if [[ -f "$SYSTEMD_DIR/$unit" ]]; then
            installed="설치됨"
            # 템플릿(@)은 인스턴스 없이는 상태를 물을 수 없습니다.
            if [[ "$unit" != *@.service ]]; then
                state="$(systemctl is-active "$unit" 2>/dev/null || true)"
            else
                state="템플릿"
            fi
        fi
        printf '  %-28s %-12s %s\n' "$unit" "$installed" "$state"
    done
    local lr="—"
    [[ -f "$LOGROTATE_DST" ]] && lr="설치됨"
    printf '  %-28s %-12s %s\n' "logrotate.d/sementica" "$lr" "—"
    echo
}

# ── 사전 점검 ────────────────────────────────────────────────────────────────
# 유닛이 참조하는 것이 없으면 systemd 는 기동에 실패하면서도 원인을 journal
# 깊숙이 남깁니다. 설치 전에 확인하는 편이 훨씬 빠릅니다.
preflight() {
    [[ -x "$ROOT/.venv/bin/python" ]] \
        || die ".venv/bin/python 이 없습니다 — python3 -m venv .venv 부터 하세요"
    [[ -f "$ROOT/.env" ]] \
        || die ".env 가 없습니다 (EnvironmentFile 로 참조하므로 없으면 기동 실패)"
    id "$OWNER" >/dev/null 2>&1 \
        || die "계정 '$OWNER' 가 없습니다 (레포 소유자를 읽었습니다)"
}

# ── 치환 ─────────────────────────────────────────────────────────────────────
render() {
    local src="$1"
    sed -e "s|/home/seongin/sementica|$ROOT|g" \
        -e "s|^User=seongin$|User=$OWNER|" \
        -e "s|^\(\s*create 0640 \)seongin seongin$|\1$OWNER $OWNER|" \
        "$src"
}

install_file() {
    local name="$1" src="$2" dst="$3"
    [[ -f "$src" ]] || die "레포에 $name 이 없습니다"

    local tmp
    tmp="$(mktemp)"
    render "$src" > "$tmp"

    if [[ -f "$dst" ]] && diff -q "$tmp" "$dst" >/dev/null 2>&1; then
        info "$name — 이미 같은 내용입니다"
        rm -f "$tmp"
        return
    fi

    if [[ -f "$dst" ]]; then
        warn "$name 이 이미 있습니다. 바뀌는 부분:"
        diff -u "$dst" "$tmp" | sed 's/^/      /' || true
    fi

    if (( DRY_RUN )); then
        info "[dry-run] $dst 에 쓰지 않았습니다"
        rm -f "$tmp"
        return
    fi

    install -m 0644 "$tmp" "$dst"
    rm -f "$tmp"
    info "$name → $dst"
}

install_target() {
    local key="$1"
    if [[ "$key" == logrotate ]]; then
        install_file "$LOGROTATE_SRC" "$ROOT/deploy/$LOGROTATE_SRC" "$LOGROTATE_DST"
        return
    fi
    local unit="${UNITS[$key]:-}"
    [[ -n "$unit" ]] || die "알 수 없는 유닛: $key"
    install_file "$unit" "$ROOT/deploy/$unit" "$SYSTEMD_DIR/$unit"
}

# ── 인자 ─────────────────────────────────────────────────────────────────────
targets=()
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        -h|--help) usage; exit 0 ;;
        ops|rest|mcp|logrotate) targets+=("$arg") ;;
        *) usage; die "알 수 없는 인자: $arg" ;;
    esac
done

show_state

if (( ${#targets[@]} == 0 )); then
    usage
    info "설치할 유닛을 지정하세요. 위 표는 현재 상태입니다."
    exit 0
fi

# dry-run 은 아무것도 쓰지 않으므로 root 없이도 돌려볼 수 있어야 합니다.
if (( ! DRY_RUN )); then
    [[ $EUID -eq 0 ]] || die "root 권한이 필요합니다 (sudo bash deploy/install.sh ...)"
fi

preflight

for t in "${targets[@]}"; do
    install_target "$t"
done

if (( DRY_RUN )); then
    echo
    info "dry-run 이므로 daemon-reload 도 하지 않았습니다."
    exit 0
fi

systemctl daemon-reload
info "daemon-reload 완료"

echo
info "다음 단계:"
for t in "${targets[@]}"; do
    case "$t" in
        mcp)       printf '    sudo systemctl enable --now sementica-mcp@strategic\n' ;;
        logrotate) printf '    sudo logrotate -d %s   # 점검 (회전 없음)\n' "$LOGROTATE_DST" ;;
        *)         printf '    sudo systemctl enable --now %s\n' "${UNITS[$t]%.service}" ;;
    esac
done
echo

# Ops 대시보드는 인증이 없습니다. 유닛이 127.0.0.1 에만 바인딩하는 이유가
# 그것이고, 방화벽으로 열면 그 대역 누구나 /api/batch/run 을 칠 수 있습니다.
for t in "${targets[@]}"; do
    if [[ "$t" == ops ]]; then
        cat <<EOF
  ℹ️  Ops 대시보드는 127.0.0.1 에만 바인딩합니다 — 인증이 없기 때문입니다.
      /api/batch/run 의 confirm 필드는 오조작 방지지 인증이 아닙니다.
      접속은 SSH 터널로 하세요:

          ssh -N -L 8080:127.0.0.1:8080 -p 50022 $OWNER@<서버IP>

EOF
    fi
done
