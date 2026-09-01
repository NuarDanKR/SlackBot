#!/usr/bin/env bash
# TYBot 자동 업데이트: 변경 확인 → 테스트 → 배포 → 서비스 재시작.
#
#   sudo bash /opt/tybot/deploy/update.sh          # 수동
#   systemd timer 로 주기 실행 (tybot-update.timer)
set -euo pipefail

SRC=${TYBOT_SRC:-/tmp/tybot-src}
APP=/opt/tybot
BRANCH=${TYBOT_BRANCH:-master}

# 콘솔이 이미 설치돼 있으면 함께 갱신한다.
if [[ -z ${WITH_CONSOLE:-} ]]; then
  if [[ -f /etc/systemd/system/tybot-console.service ]]; then
    WITH_CONSOLE=1
  else
    WITH_CONSOLE=0
  fi
fi
export WITH_CONSOLE

log() { echo "[$(date '+%F %T')] $*"; }

[[ $EUID -eq 0 ]] || { echo "root 로 실행하세요 (sudo)"; exit 1; }
[[ -d "$SRC/.git" ]] || { log "소스 클론이 없습니다: $SRC"; exit 1; }

cd "$SRC"
git fetch --quiet origin "$BRANCH"
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")

if [[ "$LOCAL" == "$REMOTE" ]]; then
  log "변경 없음 ($(git rev-parse --short HEAD))"
  exit 0
fi

log "새 커밋 발견: $(git rev-parse --short "$LOCAL") -> $(git rev-parse --short "$REMOTE")"
git log --oneline "$LOCAL..$REMOTE" | sed 's/^/  /'
git reset --hard --quiet "origin/$BRANCH"

log "테스트 실행"
"$APP/.venv/bin/python" -m pytest --version >/dev/null 2>&1 \
  || "$APP/.venv/bin/pip" install -q pytest
# editable 설치는 아직 /opt 를 가리키므로 새 소스를 명시한다.
if ! PYTHONPATH="$SRC/src" "$APP/.venv/bin/python" -m pytest -q "$SRC/tests" \
      -p no:cacheprovider --rootdir "$SRC" 2>&1 | tail -20; then
  log "테스트 실패 — 배포 중단. 운영 프로세스는 그대로 유지됩니다."
  log "커밋 $(git rev-parse --short HEAD) 를 확인하세요."
  exit 1
fi

log "배포"
TYBOT_INSTALL_HINTS=0 bash "$SRC/deploy/install.sh"
systemctl restart tybot

# 콘솔은 별도 프로세스이므로 따로 재시작한다.
if [[ "$WITH_CONSOLE" == "1" ]] && systemctl is-enabled --quiet tybot-console 2>/dev/null; then
  systemctl restart tybot-console || log '콘솔 재시작 실패 — journalctl -u tybot-console 확인'
fi

sleep 5

if systemctl is-active --quiet tybot; then
  log "완료: $(git rev-parse --short HEAD) 기동 정상"
else
  log "기동 실패 — journalctl -u tybot -n 50 확인"
  exit 1
fi
