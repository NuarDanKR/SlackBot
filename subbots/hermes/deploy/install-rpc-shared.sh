#!/usr/bin/env bash
# 공유 Hermes RPC worker 설치 helper.
#
#   sudo subbots/hermes/deploy/install-rpc-shared.sh
#
# 하는 일은 **하나뿐**이다 — unit 을 /etc/systemd/system 에 놓고 daemon-reload.
# 그다음 전제가 갖춰졌는지 **점검해 알려 준다.** 고쳐 주지는 않는다.
#
# 개별 worker helper(`install-rpc.sh`)와 **따로** 둔다. 둘은 계정도 비밀도 unit 도
# 다르고, 한 스크립트에 섞으면 「공유를 깔려다 개별 설정을 덮었다」 가 생긴다.
#
# 중앙 `deploy/install.sh` 가 이 파일을 부른다(연결은 Codex 몫).
#
# ── 안 하는 것, 그리고 그 이유 ────────────────────────────────────────────
#
#   enable·start      안 한다. 공유 worker 는 재시작 하나가 **공유 대상 전체**를
#                     끊는다. 켜는 시점은 사람이 고른다
#   계정 생성         안 한다. 이 worker 의 격리는 전부 그룹이고, 스크립트가 계정을
#                     만들면 그 그룹을 아무도 안 본다
#   HMAC·API 키 생성  **절대 안 한다.** HMAC 은 TYBot 쪽과 같은 값이어야 하는데 한쪽만
#                     새로 만들면 전부 401 이고, 증상은 「조용히 답을 안 함」 이다.
#                     API 키를 만들어 줄 방법은 애초에 없다
#   profiles.json 생성 안 한다. 빈 명부를 놓으면 모든 요청이 400 이고, 그건
#                     「설정이 없다」 가 아니라 「workspace 를 잘못 보냈다」 로 읽힌다
#   개별 worker 설정 복제  안 한다(인계 §4). 공유 비밀은 **새로** 만든다 —
#                     복제하면 공유를 안 쓰기로 한 workspace 의 격리까지 무너진다
#
# ── 멱등 ──────────────────────────────────────────────────────────────────
#
# 몇 번을 돌려도 같다. unit 내용이 같으면 복사도 daemon-reload 도 건너뛴다.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_SRC="$HERE/hermes-rpc-shared.service"
UNIT_DST="/etc/systemd/system/hermes-rpc-shared.service"
SHARED_GROUP="tybot-specialist-rpc"
TOOL_SOCKET="/run/tybot/specialist-tools.sock"
USER_NAME="hermes-rpc-shared"
ETC="/etc/hermes-rpc-shared"

say()  { printf '%s\n' "$*"; }
warn() { printf '  ! %s\n' "$*" >&2; }
ok()   { printf '  . %s\n' "$*"; }

[[ -f "$UNIT_SRC" ]] || { say "unit 이 없습니다: $UNIT_SRC"; exit 1; }

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  say "root 로 실행하세요: sudo $0"
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

missing=0

# ── 2. 공통 전제 ──────────────────────────────────────────────────────────
say ""
say "[공통]"
if getent group "$SHARED_GROUP" >/dev/null; then
  ok "그룹 $SHARED_GROUP 있음"
else
  warn "그룹 $SHARED_GROUP 이 없습니다 — TYBot 과 worker 가 만나는 자리입니다."
  warn "  groupadd --system $SHARED_GROUP"
  missing=1
fi
if [[ -S "$TOOL_SOCKET" ]]; then
  ok "TYBot 도구 소켓 있음: $TOOL_SOCKET"
else
  warn "TYBot 도구 소켓이 없습니다: $TOOL_SOCKET (TYBot 이 떠 있어야 생깁니다)"
  missing=1
fi
command -v node >/dev/null && ok "node $(node --version)" || { warn "node 가 PATH 에 없습니다"; missing=1; }

# ── 3. 계정 ───────────────────────────────────────────────────────────────
say ""
say "[계정]"
if id -u "$USER_NAME" >/dev/null 2>&1; then
  ok "계정 $USER_NAME 있음"
  if id -nG "$USER_NAME" | tr ' ' '\n' | grep -qx "$SHARED_GROUP"; then
    ok "$USER_NAME 가 $SHARED_GROUP 에 있음"
  else
    warn "$USER_NAME 가 $SHARED_GROUP 에 없습니다 — 도구 소켓에 못 붙습니다."
    warn "  usermod -aG $SHARED_GROUP $USER_NAME"
    missing=1
  fi
else
  warn "계정 $USER_NAME 가 없습니다. systemd 는 계정을 만들지 않습니다(217/USER)."
  warn "  useradd --system --home-dir /nonexistent --shell /sbin/nologin \\"
  warn "          --groups $SHARED_GROUP $USER_NAME"
  missing=1
fi

# ── 4. 설정과 비밀 ────────────────────────────────────────────────────────
say ""
say "[설정]"
if [[ -d "$ETC" ]]; then
  ok "$ETC 있음"
else
  warn "$ETC 가 없습니다."
  warn "  install -d -o root -g $USER_NAME -m 0750 $ETC"
  warn "  install -d -o root -g $USER_NAME -m 0750 $ETC/keys"
  missing=1
fi

if [[ -f "$ETC/rpc.env" ]]; then
  ok "rpc.env 있음"
else
  warn "env 가 없습니다: $ETC/rpc.env"
  warn "  install -o root -g $USER_NAME -m 0640 /dev/null $ETC/rpc.env"
  warn "  # HERMES_RPC_MAX_CONCURRENT=4"
  warn "  # HERMES_RPC_MAX_PER_WORKSPACE=2"
  warn "  # HERMES_RPC_VERSION=\$(git rev-parse --short HEAD)"
  missing=1
fi

if [[ -f "$ETC/hmac" ]]; then
  # 길이만 본다. **값은 찍지 않는다.**
  bytes=$(wc -c <"$ETC/hmac")
  if (( bytes >= 32 )); then
    ok "HMAC 비밀 있음 (${bytes}바이트)"
  else
    warn "HMAC 비밀이 너무 짧습니다 (${bytes}바이트, 32 이상 필요)"
    missing=1
  fi
else
  warn "HMAC 비밀이 없습니다: $ETC/hmac"
  warn "  TYBot 쪽 \$TYBOT_SPECIALIST_V3_HMAC_DIR/hermes-shared.hmac 과 **같은 값**입니다."
  warn "  개별 worker 의 비밀을 복사하지 마세요 — 공유 전용으로 새로 만듭니다."
  warn "  여기서 만들지 않습니다."
  missing=1
fi

# ── 5. 명부 ───────────────────────────────────────────────────────────────
#
# JSON 문법과 비밀 인라인만 본다. 나머지(domain 값·키 파일 내용)는 worker 가 뜰 때
# 보고, 못 읽은 workspace 는 health 의 `workspaces` 에 `false` 로 나온다.
say ""
say "[명부]"
if [[ -f "$ETC/profiles.json" ]]; then
  if command -v python3 >/dev/null && python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$ETC/profiles.json" 2>/dev/null; then
    ok "profiles.json 이 올바른 JSON 입니다"
    if grep -Eq '"(api_key|apiKey|secret|token|password)"[[:space:]]*:' "$ETC/profiles.json"; then
      warn "명부에 비밀이 적혀 있는 것 같습니다. 키 **파일 경로**를 api_key_file 에 적으세요."
      warn "  이 파일은 사람이 열어 보고 화면에 띄우는 파일입니다."
      missing=1
    fi
    if grep -q '"\*"' "$ETC/profiles.json"; then
      warn "명부에 wildcard 가 있습니다 — 미등록 workspace 가 그냥 돕니다."
      missing=1
    fi
  else
    warn "profiles.json 을 읽지 못했습니다(JSON 오류이거나 python3 없음)."
    missing=1
  fi
else
  warn "명부가 없습니다: $ETC/profiles.json"
  warn "  받을 workspace 와 각자의 domain·키 파일을 적습니다. 예:"
  warn '  { "tyit": { "domain": "pf-construction", "api_key_file": "'"$ETC"'/keys/tyit" } }'
  warn "  비밀 값은 넣지 않습니다 — 키 파일 경로만 적습니다."
  missing=1
fi

# ── 6. 마무리 ─────────────────────────────────────────────────────────────
say ""
if (( missing )); then
  say "모자란 것이 있습니다. 위 명령을 먼저 끝내세요."
  say "절차: subbots/hermes/deploy/README.md 의 「공유 RPC worker」"
fi
say "켜는 것은 사람이 합니다(이 스크립트는 enable 도 start 도 하지 않습니다):"
say "  systemctl enable --now hermes-rpc-shared"
say ""
say "**TYIT 만 먼저 공유 경로로 보내고, 확인한 뒤 넓히세요**(인계 §재시작) —"
say "재시작 하나가 공유 대상 전체를 끊습니다."
exit 0
