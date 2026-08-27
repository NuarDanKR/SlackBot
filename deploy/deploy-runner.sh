#!/usr/bin/env bash
# 콘솔의 배포 요청을 처리한다. tybot-deploy.path 가 요청 파일을 감지해 이 스크립트를 부른다.
#
# 설계 의도:
#   - 콘솔(User=tybot)에게 root 를 주지 않는다. 콘솔은 요청 파일만 쓴다.
#   - **요청 파일의 내용을 명령 인자로 쓰지 않는다.** 브랜치·경로는 아래에 고정돼 있어
#     웹에서 온 값이 실행에 영향을 주는 경로가 없다(주입 불가).
#   - 실제 배포 판단은 update.sh 가 한다 — 테스트를 통과하지 못한 커밋은 배포되지 않는다.
set -euo pipefail

APP=/opt/tybot
STATE=${STATE_DIR:-/var/lib/tybot}
SRC=${TYBOT_SRC:-/tmp/tybot-src}
BRANCH=${TYBOT_BRANCH:-master}          # 고정. 콘솔이 바꿀 수 없다.
PY="$APP/.venv/bin/python"
LOCK="$STATE/.locks/deploy.lock"

log() { echo "[$(date '+%F %T')] $*"; }

# 상태 기록은 python 쪽 한 곳(deploy_request.write_status)에서만 한다 —
# 포맷이 두 군데로 갈라지면 콘솔 화면이 조용히 깨진다.
status() {
  STATE_DIR="$STATE" "$PY" - "$@" <<'PYEOF'
import sys
sys.path.insert(0, "/opt/tybot/src")
from tybot.deploy_request import write_status
state, actor, before, after, message = (sys.argv[1:6] + [""] * 5)[:5]
write_status(state, actor=actor, before=before, after=after, message=message)
PYEOF
}

mkdir -p "$STATE/.locks"
exec 9>"$LOCK"
if ! flock -n 9; then
  log "다른 배포가 진행 중 — 이번 요청은 무시한다"
  exit 0
fi

# 요청을 먼저 소비한다. 여기서 지워야 실패해도 무한 재트리거가 되지 않는다.
ACTOR=$(STATE_DIR="$STATE" "$PY" - <<'PYEOF'
import sys
sys.path.insert(0, "/opt/tybot/src")
from tybot.deploy_request import consume_request
req = consume_request() or {}
print(req.get("actor", "unknown"))
PYEOF
)
[[ -n "$ACTOR" ]] || ACTOR=unknown
log "배포 요청 수신 actor=$ACTOR branch=$BRANCH"

BEFORE=$(git -C "$SRC" rev-parse --short HEAD 2>/dev/null || echo unknown)
status running "$ACTOR" "$BEFORE" "" "배포 진행 중"

if TYBOT_SRC="$SRC" TYBOT_BRANCH="$BRANCH" bash "$APP/deploy/update.sh"; then
  AFTER=$(git -C "$SRC" rev-parse --short HEAD 2>/dev/null || echo unknown)
  if [[ "$BEFORE" == "$AFTER" ]]; then
    status skipped "$ACTOR" "$BEFORE" "$AFTER" "새 커밋이 없어 배포하지 않았습니다."
    log "변경 없음"
  else
    status ok "$ACTOR" "$BEFORE" "$AFTER" "배포 완료"
    log "배포 완료 $BEFORE -> $AFTER"
  fi
else
  AFTER=$(git -C "$SRC" rev-parse --short HEAD 2>/dev/null || echo unknown)
  status failed "$ACTOR" "$BEFORE" "$AFTER" \
    "배포 실패. 테스트 불통 또는 기동 실패 — journalctl -u tybot-deploy 를 확인하세요. 운영 프로세스는 이전 상태입니다."
  log "배포 실패"
  exit 1
fi
