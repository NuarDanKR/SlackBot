#!/usr/bin/env bash
# TYBot 서버 설치 (Rocky Linux 8) — 멱등. 여러 번 실행해도 안전하다.
#
#   sudo bash deploy/install.sh            # 온라인(PyPI 접근 가능)
#   sudo OFFLINE=1 bash deploy/install.sh  # 오프라인(./wheels 디렉터리 사용)
#
# 하지 않는 것: .env 값 입력(사람이 직접), 서비스 자동 시작(설치와 기동 분리).
set -euo pipefail

APP_DIR=/opt/tybot
DATA_DIR=/var/lib/tybot
CONF_DIR=/etc/tybot
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY=python3.11

[[ $EUID -eq 0 ]] || { echo "root 로 실행하세요 (sudo)"; exit 1; }

echo "== 1/6 시스템 패키지 =="
command -v $PY >/dev/null || dnf install -y python3.11 python3.11-pip
command -v rsync >/dev/null || dnf install -y rsync
command -v git   >/dev/null || dnf install -y git

# 콘솔 화면을 서버에서 빌드하므로 Node 가 필요하다.
# Rocky 8 기본 스트림은 Node 10 이고, 그 npm(6)은 lockfileVersion 3 을 읽지 못해
# `Cannot read property 'react' of undefined` 로 죽는다.
#
# **있는지만 보면 안 된다.** Node 10 이 이미 깔려 있으면 그대로 통과해 버리고,
# 빌드 단계에 가서야 저 오류를 만난다. 버전을 재서 낮으면 올린다.
node_too_old() {
  command -v node >/dev/null || return 0
  local major minor
  major=$(node -p "process.versions.node.split('.')[0]" 2>/dev/null) || return 0
  minor=$(node -p "process.versions.node.split('.')[1]" 2>/dev/null) || return 0
  # Vite 7 이 요구하는 최소값이다. `set -e` 아래에서 && 사슬은 읽기 어렵고
  # 마지막 항이 실패하면 스크립트가 통째로 죽을 수 있어 if 로 쓴다.
  if (( major > 20 )); then
    return 1
  fi
  if (( major == 20 )) && (( minor >= 19 )); then
    return 1
  fi
  return 0
}

if [[ "${WITH_CONSOLE:-0}" == "1" && "${OFFLINE:-0}" != "1" ]] && node_too_old; then
  echo "  Node 올리기(nodejs:20) — 현재 $(command -v node >/dev/null && node -v || echo 없음)"
  dnf module reset -y nodejs >/dev/null 2>&1 || true
  dnf module enable -y nodejs:20 >/dev/null 2>&1 || true
  # 이미 낡은 Node 가 깔려 있으면 `install` 은 '이미 설치됨' 으로 끝난다.
  # distro-sync 가 설치된 패키지를 새 스트림 버전으로 옮긴다.
  dnf distro-sync -y nodejs npm >/dev/null 2>&1 || dnf install -y nodejs >/dev/null 2>&1 || true
  if node_too_old; then
    echo "  ! Node 를 올리지 못했습니다($(node -v 2>/dev/null || echo 없음)) — 화면 빌드는 건너뜁니다"
  else
    echo "  Node $(node -v) 준비됨"
  fi
fi

# 요약 기간 계산이 서버 로컬 날짜를 쓴다 - 시간대가 KST 여야 "오늘/이번주"가 맞는다.
if [[ "$(timedatectl show -p Timezone --value 2>/dev/null)" != "Asia/Seoul" ]]; then
  echo "  ! 시간대가 Asia/Seoul 이 아닙니다: sudo timedatectl set-timezone Asia/Seoul"
fi

echo "== 2/6 계정·디렉터리 =="
id tybot &>/dev/null || useradd -r -d "$DATA_DIR" -s /sbin/nologin tybot
mkdir -p "$APP_DIR" "$CONF_DIR" "$DATA_DIR"/{archive,cache,qa-log,reports}
chown -R tybot:tybot "$DATA_DIR"
chmod 750 "$DATA_DIR"

echo "== 3/6 코드 배치 =="
if [[ "$SRC_DIR" != "$APP_DIR" ]]; then
  # .env·아카이브·git 메타는 옮기지 않는다(시크릿은 /etc, 데이터는 /var/lib)
  # 주의: 패턴 앞의 '/' 는 전송 루트 고정이다. 'archive' 로 쓰면 하위의
  # src/tybot/archive/ 까지 제외되어 봇이 ModuleNotFoundError 로 죽는다.
  rsync -a --delete \
    --exclude '/.git' --exclude '/.venv' --exclude '/.env' --exclude '/archive' \
    --exclude '/wheels' \
    --exclude '/console-web/node_modules' --exclude '/console-web/dist' \
    --exclude '__pycache__' --exclude '.pytest_cache' --exclude '*.egg-info' \
    "$SRC_DIR"/ "$APP_DIR"/

  # 배치 결과를 검증한다 — 조용히 빠진 모듈이 가장 잡기 어렵다.
  for m in answer.py intent.py archive/store.py archive/writer.py slack/pilot.py; do
    [[ -f "$APP_DIR/src/tybot/$m" ]] || { echo "배치 누락: src/tybot/$m"; exit 1; }
  done
fi

echo "== 4/6 가상환경 =="
[[ -x "$APP_DIR/.venv/bin/python" ]] || $PY -m venv "$APP_DIR/.venv"
if [[ "${OFFLINE:-0}" == "1" ]]; then
  "$APP_DIR/.venv/bin/pip" install --no-index --find-links "$APP_DIR/wheels" \
    -r "$APP_DIR/requirements.txt"
else
  "$APP_DIR/.venv/bin/pip" install --upgrade pip -q
  "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q
fi
"$APP_DIR/.venv/bin/pip" install -e "$APP_DIR" --no-deps -q

# 관리 콘솔은 선택 설치다. `-e . --no-deps` 는 extras 를 건너뛰므로 따로 깐다.
# 이걸 빼먹으면 콘솔이 ModuleNotFoundError 로 기동하지 않고, 배포 테스트도 실패한다.
if [[ "${WITH_CONSOLE:-0}" == "1" ]]; then
  echo "  콘솔 의존성 설치"
  if [[ "${OFFLINE:-0}" == "1" ]]; then
    "$APP_DIR/.venv/bin/pip" install --no-index --find-links "$APP_DIR/wheels" \
      -r "$APP_DIR/deploy/requirements-console.txt"
  else
    "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/deploy/requirements-console.txt" -q
  fi

  # --- 콘솔 화면 빌드 ---
  # **실패해도 설치를 멈추지 않는다.** 화면이 안 만들어지는 것과 봇이 못 뜨는 것은
  # 무게가 다르다. 여기서 exit 하면 프런트엔드 사정으로 봇 배포가 막힌다.
  build_console() {
    # Vite 7 은 Node 20.19+ 를 요구한다. 낮으면 알 수 없는 오류로 죽는다.
    if node_too_old; then
      echo "  ! Node $(node -v 2>/dev/null || echo 없음) 로는 빌드할 수 없습니다(20.19+ 필요)"
      return 1
    fi
    ( cd "$APP_DIR/console-web" && npm ci --no-audit --no-fund && npm run build ) || return 1
    [[ -f "$APP_DIR/console-web/dist/index.html" ]] || return 1
  }

  if [[ "${OFFLINE:-0}" == "1" ]]; then
    echo "  화면 빌드 건너뜀(OFFLINE=1) — dist 를 직접 올려 두세요"
  else
    echo "  콘솔 화면 빌드"
    if build_console; then
      echo "  화면 빌드 완료: $APP_DIR/console-web/dist"
    else
      CONSOLE_BUILD_FAILED=1
      echo "  ! 화면 빌드 실패 — API 는 뜨지만 화면은 안 나옵니다. 설치는 계속합니다."
    fi
  fi
fi

echo "== 5/6 권한 (코드는 봇이 수정 불가) =="
# node_modules 는 건너뛴다 — 파일이 수만 개라 매 배포마다 수십 초가 든다.
# 빌드 산출물(dist)만 봇이 읽을 수 있으면 된다.
find "$APP_DIR" -path "$APP_DIR/console-web/node_modules" -prune -o -print0 |
  xargs -0 -r chown root:tybot
find "$APP_DIR" -path "$APP_DIR/console-web/node_modules" -prune -o -print0 |
  xargs -0 -r chmod g-w,o-rwx
chmod -R u+w "$APP_DIR/.venv"

echo "== 6/6 설정 파일·서비스 =="
if [[ ! -f "$CONF_DIR/tybot.env" ]]; then
  install -m 0640 -o root -g tybot "$APP_DIR/.env.example" "$CONF_DIR/tybot.env"
  sed -i 's|^ARCHIVE_DIR=.*|ARCHIVE_DIR=/var/lib/tybot/archive|' "$CONF_DIR/tybot.env"
  sed -i 's|^QA_LOG_DIR=.*|QA_LOG_DIR=/var/lib/tybot/qa-log|' "$CONF_DIR/tybot.env"
  sed -i 's|^REPORTS_DIR=.*|REPORTS_DIR=/var/lib/tybot/reports|' "$CONF_DIR/tybot.env"
  echo "  → $CONF_DIR/tybot.env 생성됨. 토큰·키를 직접 입력하세요(REPLACE_ME)."
else
  echo "  → $CONF_DIR/tybot.env 이미 존재 — 시크릿은 건드리지 않음."
  # 경로 키만 보강한다. 값이 이미 있으면 절대 덮지 않고, 없거나 비었을 때만 추가한다.
  # 이 키가 빠지면 봇이 코드 경로(읽기 전용)에 쓰려 해서 기동 자체가 막힌다.
  for kv in ARCHIVE_DIR=/var/lib/tybot/archive QA_LOG_DIR=/var/lib/tybot/qa-log REPORTS_DIR=/var/lib/tybot/reports STATE_DIR=/var/lib/tybot; do
    k="${kv%%=*}"
    if grep -qE "^${k}=.+" "$CONF_DIR/tybot.env"; then
      continue
    fi
    sed -i -E "/^${k}=\s*$/d" "$CONF_DIR/tybot.env"   # 빈 값 줄 제거 후 추가
    echo "$kv" >> "$CONF_DIR/tybot.env"
    echo "  → 누락된 $k 를 추가했습니다: $kv"
  done
fi
# 콘솔이 화면 파일을 어디서 읽는지. 없으면 API 만 열리고 브라우저에는 404 가 뜬다.
if [[ "${WITH_CONSOLE:-0}" == "1" ]] && ! grep -qE "^CONSOLE_DIST=.+" "$CONF_DIR/tybot.env"; then
  sed -i -E "/^CONSOLE_DIST=[[:space:]]*$/d" "$CONF_DIR/tybot.env"
  echo "CONSOLE_DIST=$APP_DIR/console-web/dist" >> "$CONF_DIR/tybot.env"
  echo "  → CONSOLE_DIST 를 추가했습니다: $APP_DIR/console-web/dist"
fi

install -m 0644 "$APP_DIR/deploy/tybot.service" /etc/systemd/system/tybot.service
# 타이머(자동배포·정기백필·점검)는 파일만 배치한다. enable 은 운영자가 결정한다.
for u in tybot-update tybot-collect tybot-tidy tybot-schedule-sync tybot-schedule-dm; do
  install -m 0644 "$APP_DIR/deploy/$u.service" "/etc/systemd/system/$u.service"
  install -m 0644 "$APP_DIR/deploy/$u.timer"   "/etc/systemd/system/$u.timer"
done
if [[ "${WITH_CONSOLE:-0}" == "1" ]]; then
  install -m 0644 "$APP_DIR/deploy/tybot-console.service" /etc/systemd/system/tybot-console.service
fi
install -m 0644 "$APP_DIR/deploy/tybot-deploy.service" /etc/systemd/system/tybot-deploy.service
install -m 0644 "$APP_DIR/deploy/tybot-deploy.path"    /etc/systemd/system/tybot-deploy.path
systemctl daemon-reload

cat <<EOF

설치 완료. 다음 순서로 진행하세요:
  1) sudo vi $CONF_DIR/tybot.env          # SLACK_BOT_TOKEN / SLACK_APP_TOKEN / ANTHROPIC_API_KEY
  2) sudo -u tybot $APP_DIR/.venv/bin/python $APP_DIR/scripts/check_env.py
  3) 로컬 PC 봇을 먼저 끄고:  sudo systemctl enable --now tybot
  4) journalctl -u tybot -f
EOF

if [[ "${WITH_CONSOLE:-0}" == "1" ]]; then
cat <<EOF
관리 콘솔:
  5) 계정을 만드세요(안 만들면 admin/1111 임시 계정으로 열립니다):
       $APP_DIR/.venv/bin/python -m tybot.console.auth <새비밀번호>
       → 나온 해시를 $CONF_DIR/tybot.env 의 CONSOLE_ACCOUNTS 에 넣습니다
  6) sudo systemctl enable --now tybot-console
  7) 127.0.0.1:8787 에만 열립니다. 외부에서 보려면 앞단(nginx 등)을 두세요.
EOF
if [[ "${CONSOLE_BUILD_FAILED:-0}" == "1" ]]; then
  echo "  ! 화면 빌드가 실패했습니다. 콘솔 API 는 뜨지만 브라우저에는 404 가 나옵니다."
  echo "    Node 20.19+ 설치 후 다시 실행하거나, dist 를 직접 올리세요."
fi
fi
