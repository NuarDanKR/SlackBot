#!/usr/bin/env bash
# setup.sh 가 **봇이 실제로 부르는 것**을 깔고 있나.
#
#     bash deploy/test-setup-packages.sh
#
# 왜 필요한가: 2026-09-03 까지 `deploy/` 어디에도 `python` 이 한 번도 안 나왔다. 그런데
# `src/ingest/util.js` 의 `python()` 은 `python3`·`python` 을 둘 다 못 찾으면 던지고,
# 그 함수를 자동 반영·백필·파생값 재계산·포맷 관문·위생 점검이 부른다.
#
# **봇은 그래도 정상 기동한다** — `src/index.js` 는 파이썬을 안 부른다. `systemctl status`
# 도 active 다. 처음 걸리는 자리는 매일 07:00 자동 반영이고, 증상은 에러가 아니라
# 「아카이브가 안 늘어난다」다. 데비안 이미지에 파이썬이 들어 있어 지금까지 굴러왔을 수는
# 있지만, **이미지가 주는 것과 설치 스크립트가 보장하는 것은 다르다.**
#
# ── 못 잡는 구간 ──
#
# · apt 가 실제로 설치에 성공하는지는 안 본다 (여기는 데비안이 아니다). setup.sh 가
#   **무엇을 깔라고 적었나**와, 없을 때 **멈추나**만 본다.
# · ③ 은 파이썬 파일의 import 줄을 글자로 본다. 실행해서 확인하지 않는다.
#   그리고 **직접 import 와 그 한 단계까지만** 본다 — 그 아래로 더 파고들지 않는다.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
SETUP="$HERE/setup.sh"
TMP="${TMPDIR:-/tmp}/setup-packages.$$"
pass=0; fail=0
ok() { pass=$((pass+1)); echo "  OK  $1"; }
ng() { fail=$((fail+1)); echo "  NG  $1"; }

rm -rf "$TMP"; mkdir -p "$TMP/bin"

# ── ① 설치 목록에 python3 가 있나 ──────────────────────────────────────────
if grep -qE '^apt-get install .*[[:space:]]python3([[:space:]]|$)' "$SETUP"; then
  ok "기본 패키지 설치 줄에 python3 가 있다"
else
  ng "기본 패키지 설치 줄에 python3 가 없다 — 07:00 자동 반영이 매일 조용히 멈춘다"
  grep -nE '^apt-get install' "$SETUP" | sed 's/^/        │ /'
fi

# ── ② 그래도 없으면 **멈추나** ─────────────────────────────────────────────
#
# `ok "$(python3 --version)"` 하나로 끝내면 안 된다 — 명령 치환이 실패해도 `ok` 자신은 0 이라
# `set -e` 가 안 걸리고 빈 줄이 초록으로 찍힌다. 그래서 명시적인 관문이 있어야 한다.
sed -n '/^if ! command -v python3/,/^fi$/p' "$SETUP" > "$TMP/guard.sh"
if [ ! -s "$TMP/guard.sh" ]; then
  ng "python3 관문(if ! command -v python3 …)을 setup.sh 에서 못 찾았습니다"
else
  # PATH 를 비워 python3 가 없는 기계를 흉내 낸다. `command -v` 와 `echo` 는 셸 내장이라
  # PATH 가 비어도 돈다.
  out=$( { echo 'set -euo pipefail'; echo 'PATH=""'; cat "$TMP/guard.sh"; echo 'echo "여기까지 왔다"'; } | bash 2>&1 ); rc=$?
  if [ "$rc" -eq 0 ]; then
    ng "python3 가 없는데 통과했다 (설치가 끝까지 가고 07:00 에야 걸린다)"
  else
    case "$out" in
      *"여기까지 왔다"*) ng "관문이 멈추지 않고 계속 진행했다" ;;
      *python3*)        ok "python3 가 없으면 멈추고 이유를 말한다" ;;
      *)                ng "멈추긴 했는데 무엇이 없는지 안 말한다" ;;
    esac
  fi
  # 대조군 — 있으면 통과해야 한다. 막기만 하는 관문은 설치를 못 하게 만든다.
  printf '#!/bin/sh\necho "Python 3.11.2"\n' > "$TMP/bin/python3"
  chmod +x "$TMP/bin/python3"
  if { echo 'set -euo pipefail'; echo "PATH=\"$TMP/bin\""; cat "$TMP/guard.sh"; } | bash >/dev/null 2>&1; then
    ok "python3 가 있으면 통과한다"
  else
    ng "python3 가 있는데 막혔다"
  fi
fi

# ── ③ SKILL_SCRIPTS 의 파이썬이 정말 표준 라이브러리만 쓰나 ──────────────────
#
# 목록에는 VM 이 부르는 넷(insert_messages·sync_index·verify_archive·verify_format)에
# 더해 사람이 부르는 스킬 전용 둘(fetch_slack_files·insert_entry)도 들어 있다
# (2026-09-22 전수 확인 — setup.sh 주석 참조). 넓게 재는 것이 안전해 목록을 안 좁힌다.
#
# **이 검사가 「openpyxl 을 안 깐다」는 판단을 받친다.** VM 은 엑셀 변환을 안 한다 —
# 그건 사람이 노트북에서 `doc-archive` 로 돌린다. 그래서 setup.sh 는 pip 를 안 쓰고
# 데비안 12 의 PEP 668 벽도 안 만난다. 다만 아래 파일 중 하나에 `import openpyxl` 이
# 들어오는 날 **그 판단이 조용히 깨진다** — VM 에서만, 07:00 에만 터진다.
#
# 목록의 원본은 `src/ingest/util.js` 의 `SKILL_SCRIPTS` 다 (+ 그것들이 함께 끌고 오는
# `_shared/paths.py`).
VM_PY="
.claude/skills/slack-sync/scripts/insert_messages.py
.claude/skills/slack-sync/scripts/sync_index.py
.claude/skills/slack-sync/scripts/verify_archive.py
.claude/skills/doc-archive/scripts/fetch_slack_files.py
.claude/skills/doc-archive/scripts/insert_entry.py
.claude/skills/doc-archive/scripts/verify_format.py
.claude/skills/_shared/paths.py
"
missing=""; tainted=""; n=0
for f in $VM_PY; do
  [ -f "$ROOT/$f" ] || { missing="$missing $f"; continue; }
  n=$((n+1))
  # 주석 속의 언급은 넘긴다 — 실제 import 줄만 본다.
  grep -qE '^[[:space:]]*(import openpyxl|from openpyxl)' "$ROOT/$f" && tainted="$tainted $f"
done
if [ -n "$missing" ]; then
  ng "SKILL_SCRIPTS 의 파이썬 파일을 못 찾았습니다:$missing (util.js 의 SKILL_SCRIPTS 와 갈렸습니다)"
elif [ "$n" -lt 7 ]; then
  ng "잰 파일이 ${n}개뿐입니다 (7개여야 합니다) — 목록이 안 읽힌 것으로 봅니다"
elif [ -n "$tainted" ]; then
  ng "SKILL_SCRIPTS 의 파이썬이 openpyxl 을 씁니다:$tainted"
  echo "        │ setup.sh 는 openpyxl 을 안 깝니다. 깔든지, 그 import 를 빼든지 정해야 합니다."
  echo "        │ (데비안 12 는 시스템 파이썬에 pip install 을 막습니다 — apt 의 python3-openpyxl 이나 venv 가 필요합니다)"
else
  ok "SKILL_SCRIPTS 의 파이썬 ${n}개가 표준 라이브러리만 씁니다 (openpyxl 을 안 깔아도 되는 근거)"
fi

rm -rf "$TMP"
echo ""
echo "통과 $pass · 실패 $fail"
[ "$fail" = 0 ] || exit 1
