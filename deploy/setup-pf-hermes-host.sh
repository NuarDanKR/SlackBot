#!/usr/bin/env bash
# pf-hermes 전용 계정·경로·권한을 만들고 **격리를 실제로 확인한다**.
#
#   sudo /opt/tybot/deploy/setup-pf-hermes-host.sh --dry-run   # 무엇을 할지만 본다
#   sudo /opt/tybot/deploy/setup-pf-hermes-host.sh             # 만들고 확인
#   sudo /opt/tybot/deploy/setup-pf-hermes-host.sh --verify     # 확인만 다시
#
# 근거: docs/design/pf-hermes-owner-plan.md §7·§13,
#       docs/design/pf-hermes-migration-handoff.md §5
#
# ## 왜 확인이 같이 들어 있나
# 완료 게이트의 첫 줄은 「Hermes 계정이 TYBot env/archive/DB 를 읽지 못한다」 다(§13).
# 그런데 계정과 경로를 만드는 것과 **못 읽는 것을 확인하는 것**은 다른 일이고,
# 확인을 따로 두면 안 한다. 만든 직후가 확인하기 가장 쉬운 때다.
#
# 확인은 「권한을 줬으니 됐겠지」 가 아니라 **실제로 읽어 보고 거부되는지** 본다.
# `chmod` 가 맞아도 상위 디렉터리 권한이나 ACL 때문에 열려 있을 수 있다.
#
# ## 하지 않는 것
# - **시크릿을 쓰지 않는다.** `pf-hermes.env` 는 빈 골격만 만들고 값은 사람이 넣는다.
#   값을 스크립트가 받으면 shell 이력과 배포 로그에 남는다
# - **Hermes 코드를 배치하지 않는다.** 그건 프금팀 release 이고 §9 단계다
# - **unit 을 만들지 않는다.** 프금팀이 제출한 unit 을 쓴다(인계서 §8-2)
set -euo pipefail

SVC=pf-hermes
CODE_ROOT=/opt/tybot-subbots/$SVC
STATE_ROOT=/var/lib/tybot-subbots/$SVC
ARCHIVE_ROOT=$STATE_ROOT/archive
STATE_DIR=$STATE_ROOT/state
CONF_DIR=/etc/tybot-subbots
ENV_FILE=$CONF_DIR/$SVC.env
CONFIG_FILE=$CONF_DIR/$SVC.json
KEY_DIR=$CONF_DIR/$SVC-keys
BACKUP_DIR=/var/backups/$SVC
RUN_ROOT=/run/tybot-subbots/$SVC

# 이 계정이 **읽으면 안 되는** 것들. §13 의 첫 줄이 이 목록이다.
FORBIDDEN=(
  /etc/tybot/tybot.env
  /var/lib/tybot/archive
  /var/lib/tybot/qa-log
)

DRY_RUN=0
VERIFY_ONLY=0
case "${1:-}" in
  --dry-run) DRY_RUN=1 ;;
  --verify)  VERIFY_ONLY=1 ;;
  "")        ;;
  *) echo "알 수 없는 인자: $1 (--dry-run | --verify)"; exit 2 ;;
esac

log() { echo "[$(date '+%F %T')] $*"; }
run() { if ((DRY_RUN)); then echo "    (dry-run) $*"; else "$@"; fi; }

[[ $EUID -eq 0 ]] || { echo "root 로 실행하세요 (sudo)"; exit 1; }

# ---------------------------------------------------------------------------
# 만들기
# ---------------------------------------------------------------------------
if ((VERIFY_ONLY == 0)); then
  # 1. 계정 — 로그인 불가, home 디렉터리를 자동 생성하지 않는다. 사람이 쓰는 계정이 아니다.
  if ! getent group "$SVC" >/dev/null 2>&1; then
    log "그룹 $SVC 을 만듭니다"
    run groupadd --system "$SVC"
  fi
  if id "$SVC" >/dev/null 2>&1; then
    log "계정 $SVC 이 이미 있습니다"
    if [[ "$(id -gn "$SVC")" != "$SVC" ]]; then
      echo "기존 $SVC 계정의 기본 그룹이 $SVC 가 아닙니다. 자동 변경하지 않습니다." >&2
      exit 3
    fi
    account_shell="$(getent passwd "$SVC" | cut -d: -f7)"
    if [[ "$account_shell" != "/sbin/nologin" && "$account_shell" != "/usr/sbin/nologin" ]]; then
      echo "기존 $SVC 계정의 shell이 nologin이 아닙니다. 자동 변경하지 않습니다." >&2
      exit 3
    fi
  else
    log "계정 $SVC 을 만듭니다 (로그인 불가)"
    run useradd --system --gid "$SVC" --no-create-home \
      --home-dir "$STATE_ROOT" --shell /sbin/nologin "$SVC"
  fi

  # 2. 코드 release 는 root 가 고정하고, 변경되는 archive/state 만 서비스가 쓴다.
  log "경로를 만듭니다"
  run install -d -m 0755 -o root  -g root  /opt/tybot-subbots
  run install -d -m 0750 -o root  -g "$SVC" "$CODE_ROOT"
  run install -d -m 0750 -o root  -g "$SVC" "$CODE_ROOT/releases"
  run install -d -m 0750 -o root  -g root  /var/lib/tybot-subbots
  run install -d -m 0750 -o "$SVC" -g "$SVC" "$STATE_ROOT"
  run install -d -m 0750 -o "$SVC" -g "$SVC" "$ARCHIVE_ROOT"
  run install -d -m 0750 -o "$SVC" -g "$SVC" "$STATE_DIR"
  run install -d -m 0750 -o root  -g root  "$CONF_DIR"
  run install -d -m 0750 -o root  -g "$SVC" "$KEY_DIR"
  run install -d -m 0750 -o root  -g root  "$BACKUP_DIR"
  run install -d -m 0750 -o "$SVC" -g "$SVC" "$RUN_ROOT"

  # `/var/lib/tybot-subbots` 는 PF 콘솔(`tybot-pf`)도 **읽어야** 한다. 상태 파일을
  # 보는 것이 그 화면의 전부다. 쓰기는 주지 않는다.
  if id tybot-pf >/dev/null 2>&1; then
    log "PF 콘솔 계정에 상태 파일 읽기 권한을 줍니다 (읽기만)"
    run setfacl -m u:tybot-pf:rx /var/lib/tybot-subbots
    run setfacl -m u:tybot-pf:rx "$STATE_ROOT"
    run setfacl -d -m u:tybot-pf:r "$STATE_ROOT"
  else
    log "PF 콘솔 계정(tybot-pf)이 없습니다 — 상태 읽기 권한은 나중에 줍니다"
  fi

  # 3. 설정 골격. **값은 넣지 않는다.**
  if [[ -f "$ENV_FILE" ]]; then
    log "설정 파일이 이미 있습니다: $ENV_FILE (건드리지 않습니다)"
  else
    log "설정 골격을 만듭니다: $ENV_FILE — 값은 사람이 넣습니다"
    if ((DRY_RUN)); then
      echo "    (dry-run) $ENV_FILE 골격 생성"
    else
      cat > "$ENV_FILE" <<'ENVSKEL'
# pf-hermes 전용 설정. **값은 여기에 직접 넣는다.** 배포·채팅·문서에 붙이지 않는다.
#
# 인계서 §3.6 의 offline check 가 아래를 실패로 잡는다.
#   - 경로가 TYBot 을 가리키면 실패
#   - 이 파일이 root:pf-hermes 0640 이 아니거나 other 에게 읽히면 실패
#   - token·key 가 비었거나 예시값이면 실패
#
# 경로 (내부 실행 계획 §7)
HERMES_INSTANCE=pf
HERMES_SERVICE_NAME=pf-hermes
HERMES_ENV_FILE=/etc/tybot-subbots/pf-hermes.env
HERMES_CONFIG_FILE=/etc/tybot-subbots/pf-hermes.json
HERMES_STATE_ROOT=/var/lib/tybot-subbots/pf-hermes/state
HERMES_DATA_ROOT=/var/lib/tybot-subbots/pf-hermes/archive
HERMES_RUNTIME_ROOT=/run/tybot-subbots/pf-hermes

# Slack — shadow 중에는 기존 GCP 와 같은 token 을 쓰지 않는다. 최종 전환에서는 기존
# 프로세스가 0개인 것을 확인한 뒤 운영 token 을 이쪽 한 프로세스에만 둔다.
SLACK_BOT_TOKEN=
SLACK_APP_TOKEN=

# 모델 — 계정과 일일 상한의 주인은 프금팀이다(§7.1)
ANTHROPIC_API_KEY=

# 자료 저장소 — 코드 Git 은 read-only, 자료 Git 은 read/write 로 키를 나눈다
ARCHIVE_GIT_REMOTE=
CODE_GIT_SSH_KEY=/etc/tybot-subbots/pf-hermes-keys/code
ARCHIVE_GIT_SSH_KEY=/etc/tybot-subbots/pf-hermes-keys/archive

# shadow 동안은 쓰기를 막는다. 운영 전환 창에서만 푼다.
HERMES_READ_ONLY=1
ENVSKEL
      chown root:"$SVC" "$ENV_FILE"
      chmod 640 "$ENV_FILE"
    fi
  fi

  if [[ -f "$CONFIG_FILE" ]]; then
    log "설정 파일이 이미 있습니다: $CONFIG_FILE (건드리지 않습니다)"
  else
    log "비시크릿 설정 골격을 만듭니다: $CONFIG_FILE"
    if ((DRY_RUN)); then
      echo "    (dry-run) $CONFIG_FILE 골격 생성"
    else
      printf '{}\n' > "$CONFIG_FILE"
      chown root:"$SVC" "$CONFIG_FILE"
      chmod 640 "$CONFIG_FILE"
    fi
  fi

  if command -v restorecon >/dev/null 2>&1; then
    log "SELinux context 를 복원합니다"
    run restorecon -RF "$CODE_ROOT" "$STATE_ROOT" "$CONF_DIR" "$BACKUP_DIR"
  else
    log "restorecon 이 없어 SELinux context 복원을 건너뜁니다"
  fi
fi

# ---------------------------------------------------------------------------
# 확인 — 실제로 읽어 본다
# ---------------------------------------------------------------------------
if ((DRY_RUN)); then
  log "dry-run 이므로 격리 확인을 건너뜁니다"
  exit 0
fi

log "격리를 확인합니다 (§13 완료 게이트)"
fail=0

check_denied() {
  local path="$1"
  if [[ ! -e "$path" ]]; then
    echo "    - $path — 없음 (확인 생략)"
    return 0
  fi
  # 파일이면 읽어 보고, 디렉터리면 목록을 열어 본다.
  local cmd=(cat "$path")
  [[ -d "$path" ]] && cmd=(ls -A "$path")
  if runuser -u "$SVC" -- "${cmd[@]}" >/dev/null 2>&1; then
    echo "    ✗ $path — **읽힙니다.** 격리가 깨졌습니다"
    fail=1
  else
    echo "    ✓ $path — 거부됨"
  fi
}

for path in "${FORBIDDEN[@]}"; do
  check_denied "$path"
done

# DB. 소켓이 열려 있어도 TYBot DB 에 붙으면 안 된다.
if command -v psql >/dev/null 2>&1; then
  if runuser -u "$SVC" -- psql -p "${TYBOT_DB_PORT:-55432}" -d "${TYBOT_DB_NAME:-tyslackai}" \
       -c 'SELECT 1' >/dev/null 2>&1; then
    echo "    ✗ TYBot DB — **붙습니다.** 격리가 깨졌습니다"
    fail=1
  else
    echo "    ✓ TYBot DB — 거부됨"
  fi
else
  echo "    - psql 이 없어 DB 확인을 건너뜁니다"
fi

# 자기 것은 읽고 쓸 수 있어야 한다. 막히는 것만 확인하면 「전부 막혔는데 서비스도
# 안 도는」 상태를 통과로 읽는다.
if runuser -u "$SVC" -- test -r "$ENV_FILE"; then
  echo "    ✓ $ENV_FILE — 자기 설정은 읽힘"
else
  echo "    ✗ $ENV_FILE — 자기 설정을 못 읽습니다. 서비스가 뜨지 않습니다"
  fail=1
fi
if runuser -u "$SVC" -- test -r "$CONFIG_FILE"; then
  echo "    ✓ $CONFIG_FILE — 자기 비시크릿 설정은 읽힘"
else
  echo "    ✗ $CONFIG_FILE — 자기 비시크릿 설정을 못 읽습니다"
  fail=1
fi
if runuser -u "$SVC" -- test -w "$STATE_ROOT"; then
  echo "    ✓ $STATE_ROOT — 자기 state 는 쓰임"
else
  echo "    ✗ $STATE_ROOT — 자기 state 에 못 씁니다"
  fail=1
fi
if runuser -u "$SVC" -- test -w "$ARCHIVE_ROOT"; then
  echo "    ✓ $ARCHIVE_ROOT — archive 를 쓸 수 있음"
else
  echo "    ✗ $ARCHIVE_ROOT — archive 에 못 씁니다"
  fail=1
fi

# 설정 파일이 남에게 읽히면 안 된다(인계서 §3.6).
perm="$(stat -c '%a %U:%G' "$ENV_FILE" 2>/dev/null || echo '?')"
if [[ "$perm" == "640 root:$SVC" ]]; then
  echo "    ✓ $ENV_FILE 권한 $perm"
else
  echo "    ✗ $ENV_FILE 권한이 $perm 입니다 (640 root:$SVC 여야 합니다)"
  fail=1
fi

config_perm="$(stat -c '%a %U:%G' "$CONFIG_FILE" 2>/dev/null || echo '?')"
if [[ "$config_perm" == "640 root:$SVC" ]]; then
  echo "    ✓ $CONFIG_FILE 권한 $config_perm"
else
  echo "    ✗ $CONFIG_FILE 권한이 $config_perm 입니다 (640 root:$SVC 여야 합니다)"
  fail=1
fi

if command -v node >/dev/null 2>&1; then
  node_major="$(node -p "process.versions.node.split('.')[0]" 2>/dev/null || echo 0)"
else
  node_major=0
fi
if [[ "$node_major" =~ ^[0-9]+$ ]] && ((node_major >= 20)); then
  echo "    ✓ Node $(node --version)"
else
  echo "    ✗ Node.js 20 이상이 필요합니다 (현재: $(node --version 2>/dev/null || echo 없음))"
  fail=1
fi

# PF 콘솔이 상태를 읽을 수 있어야 화면이 뜬다.
if id tybot-pf >/dev/null 2>&1; then
  if runuser -u tybot-pf -- test -r "$STATE_ROOT"; then
    echo "    ✓ $STATE_ROOT — PF 콘솔이 읽을 수 있음"
  else
    echo "    ✗ $STATE_ROOT — PF 콘솔이 못 읽습니다. /pf/ 가 「상태 없음」 으로만 보입니다"
    fail=1
  fi
fi

echo
if ((fail)); then
  log "격리 확인 실패 — 위 ✗ 를 고치고 --verify 로 다시 확인하세요."
  log "통과하기 전에는 pf-hermes unit 을 만들지 않습니다(runbook 08:20 단계)."
  exit 3
fi
log "격리 확인 통과. 다음은 프금팀 release 배치입니다(runbook 09:00 단계)."
