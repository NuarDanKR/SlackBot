#!/usr/bin/env bash
# PF 운영 콘솔(`/pf/`)을 설치한다. 여러 번 실행해도 안전하다.
#
#   sudo /opt/tybot/deploy/setup-pf-console.sh --dry-run   # 무엇을 할지만 본다
#   sudo /opt/tybot/deploy/setup-pf-console.sh
#   sudo /opt/tybot/deploy/setup-pf-console.sh --verify     # 확인만 다시
#
# 설계: docs/design/pf-console.md §8
#
# ## 왜 스크립트인가
# 단계가 여섯인데 전부 수동이었다. 2026-09-22 에 그 수동 단계들이 두 번 사고를
# 냈다 — 배포는 자동인데 수동 단계는 따라오지 않아서, 한 번은 콘솔이 잠겼고
# 한 번은 문서 변환기 하나가 배포를 끊었다.
#
# 「빠뜨려도 조용히 실패하는」 것이 셋이다.
# - DB role 이 없으면 로그인이 503 이다. 화면에는 「서버 오류」 만 뜬다
# - `dist-pf` 가 없으면 API 만 뜨고 브라우저에는 빈 404 가 보인다
# - `console_user_service` 행이 없으면 로그인은 되는데 아무것도 안 보인다
#
# 셋 다 「PF 콘솔이 고장났나」 로 읽힌다. 그래서 각각을 확인하고 **다른 문장**으로
# 말한다.
#
# ## 하지 않는 것
# - **시크릿을 인자로 받지 않는다.** DB 암호와 세션 키는 사람이 env 에 넣는다.
#   인자로 받으면 shell 이력과 배포 로그에 남는다
# - **DB role 을 만들지 않는다.** `CREATE ROLE` 은 암호를 받아야 하고, 그 암호가
#   이 스크립트를 지나가면 안 된다. 명령만 출력한다
# - **권한을 주지 않는다.** 누구에게 PF 를 열지는 사람의 판단이다.
#   `scripts/pf_grant.py` 로 따로 한다
set -euo pipefail

APP_DIR=/opt/tybot
SVC_USER=tybot-pf
CONF_DIR=/etc/tybot-pf
ENV_FILE=$CONF_DIR/console.env
UNIT=pf-hermes-console.service
DIST=$APP_DIR/console-web/dist-pf

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
  # 1. 계정 — TYBot 콘솔(`tybot`)과 다르다. 같은 계정이면 /etc/tybot/tybot.env 를
  #    읽을 수 있고, 그 파일에 TYBot 의 DATABASE_URL 과 Slack·Anthropic 키가 있다.
  if ! getent group "$SVC_USER" >/dev/null 2>&1; then
    log "그룹 $SVC_USER 을 만듭니다"
    run groupadd --system "$SVC_USER"
  fi
  if id "$SVC_USER" >/dev/null 2>&1; then
    log "계정 $SVC_USER 이 이미 있습니다"
  else
    log "계정 $SVC_USER 을 만듭니다 (로그인 불가)"
    run useradd --system --gid "$SVC_USER" --no-create-home \
      --shell /sbin/nologin "$SVC_USER"
  fi

  # 2. 설정 골격. **값은 넣지 않는다.**
  run install -d -m 0750 -o root -g "$SVC_USER" "$CONF_DIR"
  if [[ -f "$ENV_FILE" ]]; then
    log "설정 파일이 이미 있습니다: $ENV_FILE (건드리지 않습니다)"
  else
    log "설정 골격을 만듭니다: $ENV_FILE — 값은 사람이 넣습니다"
    if ((DRY_RUN)); then
      echo "    (dry-run) $ENV_FILE 골격 생성"
    else
      cat > "$ENV_FILE" <<ENVSKEL
# PF 운영 콘솔 전용 설정. **TYBot 의 /etc/tybot/tybot.env 를 읽지 않는다.**
#
# 값을 넣은 뒤: sudo systemctl restart $UNIT

# 전용 DB role. TYBot 의 DATABASE_URL 을 쓰지 않는다 — 쓰면 분리해 둔 권한이
# 의미를 잃고, 코드는 PF role 로 붙는 줄 알지만 실제로는 TYBot 표 전부가 열린다.
PF_DATABASE_URL=postgresql://tybot_pf_console:<암호>@127.0.0.1:55432/tyslackai

# 세션 서명 키.  openssl rand -base64 32
PF_CONSOLE_SECRET=

PF_CONSOLE_DIST=$DIST
PF_STATE_ROOT=/var/lib/tybot-subbots

# **TLS 를 붙인 뒤에 1 로 바꾼다.** 지금처럼 평문으로 여는 동안 1 이면 브라우저가
# 쿠키를 보내지 않아 로그인이 안 되고, 화면에는 「비밀번호가 틀렸다」 처럼 보인다.
PF_CONSOLE_COOKIE_SECURE=0
ENVSKEL
      chown root:"$SVC_USER" "$ENV_FILE"
      chmod 640 "$ENV_FILE"
    fi
  fi

  # 3. unit
  if [[ -f "$APP_DIR/deploy/$UNIT" ]]; then
    log "unit 을 배치합니다: /etc/systemd/system/$UNIT"
    run install -m 0644 "$APP_DIR/deploy/$UNIT" "/etc/systemd/system/$UNIT"
    run systemctl daemon-reload
  else
    echo "  ! unit 원본이 없습니다: $APP_DIR/deploy/$UNIT — 코드를 먼저 배포하세요" >&2
    exit 2
  fi

  # 4. 바인딩. nginx 가 없으면 install.sh 안전장치가 0.0.0.0 으로 유지한다.
  log "바인딩 안전장치를 적용합니다 (install.sh 와 같은 판단을 쓴다)"
  run env WITH_CONSOLE=1 TYBOT_INSTALL_HINTS=0 bash "$APP_DIR/deploy/install.sh" \
    >/dev/null 2>&1 || true
fi

# ---------------------------------------------------------------------------
# 확인 — 빠뜨려도 조용히 실패하는 셋을 각각 다른 문장으로 말한다
# ---------------------------------------------------------------------------
if ((DRY_RUN)); then
  log "dry-run 이므로 확인을 건너뜁니다"
  exit 0
fi

log "PF 콘솔을 확인합니다"
fail=0
todo=()

# (a) 화면
if [[ -f "$DIST/index.html" ]]; then
  echo "    ✓ 화면 빌드 있음: $DIST"
else
  echo "    ✗ 화면 빌드가 없습니다: $DIST"
  echo "      → API 는 뜨지만 브라우저에는 빈 404 가 보입니다"
  todo+=("cd $APP_DIR/console-web && npm run build:all   (또는 배포를 다시 실행)")
  fail=1
fi

# (b) 설정
if [[ -f "$ENV_FILE" ]] && ! grep -q '<암호>' "$ENV_FILE" && grep -q '^PF_CONSOLE_SECRET=.\+' "$ENV_FILE"; then
  echo "    ✓ 설정 값이 채워져 있음"
else
  echo "    ✗ 설정이 아직 골격입니다: $ENV_FILE"
  echo "      → 로그인이 503 으로 답하고 화면에는 「서버 오류」 만 뜹니다"
  todo+=("sudo vi $ENV_FILE    # PF_DATABASE_URL 암호와 PF_CONSOLE_SECRET")
  fail=1
fi
perm="$(stat -c '%a %U:%G' "$ENV_FILE" 2>/dev/null || echo '?')"
if [[ "$perm" == "640 root:$SVC_USER" ]]; then
  echo "    ✓ 설정 권한 $perm"
else
  echo "    ✗ 설정 권한이 $perm 입니다 (640 root:$SVC_USER 여야 합니다)"
  fail=1
fi

# (c) DB role — 실제로 붙어 본다. 「만들었으니 됐겠지」 가 아니다.
if [[ -f "$ENV_FILE" ]] && command -v psql >/dev/null 2>&1; then
  url="$(grep -m1 '^PF_DATABASE_URL=' "$ENV_FILE" | cut -d= -f2-)"
  if [[ -n "$url" && "$url" != *"<암호>"* ]] \
     && runuser -u "$SVC_USER" -- psql "$url" -c 'SELECT 1 FROM managed_service LIMIT 1' >/dev/null 2>&1; then
    echo "    ✓ PF DB role 로 PF 표를 읽을 수 있음"
    # 반대 방향도 본다. TYBot 표가 읽히면 격리가 깨진 것이다.
    if runuser -u "$SVC_USER" -- psql "$url" -c 'SELECT 1 FROM raw_line LIMIT 1' >/dev/null 2>&1; then
      echo "    ✗ PF DB role 이 TYBot 표(raw_line)를 읽습니다 — 격리가 깨졌습니다"
      fail=1
    else
      echo "    ✓ PF DB role 이 TYBot 표에 닿지 못함"
    fi
  else
    echo "    ✗ PF DB role 로 PF 표를 읽지 못합니다"
    echo "      → 로그인이 503 으로 답합니다. role 이 없거나 GRANT 가 안 붙은 상태입니다"
    todo+=("sudo -u postgres psql -p 55432 -d tyslackai -c \"CREATE ROLE tybot_pf_console LOGIN PASSWORD '<암호>';\"")
    todo+=("sudo $APP_DIR/deploy/apply-schema.sh   # GRANT 가 role 존재를 보고 붙는다")
    fail=1
  fi
else
  echo "    - psql 이 없어 DB 확인을 건너뜁니다"
fi

# (d) 서비스
if systemctl is-active --quiet "$UNIT"; then
  echo "    ✓ $UNIT 동작 중"
  port="$(ss -lntp 2>/dev/null | grep -o '[0-9.]*:8788' | head -1)"
  echo "    · 바인딩: ${port:-확인 실패}"
  if curl -sf --max-time 5 http://127.0.0.1:8788/pf/api/health >/dev/null 2>&1; then
    echo "    ✓ /pf/api/health 응답"
  else
    echo "    ✗ /pf/api/health 가 응답하지 않습니다 — journalctl -u $UNIT -n 30"
    fail=1
  fi
else
  echo "    ✗ $UNIT 이 동작하지 않습니다"
  todo+=("sudo systemctl enable --now $UNIT")
  fail=1
fi

# (e) 권한 행 — 있어야 화면에 무엇이든 보인다
if command -v psql >/dev/null 2>&1 \
   && sudo -u postgres psql -p "${TYBOT_DB_PORT:-55432}" -d "${TYBOT_DB_NAME:-tyslackai}" \
        -tAc 'SELECT count(*) FROM console_user_service' 2>/dev/null | grep -qE '^[1-9]'; then
  echo "    ✓ PF 권한이 부여된 계정이 있음"
else
  echo "    ✗ PF 권한 행이 없습니다"
  echo "      → 로그인은 되는데 「볼 수 있는 서비스가 없습니다」 만 보입니다"
  todo+=("sudo -u tybot $APP_DIR/.venv/bin/python $APP_DIR/scripts/pf_grant.py add <회사이메일> --role viewer")
fi

echo
if ((${#todo[@]})); then
  echo "■ 남은 일"
  for item in "${todo[@]}"; do
    echo "   $item"
  done
  echo
fi

if ((fail)); then
  log "아직 쓸 수 없습니다. 위 항목을 끝내고 --verify 로 다시 확인하세요."
  exit 3
fi
log "PF 콘솔 준비 완료 — http://<서버>:8788/pf/  (nginx 를 붙이면 https://<서버>/pf/)"
