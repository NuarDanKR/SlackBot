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
# 소스 체크아웃(`$DATA_DIR/src`)은 건드리지 않는다.
#
# 여기를 통째로 tybot 소유로 바꾸면 **다음 배포가 막힌다** — git 은 root 가 남의 소유
# 저장소에서 도는 것을 `dubious ownership` 으로 거부한다. 그 방어에는 이유가 있어서
# (저장소의 hook 이 root 로 실행된다) 예외를 두는 대신 소유를 나눈다.
find "$DATA_DIR" -mindepth 1 -maxdepth 1 ! -name src -exec chown -R tybot:tybot {} +
chown tybot:tybot "$DATA_DIR"
chmod 750 "$DATA_DIR"

echo "== 3/6 코드 배치 =="
if [[ "$SRC_DIR" != "$APP_DIR" ]]; then
  # .env·아카이브·git 메타는 옮기지 않는다(시크릿은 /etc, 데이터는 /var/lib)
  # 주의: 패턴 앞의 '/' 는 전송 루트 고정이다. 'archive' 로 쓰면 하위의
  # src/tybot/archive/ 까지 제외되어 봇이 ModuleNotFoundError 로 죽는다.
  # 배포 서비스가 이미 소스 디렉터리에 진입한 뒤 rsync가 절대경로로 다시 change_dir
  # 하면 SELinux/임시 디렉터리 정책에서 거부될 수 있다. 전송 루트 안에서 ./를 복사한다.
  (
    cd "$SRC_DIR"
    # `-a` 는 소유자·그룹까지 보존하려 한다. 그런데 **바로 다음 단계에서 소유권을
    # 직접 설정**하므로 보존할 이유가 없고, 실패만 만든다(chgrp Permission denied).
    # 권한·시각·심볼릭링크만 가져오고 소유는 건드리지 않는다.
    rsync -a --no-owner --no-group --delete \
      --exclude '/.git' --exclude '/.venv' --exclude '/.env' --exclude '/archive' \
      --exclude '/wheels' \
      --exclude '/console-web/node_modules' --exclude '/console-web/dist' \
      --exclude '__pycache__' --exclude '.pytest_cache' --exclude '*.egg-info' \
      ./ "$APP_DIR"/
  )

  # 배치 결과를 검증한다 — 조용히 빠진 모듈이 가장 잡기 어렵다.
  for m in answer.py intent.py archive/store.py archive/writer.py slack/pilot.py; do
    [[ -f "$APP_DIR/src/tybot/$m" ]] || { echo "배치 누락: src/tybot/$m"; exit 1; }
  done
fi

echo "== 4/6 가상환경 =="
[[ -x "$APP_DIR/.venv/bin/python" ]] || $PY -m venv "$APP_DIR/.venv"
# 콘솔을 함께 깔 때는 **한 번의 pip 호출로** 넘긴다.
# 따로 부르면 pip 이 앞서 깐 것을 고려하지 못해, 겉으로는 성공하고
# 실제로는 버전이 어긋난 상태가 남는다(typing-inspection 이 그랬다).
REQ_FILES=(-r "$APP_DIR/requirements.txt")
if [[ "${WITH_CONSOLE:-0}" == "1" ]]; then
  REQ_FILES+=(-r "$APP_DIR/deploy/requirements-console.txt")
fi

if [[ "${OFFLINE:-0}" == "1" ]]; then
  "$APP_DIR/.venv/bin/pip" install --no-index --find-links "$APP_DIR/wheels" \
    "${REQ_FILES[@]}"
else
  "$APP_DIR/.venv/bin/pip" install --upgrade pip -q
  "$APP_DIR/.venv/bin/pip" install "${REQ_FILES[@]}" -q
fi
"$APP_DIR/.venv/bin/pip" install -e "$APP_DIR" --no-deps -q

# 관리 콘솔은 선택 설치다. `-e . --no-deps` 는 extras 를 건너뛰므로 따로 깐다.
# 이걸 빼먹으면 콘솔이 ModuleNotFoundError 로 기동하지 않고, 배포 테스트도 실패한다.
if [[ "${WITH_CONSOLE:-0}" == "1" ]]; then

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
# `g+rX` 로 **읽기를 명시적으로 준다.** 예전에는 `g-w,o-rwx` 만 있었는데 그건 비트를
# 빼기만 한다. 배포가 umask 077 로 돌면 파일이 600 으로 만들어지고, 그 뒤 이 줄을
# 지나도 그대로 600 이라 봇이 자기 코드를 못 읽는다. 오류는 기동할 때야 난다.
find "$APP_DIR" -path "$APP_DIR/console-web/node_modules" -prune -o -print0 |
  xargs -0 -r chmod g+rX,g-w,o-rwx
chmod -R u+w "$APP_DIR/.venv"

# 봇 계정이 실제로 읽을 수 있는지 확인한다. 여기서 막히면 서비스가 기동에 실패하는데,
# 그때는 원인이 권한인지 코드인지 로그만 보고는 알기 어렵다.
if ! sudo -u tybot test -r "$APP_DIR/src/tybot/slack/pilot.py"; then
  echo "  ! 봇 계정이 코드를 읽지 못합니다: $APP_DIR/src/tybot/slack/pilot.py"
  echo "    권한을 확인하세요:  ls -l $APP_DIR/src/tybot/slack/pilot.py"
  exit 1
fi

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
# 타이머는 파일만 배치한다. enable은 운영자가 직접 하거나 관리자 콘솔의 배치 관리에서 결정한다.
#
# 다만 **꺼져 있다는 사실을 아무도 모르는 것**이 실제 문제였다. 타이머가 안 켜져 있으면
# 일정 동기화·DM 알림·백필이 통째로 돌지 않는데, 오류가 나지 않으니 몇 주가 지나도
# 모른다. 그래서 설치 끝에 어떤 것이 꺼져 있는지 이름을 대고 알린다.
TIMERS=(tybot-update tybot-collect tybot-tidy tybot-schedule-sync tybot-schedule-dm)
for u in "${TIMERS[@]}"; do
  install -m 0644 "$APP_DIR/deploy/$u.service" "/etc/systemd/system/$u.service"
  install -m 0644 "$APP_DIR/deploy/$u.timer"   "/etc/systemd/system/$u.timer"
done
if [[ "${WITH_CONSOLE:-0}" == "1" ]]; then
  visudo -cf "$APP_DIR/deploy/tybot-console-logs.sudoers" >/dev/null
  visudo -cf "$APP_DIR/deploy/tybot-console-timers.sudoers" >/dev/null
  install -m 0644 "$APP_DIR/deploy/tybot-console.service" /etc/systemd/system/tybot-console.service
  install -d -m 0755 /usr/local/libexec
  install -m 0755 "$APP_DIR/deploy/tybot-console-logs" /usr/local/libexec/tybot-console-logs
  install -m 0755 "$APP_DIR/deploy/tybot-console-timers" /usr/local/libexec/tybot-console-timers
  install -m 0440 "$APP_DIR/deploy/tybot-console-logs.sudoers" /etc/sudoers.d/tybot-console-logs
  install -m 0440 "$APP_DIR/deploy/tybot-console-timers.sudoers" /etc/sudoers.d/tybot-console-timers
fi
install -m 0644 "$APP_DIR/deploy/tybot-deploy.service" /etc/systemd/system/tybot-deploy.service
install -m 0644 "$APP_DIR/deploy/tybot-deploy.path"    /etc/systemd/system/tybot-deploy.path
systemctl daemon-reload

# 콘솔의 배포 버튼은 root 권한을 받지 않고 요청 파일만 만든다. 이 path 유닛은 파일
# 존재만 감시하며, 실제 배포는 고정된 deploy-runner.sh -> update.sh 경로로 실행한다.
if [[ "${WITH_CONSOLE:-0}" == "1" ]]; then
  systemctl enable --now tybot-deploy.path
fi

if [[ "${TYBOT_INSTALL_HINTS:-1}" != "1" ]]; then
  echo "설치 파일 갱신 완료"
  exit 0
fi

# 꺼진 타이머를 이름과 함께 알린다. 목록만 보여주면 사람이 대조해야 하고, 대조는 안 한다.
OFF=()
for u in "${TIMERS[@]}"; do
  systemctl is-enabled --quiet "$u.timer" 2>/dev/null || OFF+=("$u.timer")
done
if ((${#OFF[@]})); then
cat <<EOF

! 꺼져 있는 타이머 ${#OFF[@]}개 — 이 작업들은 지금 전혀 돌지 않습니다:
$(printf '    %s
' "${OFF[@]}")
  켜기:  sudo systemctl enable --now ${OFF[*]}
EOF
fi

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
  5) PostgreSQL 스키마를 적용하고 DB 계정을 만드세요:
       $APP_DIR/.venv/bin/python -m tybot.console.auth init-schema
       $APP_DIR/.venv/bin/python -m tybot.console.auth set-password <회사이메일> --role admin
  6) sudo systemctl enable --now tybot-console
  7) 8787 포트는 사내망/VPN 출발지에만 허용하세요.
EOF
if [[ "${CONSOLE_BUILD_FAILED:-0}" == "1" ]]; then
  echo "  ! 화면 빌드가 실패했습니다. 콘솔 API 는 뜨지만 브라우저에는 404 가 나옵니다."
  echo "    Node 20.19+ 설치 후 다시 실행하거나, dist 를 직접 올리세요."
fi
fi
