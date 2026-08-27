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

echo "== 5/6 권한 (코드는 봇이 수정 불가) =="
chown -R root:tybot "$APP_DIR"
chmod -R g-w,o-rwx "$APP_DIR"
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
install -m 0644 "$APP_DIR/deploy/tybot.service" /etc/systemd/system/tybot.service
# 타이머(자동배포·정기백필·점검)는 파일만 배치한다. enable 은 운영자가 결정한다.
for u in tybot-update tybot-collect tybot-tidy; do
  install -m 0644 "$APP_DIR/deploy/$u.service" "/etc/systemd/system/$u.service"
  install -m 0644 "$APP_DIR/deploy/$u.timer"   "/etc/systemd/system/$u.timer"
done
systemctl daemon-reload

cat <<EOF

설치 완료. 다음 순서로 진행하세요:
  1) sudo vi $CONF_DIR/tybot.env          # SLACK_BOT_TOKEN / SLACK_APP_TOKEN / ANTHROPIC_API_KEY
  2) sudo -u tybot $APP_DIR/.venv/bin/python $APP_DIR/scripts/check_env.py
  3) 로컬 PC 봇을 먼저 끄고:  sudo systemctl enable --now tybot
  4) journalctl -u tybot -f
EOF
