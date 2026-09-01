#!/usr/bin/env bash
# TYBot 자동 업데이트 — git fetch → 변경 있으면 테스트 → 통과 시에만 배포·재시작.
#
#   sudo bash /opt/tybot/deploy/update.sh          # 수동
#   systemd timer 로 주기 실행 (tybot-update.timer)
#
# 설계 의도:
#   - **테스트를 통과하지 못한 커밋은 배포하지 않는다.** 협업자가 push 한 코드가
#     바로 운영에 들어가므로, 자동 배포의 유일한 안전장치가 이것이다.
#   - 배포는 소스 클론에서 테스트를 먼저 돌리고, 통과한 뒤에만 install.sh 를 부른다.
#     실패 시 운영 프로세스는 손대지 않으므로 롤백이 필요 없다.
set -euo pipefail

SRC=${TYBOT_SRC:-/tmp/tybot-src}
APP=/opt/tybot
BRANCH=${TYBOT_BRANCH:-master}

# 콘솔이 이미 설치돼 있으면 자동 배포에서도 함께 갱신한다.
# 타이머는 환경변수 없이 돌기 때문에, 여기서 스스로 판단하지 않으면
# 손으로 돌릴 때만 화면이 갱신되고 자동 배포에서는 옛 화면이 남는다.
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

# --- 게이트: 테스트 통과 전에는 배포하지 않는다 ---
log "테스트 실행"
"$APP/.venv/bin/python" -m pytest --version >/dev/null 2>&1 \
  || "$APP/.venv/bin/pip" install -q pytest
# PYTHONPATH 로 새 소스를 강제한다 — venv 의 editable 설치는 아직 배포 전 코드(/opt)를 가리킨다.
if ! PYTHONPATH="$SRC/src" "$APP/.venv/bin/python" -m pytest -q "$SRC/tests" \
      -p no:cacheprovider --rootdir "$SRC" 2>&1 | tail -20; then
  log "테스트 실패 — 배포 중단. 운영 프로세스는 그대로 유지됩니다."
  log "커밋 $(git rev-parse --short HEAD) 를 확인하세요."
  exit 1
fi

log "배포"
bash "$SRC/deploy/install.sh"
systemctl restart tybot

# 콘솔은 봇과 별개 프로세스다. 재시작하지 않으면 새 API·화면이 반영되지 않는다.
# 콘솔이 못 떠도 봇 배포는 성공으로 본다 — 무게가 다르다.
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
