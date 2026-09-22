#!/usr/bin/env bash
# 콘솔 앞단에 nginx + TLS 를 붙인다 (BACKLOG B-35). 여러 번 실행해도 안전하다.
#
#   sudo /opt/tybot/deploy/setup-nginx-tls.sh
#   sudo /opt/tybot/deploy/setup-nginx-tls.sh --dry-run    # 무엇을 할지만 본다
#
# ## 왜 스크립트인가
# 단계가 여섯인데 그중 둘은 **빠뜨려도 조용히 실패한다.**
#
# - SELinux `httpd_can_network_connect` 를 안 켜면 nginx 가 루프백으로 프록시하지
#   못한다. 화면에는 502 만 뜨고, nginx 설정에는 아무 문제가 없다. Rocky 의 기본값이
#   막는 쪽이라 「설정이 맞는데 왜 안 되지」 로 한참 헤맨다
# - 인증서 경로가 없으면 nginx 가 아예 안 뜬다. 그런데 그때 콘솔은 이미 루프백으로
#   바뀐 뒤일 수 있다 — 앞문도 뒷문도 닫힌다
#
# 사람이 여섯 단계를 옮겨 적는 자리는 한 번은 빠진다. 2026-09-22 에 실제로 그랬다.
#
# ## 하지 않는 것
# - **방화벽을 건드리지 않는다.** 지금 8787 출발지를 좁혀 둔 상태일 수 있고, 그건
#   사람이 정한 범위다. 필요한 명령은 마지막에 **출력만** 한다
# - **`CONSOLE_COOKIE_SECURE` 를 켜지 않는다.** HTTPS 로 실제 로그인이 되는 것을
#   본 뒤에 켜야 한다. 먼저 켜면 쿠키가 전송되지 않아 로그인이 안 되고, 화면에는
#   「비밀번호가 틀렸다」 처럼 보인다
# - **콘솔 바인딩을 직접 바꾸지 않는다.** 그건 `install.sh` 의 안전장치가 한다 —
#   nginx 가 실제로 받고 있는지 확인한 뒤에만 루프백으로 넘어간다
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(dirname "$HERE")"
CONF_SRC="$HERE/nginx/tybot-console.conf"
CONF_DST=/etc/nginx/conf.d/tybot-console.conf
CERT_DIR=/etc/pki/tybot
CERT="$CERT_DIR/console.crt"
KEY="$CERT_DIR/console.key"
DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

log() { echo "[$(date '+%F %T')] $*"; }
run() {
  if ((DRY_RUN)); then echo "    (dry-run) $*"; else "$@"; fi
}

[[ $EUID -eq 0 ]] || { echo "root 로 실행하세요 (sudo)"; exit 1; }
[[ -f "$CONF_SRC" ]] || { echo "설정 원본이 없습니다: $CONF_SRC — 코드를 먼저 배포하세요"; exit 2; }

# ---------------------------------------------------------------------------
# 1. nginx
# ---------------------------------------------------------------------------
if command -v nginx >/dev/null 2>&1; then
  log "nginx 가 이미 설치돼 있습니다 ($(nginx -v 2>&1))"
else
  log "nginx 를 설치합니다"
  run dnf install -y nginx
fi

# ---------------------------------------------------------------------------
# 2. 인증서
# ---------------------------------------------------------------------------
#
# 사내 CA 발급이 1순위다. 없으면 자체 서명으로 시작해도 **평문보다는 낫다** —
# 자체 서명은 「이 서버가 맞나」 를 증명하지 못할 뿐, 도청은 막는다. 지금 막으려는
# 것은 같은 사내망에서 비밀번호와 세션 쿠키를 주워 담는 일이다.
#
# 이미 있는 인증서는 **덮어쓰지 않는다.** 사내 CA 발급본을 배치해 둔 경우 그것이
# 사라지면 브라우저 경고가 다시 뜨고, 사람은 이유를 모른다.
if [[ -f "$CERT" && -f "$KEY" ]]; then
  log "인증서가 이미 있습니다: $CERT (만료 $(openssl x509 -enddate -noout -in "$CERT" 2>/dev/null | cut -d= -f2 || echo '확인 실패'))"
else
  HOSTNAME_FQDN="$(hostname -f 2>/dev/null || hostname)"
  log "자체 서명 인증서를 만듭니다 (CN=$HOSTNAME_FQDN)"
  log "  사내 CA 발급본이 준비되면 같은 경로에 덮어쓰고 'systemctl reload nginx' 하세요"
  run install -d -m 0755 "$CERT_DIR"
  run openssl req -x509 -newkey rsa:2048 -nodes -days 397 \
    -keyout "$KEY" -out "$CERT" \
    -subj "/CN=$HOSTNAME_FQDN" \
    -addext "subjectAltName=DNS:$HOSTNAME_FQDN,DNS:localhost,IP:127.0.0.1"
  run chmod 600 "$KEY"
  run chmod 644 "$CERT"
fi

# ---------------------------------------------------------------------------
# 3. SELinux — 이걸 빠뜨리면 502 만 뜬다
# ---------------------------------------------------------------------------
#
# Rocky 의 기본값은 httpd 계열이 네트워크로 나가는 것을 막는다. nginx 가
# `127.0.0.1:8787` 로 프록시하는 것도 거기 걸린다. 설정에는 아무 문제가 없는데
# 502 만 나오므로, 원인을 nginx 설정에서 찾다가 시간을 다 쓴다.
if command -v getsebool >/dev/null 2>&1 && getenforce 2>/dev/null | grep -qv Disabled; then
  if getsebool httpd_can_network_connect 2>/dev/null | grep -q ' on$'; then
    log "SELinux httpd_can_network_connect 가 이미 켜져 있습니다"
  else
    log "SELinux httpd_can_network_connect 를 켭니다 (없으면 nginx 프록시가 502 를 냅니다)"
    run setsebool -P httpd_can_network_connect 1
  fi
else
  log "SELinux 가 꺼져 있거나 도구가 없습니다 — 건너뜁니다"
fi

# ---------------------------------------------------------------------------
# 4. 설정 배치
# ---------------------------------------------------------------------------
if [[ -f "$CONF_DST" ]] && cmp -s "$CONF_SRC" "$CONF_DST"; then
  log "설정이 최신입니다: $CONF_DST"
else
  if [[ -f "$CONF_DST" ]]; then
    log "기존 설정을 $CONF_DST.bak 으로 보관하고 갱신합니다"
    run cp -a "$CONF_DST" "$CONF_DST.bak"
  fi
  log "설정을 배치합니다: $CONF_DST"
  run install -m 0644 "$CONF_SRC" "$CONF_DST"
fi

# ---------------------------------------------------------------------------
# 5. 문법 검사 후 기동
# ---------------------------------------------------------------------------
#
# **문법 검사를 먼저 한다.** 틀린 설정으로 reload 하면 nginx 는 옛 설정으로 계속
# 돌지만, 처음 start 라면 아예 안 뜬다. 그 상태에서 콘솔 바인딩이 루프백으로
# 넘어가면 앞문도 뒷문도 닫힌다.
if ((DRY_RUN)); then
  echo "    (dry-run) nginx -t"
else
  if ! nginx -t; then
    log "설정 문법 검사 실패 — nginx 를 기동하지 않습니다. 위 오류를 먼저 고치세요."
    exit 3
  fi
fi

if systemctl is-active --quiet nginx 2>/dev/null; then
  log "nginx 를 reload 합니다"
  run systemctl reload nginx
else
  log "nginx 를 기동합니다"
  run systemctl enable --now nginx
fi

# ---------------------------------------------------------------------------
# 6. 콘솔 바인딩 전환은 install.sh 가 한다
# ---------------------------------------------------------------------------
#
# 여기서 직접 바꾸지 않는다. `install.sh` 의 `console_bind_guard` 가 nginx 가
# **실제로 받고 있는지** 확인한 뒤에만 루프백으로 넘긴다. 확인하는 쪽이 하나여야
# 두 곳의 판단이 어긋나지 않는다.
log "콘솔 바인딩을 전환합니다 (install.sh 안전장치)"
run env WITH_CONSOLE=1 bash "$APP_DIR/deploy/install.sh"

# ---------------------------------------------------------------------------
# 남은 일 — 사람이 판단해야 하는 것만
# ---------------------------------------------------------------------------
cat <<NOTE

────────────────────────────────────────────────────────────────────────
여기까지 자동입니다. 남은 둘은 **사람이 확인한 뒤에** 하세요.

1) 방화벽 — 지금 열어 둔 범위를 이 스크립트가 건드리지 않았습니다.
   443·80 을 열고 8787·8788 을 닫습니다. 출발지 제한은 지금 쓰시는 범위를
   그대로 유지하세요.

     sudo firewall-cmd --permanent --add-service=https --add-service=http
     sudo firewall-cmd --permanent --remove-port=8787/tcp
     sudo firewall-cmd --reload
     sudo firewall-cmd --list-all

2) Secure 쿠키 — **HTTPS 로 실제 로그인이 되는 것을 본 뒤에** 켭니다.
   먼저 켜면 브라우저가 쿠키를 보내지 않아 로그인이 안 되고, 화면에는
   「비밀번호가 틀렸다」 처럼 보입니다.

     https://$(hostname -f 2>/dev/null || hostname)/   로 로그인 확인
     그다음:
       /etc/tybot/tybot.env      에 CONSOLE_COOKIE_SECURE=1
       /etc/tybot-pf/console.env 에 PF_CONSOLE_COOKIE_SECURE=1
       sudo systemctl restart tybot-console

확인:
     curl -skI https://127.0.0.1/            | head -1     # 200 또는 3xx
     curl -sI  http://127.0.0.1:8787/        | head -1     # 연결 거부여야 정상
     sudo systemctl status nginx --no-pager  | head -5
────────────────────────────────────────────────────────────────────────
NOTE
