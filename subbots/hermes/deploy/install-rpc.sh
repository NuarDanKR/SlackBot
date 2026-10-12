#!/usr/bin/env bash
# Hermes 전문봇 RPC worker 설치 helper.
#
#   sudo subbots/hermes/deploy/install-rpc.sh [<workspace> ...]
#
# 하는 일은 **하나뿐**이다 — unit 템플릿을 /etc/systemd/system 에 놓고 daemon-reload.
# 워크스페이스를 인자로 주면 그 인스턴스에 **무엇이 모자란지 점검해 알려 준다.**
# 고쳐 주지는 않는다.
#
# 중앙 `deploy/install.sh` 가 이 파일을 부른다(연결은 Codex 몫).
#
# ── 안 하는 것, 그리고 그 이유 ────────────────────────────────────────────
#
#   enable·start      안 한다. 계정·비밀·소켓 그룹을 사람이 확인한 뒤 켠다
#   계정 생성         안 한다. 설치 스크립트가 계정을 만들면 그 계정이 어떤 그룹을
#                     갖는지 아무도 안 보게 되고, 이 worker 의 격리는 전부 그룹이다
#   HMAC 비밀 생성    **절대 안 한다.** 양쪽이 같은 값을 가져야 하는데 한쪽만 새로
#                     만들면 전부 401 이 되고, 증상은 「전문가가 조용히 답을 안 함」 이다
#   env 파일 생성     안 한다. 빈 파일을 놓으면 `ConditionPathExists` 류 점검을
#                     통과해 버리고, 모자란 설정은 기동 때야 드러난다
#
# ── 멱등 ──────────────────────────────────────────────────────────────────
#
# 몇 번을 돌려도 같다. unit 내용이 같으면 복사하지 않고 daemon-reload 도 건너뛴다 —
# 쓸데없는 reload 는 돌고 있는 인스턴스에 영향은 없지만, 「바뀐 게 없는데 왜 reload 가
# 찍혔나」 를 나중에 되짚게 만든다.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_SRC="$HERE/hermes-rpc@.service"
UNIT_DST="/etc/systemd/system/hermes-rpc@.service"
SHARED_GROUP="tybot-specialist-rpc"
TOOL_SOCKET="/run/tybot/specialist-tools.sock"
HERMES_ROOT="$(cd "$HERE/.." && pwd)"

say()  { printf '%s\n' "$*"; }
warn() { printf '  ! %s\n' "$*" >&2; }
ok()   { printf '  . %s\n' "$*"; }

[[ -f "$UNIT_SRC" ]] || { say "unit 템플릿이 없습니다: $UNIT_SRC"; exit 1; }

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  say "root 로 실행하세요: sudo $0 $*"
  exit 1
fi

# ── 1. unit 설치 ──────────────────────────────────────────────────────────
if [[ -f "$UNIT_DST" ]] && cmp -s "$UNIT_SRC" "$UNIT_DST"; then
  ok "unit 이 이미 최신입니다: $UNIT_DST"
else
  install -o root -g root -m 0644 "$UNIT_SRC" "$UNIT_DST"
  say "unit 설치: $UNIT_DST"
  systemctl daemon-reload
  say "daemon-reload 완료"
fi

# ── 2. 공통 전제 점검 ─────────────────────────────────────────────────────
say ""
say "[공통]"
if getent group "$SHARED_GROUP" >/dev/null; then
  ok "그룹 $SHARED_GROUP 있음"
else
  warn "그룹 $SHARED_GROUP 이 없습니다 — TYBot 과 worker 가 만나는 자리입니다."
  warn "  groupadd --system $SHARED_GROUP"
fi
if [[ -S "$TOOL_SOCKET" ]]; then
  ok "TYBot 도구 소켓 있음: $TOOL_SOCKET"
else
  warn "TYBot 도구 소켓이 없습니다: $TOOL_SOCKET (TYBot 이 떠 있어야 생깁니다)"
fi
command -v node >/dev/null && ok "node $(node --version)" || warn "node 가 PATH 에 없습니다"

# ── 3. 워크스페이스별 전제 점검 ───────────────────────────────────────────
#
# **모자란 것을 고치지 않고 그대로 적는다.** 설치가 조용히 메워 주면, 그 자리에
# 무엇이 필요한지 아무도 모르는 채로 운영이 시작된다.
missing=0
for ws in "$@"; do
  user="hermes-rpc-$ws"
  envfile="/etc/hermes-rpc-$ws/rpc.env"
  secret="/etc/hermes-rpc-$ws/hmac"

  say ""
  say "[$ws]"

  if id -u "$user" >/dev/null 2>&1; then
    ok "계정 $user 있음"
    if id -nG "$user" | tr ' ' '\n' | grep -qx "$SHARED_GROUP"; then
      ok "$user 가 $SHARED_GROUP 에 있음"
    else
      warn "$user 가 $SHARED_GROUP 에 없습니다 — 도구 소켓에 못 붙습니다."
      warn "  usermod -aG $SHARED_GROUP $user"
      missing=1
    fi
    if sudo -u "$user" test -r "$HERMES_ROOT/scripts/run-rpc-worker.js"; then
      ok "$user 가 RPC worker 코드를 읽을 수 있음"
    else
      warn "$user 가 RPC worker 코드를 읽지 못합니다: $HERMES_ROOT/scripts/run-rpc-worker.js"
      warn "  중앙 deploy/install.sh 를 다시 실행해 $SHARED_GROUP 코드 권한을 적용하세요."
      missing=1
    fi
  else
    warn "계정 $user 가 없습니다. systemd 는 계정을 만들지 않습니다(217/USER)."
    warn "  useradd --system --home-dir /nonexistent --shell /sbin/nologin \\"
    warn "          --groups $SHARED_GROUP $user"
    missing=1
  fi

  if [[ -f "$envfile" ]]; then
    ok "env $envfile 있음"
    for key in HERMES_DOMAIN ANTHROPIC_API_KEY; do
      grep -q "^${key}=" "$envfile" || { warn "$envfile 에 $key 가 없습니다"; missing=1; }
    done
  else
    warn "env 가 없습니다: $envfile"
    warn "  install -d -o root -g $user -m 0750 /etc/hermes-rpc-$ws"
    warn "  install -o root -g $user -m 0640 /dev/null $envfile   # HERMES_DOMAIN, ANTHROPIC_API_KEY"
    missing=1
  fi

  if [[ -f "$secret" ]]; then
    # 길이만 본다. **값은 찍지 않는다.**
    bytes=$(wc -c <"$secret")
    if (( bytes >= 32 )); then
      ok "HMAC 비밀 있음 (${bytes}바이트)"
    else
      warn "HMAC 비밀이 너무 짧습니다 (${bytes}바이트, 32 이상 필요)"
      missing=1
    fi
  else
    warn "HMAC 비밀이 없습니다: $secret"
    warn "  **TYBot 쪽과 같은 값**이어야 합니다. 한쪽만 새로 만들면 전부 401 이고,"
    warn "  증상은 「전문가가 조용히 답을 안 함」 입니다. 여기서 만들지 않습니다."
    missing=1
  fi
done

# ── 4. 마무리 ─────────────────────────────────────────────────────────────
say ""
if (( missing )); then
  say "모자란 것이 있습니다. 위 명령을 먼저 끝내세요."
  say "절차: subbots/hermes/deploy/README.md 의 「RPC worker」"
fi
say "켜는 것은 사람이 합니다(이 스크립트는 enable 도 start 도 하지 않습니다):"
for ws in "${@:-<workspace>}"; do
  say "  systemctl enable --now hermes-rpc@$ws"
done
exit 0
