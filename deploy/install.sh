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

# nginx 는 도메인까지 필요하므로 --domain 을 함께 받습니다.
NGINX_SRC=nginx-sementica.conf
NGINX_DST=/etc/nginx/conf.d/sementica.conf
DOMAIN=""

# 인증서 경로. 와일드카드(*.joycityplay.com)를 쓰므로 certbot 경로가 아닙니다.
# --with-tls 를 줄 때만 443 블록이 열립니다.
TLS_ENABLE=0
TLS_CERT=/etc/ssl/sementica/fullchain.crt
TLS_KEY=/etc/ssl/sementica/privkey.key

info()  { printf '  %s\n' "$*"; }
warn()  { printf '  ⚠️  %s\n' "$*" >&2; }
die()   { printf '  ❌ %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'

  사용법: sudo bash deploy/install.sh [옵션] <유닛...>

    ops        Ops 대시보드 (8080, 바인딩은 .env 의 OPS_HOST)
    rest       REST API     (8766, 127.0.0.1 바인딩 — nginx 경유)
    mcp        MCP 템플릿   (sementica-mcp@<부서>)
    logrotate  cron 로그 회전 (/etc/logrotate.d/sementica)
    nginx      HTTPS 종료   (/etc/nginx/conf.d/sementica.conf, --domain 필요)

  옵션:
    --dry-run              쓰지 않고 내용만 출력
    --domain <fqdn>        nginx 의 server_name
    --with-tls             443 블록을 켭니다 (인증서를 먼저 확인합니다)
    --tls-cert <path>      기본 /etc/ssl/sementica/fullchain.crt
    --tls-key  <path>      기본 /etc/ssl/sementica/privkey.key

  예:
    sudo bash deploy/install.sh ops
    sudo bash deploy/install.sh --dry-run ops rest logrotate
    sudo bash deploy/install.sh --domain ontology.joycityplay.com nginx
    sudo bash deploy/install.sh --domain ontology.joycityplay.com --with-tls nginx

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
    local ng="—" ngstate="—"
    if [[ -f "$NGINX_DST" ]]; then
        ng="설치됨"
        ngstate="$(systemctl is-active nginx 2>/dev/null || true)"
    fi
    printf '  %-28s %-12s %s\n' "nginx.conf.d/sementica" "$ng" "$ngstate"
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
    # 레포를 root 로 만졌으면(git pull 을 sudo 로 돌리는 등) 일부 파일이 root
    # 소유로 남습니다. 그 상태에서 OWNER 를 읽으면 `User=root` 짜리 유닛이
    # 조용히 써지고, 서비스가 root 로 돌게 됩니다. 멈추는 편이 낫습니다.
    #
    # .git/objects 가 특히 잘 오염됩니다 — 그러면 devadmin 의 git pull 이
    # "insufficient permission for adding an object" 로 실패합니다.
    # 이름이 아니라 UID 로 비교합니다. `find -user <이름>` 은 이름에 특수문자가
    # 있으면 "invalid user name" 으로 죽는데, 그때 2>/dev/null 과 맞물리면
    # **검사가 통과한 것처럼 보입니다** — 검출기가 조용히 꺼지는 최악의 형태입니다.
    local owner_uid strays
    owner_uid="$(id -u "$OWNER" 2>/dev/null || true)"
    [[ -n "$owner_uid" ]] || die "계정 '$OWNER' 의 UID 를 읽지 못했습니다"
    strays="$(find "$ROOT" -not -uid "$owner_uid" -printf '%u %p\n' 2>/dev/null | head -5)"
    if [[ -n "$strays" ]]; then
        warn "레포에 '$OWNER' 소유가 아닌 파일이 있습니다 (앞 5개):"
        printf '%s\n' "$strays" | sed 's/^/      /'
        die "chown -R $OWNER:$OWNER $ROOT 로 정리한 뒤 다시 실행하세요"
    fi

    id "$OWNER" >/dev/null 2>&1 \
        || die "계정 '$OWNER' 가 없습니다 (레포 소유자를 읽었습니다)"
}

# ── 치환 ─────────────────────────────────────────────────────────────────────
render() {
    local src="$1"
    # --with-tls 일 때만 TLS-BLOCK 안쪽의 주석을 한 겹 벗깁니다.
    # `//!` 로 범위 경계선(>>> / <<<)은 건드리지 않습니다.
    local tls_sed='b'
    (( TLS_ENABLE )) && tls_sed='/^# >>> TLS-BLOCK/,/^# <<< TLS-BLOCK/{//!s/^#//}'
    sed -e "s|/home/seongin/sementica|$ROOT|g" \
        -e "s|^User=seongin$|User=$OWNER|" \
        -e "s|^\(\s*create 0640 \)seongin seongin$|\1$OWNER $OWNER|" \
        -e "s|<SEMANTICA_HOST>|${DOMAIN}|g" \
        -e "s|<TLS_CERT>|${TLS_CERT}|g" \
        -e "s|<TLS_KEY>|${TLS_KEY}|g" \
        -e "$tls_sed" \
        "$src"
}

# ── 인증서 점검 ──────────────────────────────────────────────────────────────
# 443 을 켜기 전에 확인합니다. 여기서 안 잡으면 nginx 가 기동에 실패하거나,
# 더 나쁘게는 **뜨긴 하는데 Snowflake 만 붙지 못하는** 상태가 됩니다.
check_tls() {
    [[ -f "$TLS_CERT" ]] || die "인증서가 없습니다: $TLS_CERT"
    [[ -f "$TLS_KEY"  ]] || die "키가 없습니다: $TLS_KEY"

    # ① 키에 암호가 걸려 있으면 nginx 가 부팅 때 물어볼 수 없어 기동에 실패합니다.
    if grep -qi 'ENCRYPTED' "$TLS_KEY"; then
        die "키에 암호가 걸려 있습니다 — nginx 는 부팅 시 암호를 물을 수 없습니다.
      해제:  openssl rsa -in <원본> -out $TLS_KEY"
    fi

    # ② 인증서와 키가 짝인지. 엉뚱한 짝이면 nginx 가 뜨지 않습니다.
    local c k
    c="$(openssl x509 -noout -modulus -in "$TLS_CERT" 2>/dev/null | openssl md5)"
    k="$(openssl rsa  -noout -modulus -in "$TLS_KEY"  2>/dev/null | openssl md5)"
    [[ -n "$c" && "$c" == "$k" ]] || die "인증서와 키가 짝이 아닙니다 ($TLS_CERT / $TLS_KEY)"

    # ③ 체인이 들어 있는가. 리프만 있으면 브라우저는 통과해도 Snowflake 는
    #    체인을 못 세워 실패합니다 — "브라우저는 되는데 UDF 만 안 되는" 형태입니다.
    local n
    n="$(grep -c 'BEGIN CERTIFICATE' "$TLS_CERT" || true)"
    if (( n < 2 )); then
        warn "인증서 파일에 인증서가 $n 개뿐입니다 — 중간 CA 가 빠졌을 수 있습니다."
        warn "리프만 주면 Snowflake 쪽에서만 실패합니다. 이어붙이세요:"
        warn "    cat 리프.crt 체인.crt > $TLS_CERT"
    fi

    # ④ 도메인이 인증서에 포함되는가 (와일드카드 포함).
    local names
    names="$(openssl x509 -noout -ext subjectAltName -in "$TLS_CERT" 2>/dev/null | tr -d ' ')"
    if [[ -n "$names" && -n "$DOMAIN" ]]; then
        local wild="*.${DOMAIN#*.}"
        if [[ "$names" != *"DNS:$DOMAIN"* && "$names" != *"DNS:$wild"* ]]; then
            warn "인증서의 SAN 에 $DOMAIN 도 $wild 도 없습니다:"
            printf '      %s\n' "$names"
        fi
    fi

    # ⑤ 만료일. 자동 갱신이 없으므로 사람이 챙겨야 합니다.
    local until
    until="$(openssl x509 -noout -enddate -in "$TLS_CERT" 2>/dev/null | cut -d= -f2)"
    info "인증서 만료: ${until:-확인 실패}"
    if ! openssl x509 -checkend $((30*24*3600)) -noout -in "$TLS_CERT" >/dev/null 2>&1; then
        warn "30일 안에 만료됩니다 — 갱신 일정을 잡으세요 (자동 갱신 없음)."
    fi
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

    # 신규 설치일 때도 치환 결과를 보여줍니다. 확인하라고 해놓고 보여주지
    # 않으면 dry-run 이 "경로만 찍는" 것이 됩니다 — 정작 확인해야 하는 것은
    # User / WorkingDirectory / ExecStart 에 어떤 계정·경로가 들어갔는가입니다.
    #
    # diff 대신 렌더 결과에서 직접 뽑습니다. 줄바꿈(CRLF)이 섞인 체크아웃에서는
    # diff 가 파일 전체를 변경으로 잡아 출력이 환경마다 달라집니다.
    if (( DRY_RUN )) && [[ ! -f "$dst" ]]; then
        info "$name — 계정·경로가 들어간 줄:"
        grep -nE '^(User|WorkingDirectory|EnvironmentFile|ExecStart)=|create 0640|^/.*\*\.log' "$tmp" \
            | sed 's/^/      /' || info "      (해당 줄 없음)"
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
    if [[ "$key" == nginx ]]; then
        # 도메인이 비면 server_name 이 빈 채로 설치되어 nginx 가 모든 요청을
        # 이 블록으로 받습니다. 조용히 틀리느니 여기서 멈춥니다.
        [[ -n "$DOMAIN" ]] || die "nginx 는 --domain <도메인> 이 필요합니다"
        [[ -d /etc/nginx/conf.d ]] \
            || die "/etc/nginx/conf.d 가 없습니다 — nginx 를 먼저 설치하세요"
        (( TLS_ENABLE )) && check_tls
        install_file "$NGINX_SRC" "$ROOT/deploy/$NGINX_SRC" "$NGINX_DST"
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
        --with-tls) TLS_ENABLE=1 ;;
        --domain=*) DOMAIN="${arg#--domain=}" ;;
        --tls-cert=*) TLS_CERT="${arg#--tls-cert=}" ;;
        --tls-key=*) TLS_KEY="${arg#--tls-key=}" ;;
        --domain) pending=DOMAIN ;;
        --tls-cert) pending=TLS_CERT ;;
        --tls-key) pending=TLS_KEY ;;
        -h|--help) usage; exit 0 ;;
        ops|rest|mcp|logrotate|nginx) targets+=("$arg") ;;
        *)
            if [[ -n "${pending:-}" ]]; then
                printf -v "$pending" '%s' "$arg"; pending=""
            else
                usage; die "알 수 없는 인자: $arg"
            fi
            ;;
    esac
done
[[ -n "${pending:-}" ]] && die "--${pending,,} 뒤에 값이 없습니다"

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
        nginx)     printf '    sudo nginx -t && sudo systemctl reload nginx   # 문법 검사 후 반영\n' ;;
        *)         printf '    sudo systemctl enable --now %s\n' "${UNITS[$t]%.service}" ;;
    esac
done
echo

# REST 를 루프백으로 옮겼으면 8766 인바운드를 닫아야 합니다. 안 닫으면
# nginx 를 세워두고도 평문 HTTP 우회로가 그대로 남습니다.
for t in "${targets[@]}"; do
    if [[ "$t" == nginx ]]; then
        # 인증서가 없는 상태로 443 블록을 켜면 nginx 가 기동하지 않습니다.
        # 설정 파일은 443 을 주석 상태로 배포하므로, 어느 단계인지 알려줍니다.
        if grep -q '^#server {' "$NGINX_DST" 2>/dev/null; then
            cat <<EOF
  ℹ️  443 블록은 아직 **주석 상태**입니다 (인증서가 없으면 nginx 가 기동에
      실패하기 때문). 지금은 80 번만 뜹니다 — ACME 검증용입니다.

          sudo certbot certonly --webroot -w /var/www/html -d $DOMAIN
          sudo ls -l /etc/letsencrypt/live/$DOMAIN/fullchain.pem
          # 발급 확인 후 $NGINX_DST 의 443 블록 주석 해제
          sudo nginx -t && sudo systemctl reload nginx

EOF
        fi
        cat <<EOF
  ⚠️  443 을 연 뒤에는 8766 인바운드를 방화벽에서 **닫으세요.** nginx 를
      세워도 8766 이 외부에 열려 있으면 평문 HTTP 우회로가 남고, Bearer
      토큰이 그대로 지나갈 수 있습니다. 확인:

          ss -tlnp | grep :8766      # 127.0.0.1:8766 이어야 합니다

  ⚠️  Snowflake 는 표준 TLS 검증을 합니다. **사내 CA 인증서는 신뢰하지
      않으므로** 공인 CA 인증서여야 UDF 가 붙습니다.

EOF
    fi
done

# Ops 대시보드는 인증이 없습니다. 바인딩은 .env 의 OPS_HOST 가 정하므로
# 유닛만 보고 안내하면 틀립니다 — 실제 값을 읽어서 보여줍니다.
for t in "${targets[@]}"; do
    if [[ "$t" == ops ]]; then
        ops_host="$(sed -n 's/^OPS_HOST=//p' "$ROOT/.env" 2>/dev/null | tail -1 | tr -d ' \r')"
        ops_host="${ops_host:-127.0.0.1}"
        echo "  ℹ️  Ops 대시보드는 인증이 없습니다 — /api/batch/run 의 confirm 필드는"
        echo "      오조작 방지지 인증이 아닙니다."
        if [[ "$ops_host" == "127.0.0.1" || "$ops_host" == "localhost" ]]; then
            echo "      현재 바인딩: $ops_host (.env 의 OPS_HOST). SSH 터널로 접속하세요:"
            echo ""
            echo "          ssh -N -L 8080:127.0.0.1:8080 -p 50022 $OWNER@<서버IP>"
        else
            echo "      현재 바인딩: $ops_host (.env 의 OPS_HOST) — 외부에 열려 있습니다."
            echo "      **방화벽 접근 대상이 의도한 범위인지 확인하세요.**"
        fi
        echo ""
    fi
done
