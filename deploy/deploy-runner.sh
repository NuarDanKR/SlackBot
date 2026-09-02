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
OUTPUT="$STATE/deploy-last.log"

log() { echo "[$(date '+%F %T')] $*"; }

# 상태 기록은 python 쪽 한 곳(deploy_request.write_status)에서만 한다 —
# 포맷이 두 군데로 갈라지면 콘솔 화면이 조용히 깨진다.
status() {
  STATE_DIR="$STATE" "$PY" - "$@" <<'PYEOF'
import os
import sys
sys.path.insert(0, "/opt/tybot/src")
from pathlib import Path
from tybot.deploy_request import write_status
state, actor, before, after, message, before_title, after_title = (
    sys.argv[1:8] + [""] * 7
)[:7]
detail_path = Path(os.environ["DEPLOY_DETAIL_FILE"]) if os.environ.get("DEPLOY_DETAIL_FILE") else None
detail = detail_path.read_text(encoding="utf-8", errors="replace") if detail_path else ""
write_status(
    state,
    actor=actor,
    before=before,
    after=after,
    before_title=before_title,
    after_title=after_title,
    message=message,
    detail=detail,
)
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
BEFORE_TITLE=$(git -C "$SRC" log -1 --format=%s 2>/dev/null || true)
status running "$ACTOR" "$BEFORE" "" "배포 진행 중" "$BEFORE_TITLE" ""

# 로그 파일만 좁게 만든다. **umask 를 그대로 두면 안 된다** —
# 아래 update.sh 가 `git reset --hard` 로 소스를 새로 쓰고 install.sh 가 /opt 에
# 배치하는데, umask 077 이 걸려 있으면 그 파일들이 전부 600/700 으로 만들어진다.
# 그러면 봇 계정(tybot)이 자기 코드를 못 읽고, 다음 배포의 rsync 도 막힌다.
: > "$OUTPUT"
chmod 600 "$OUTPUT"
set +e
TYBOT_SRC="$SRC" TYBOT_BRANCH="$BRANCH" bash "$APP/deploy/update.sh" 2>&1 | tee "$OUTPUT"
UPDATE_RC=${PIPESTATUS[0]}
set -e

AFTER=$(git -C "$SRC" rev-parse --short HEAD 2>/dev/null || echo unknown)
AFTER_TITLE=$(git -C "$SRC" log -1 --format=%s 2>/dev/null || true)
if ((UPDATE_RC == 0)); then
  if [[ "$BEFORE" == "$AFTER" ]]; then
    status skipped "$ACTOR" "$BEFORE" "$AFTER" "새 커밋이 없어 배포하지 않았습니다." \
      "$BEFORE_TITLE" "$AFTER_TITLE"
    log "변경 없음"
  else
    status ok "$ACTOR" "$BEFORE" "$AFTER" "배포 완료" "$BEFORE_TITLE" "$AFTER_TITLE"
    log "배포 완료 $BEFORE -> $AFTER"
  fi
else
  DEPLOY_DETAIL_FILE="$OUTPUT" status failed "$ACTOR" "$BEFORE" "$AFTER" \
    "배포에 실패했습니다. 아래 실패 사유를 확인하세요." "$BEFORE_TITLE" "$AFTER_TITLE"
  log "배포 실패"
  exit 1
fi
