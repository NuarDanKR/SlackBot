#!/usr/bin/env bash
# setup.sh 의 **배포 금지 시각 관문**(7단계)을 실제로 돌려 본다.
#
#     bash deploy/test-setup-window.sh
#
# 왜 필요한가 — 9단계의 `systemctl restart hermes` 는 **돌던 회차를 끊는다.** 자동 반영이
# 끊기면 `src/ingest/index.js` 의 `rollback()` 이 안 돈다(프로세스가 통째로 죽으면 그 코드가
# 돌 기회가 없다). 그러면 자료 저장소 작업 트리가 반쯤 고쳐진 채 남고 다음 회차가
# `git pull` 에서 멈춘다. 관문이 이것을 막는데, **관문이 틀리면 틀린 줄을 아무도 모른다** —
# 안전으로 잘못 읽으면 그날 회차 하나가 죽고 나서야 드러난다.
#
# setup.sh 는 VM 에 통째로 내려받아 root 로 도는 스크립트라 여기서 전부 돌릴 수 없다.
# **관문 토막만 떼어 내어** 돌린다 — `test-setup-tail.sh` 와 같은 방식이다.
# `systemctl` 은 대역으로 세우고, 「지금」은 `HERMES_DEPLOY_NOW`(epoch 초)로 갈아끼운다.
#
# ── 여기서 특별히 보는 것 두 가지 ──
#
#   ① **시각을 config.json 에서 읽나** — 07:00·17:00·17:30 을 setup.sh 에 또 적으면
#      설정을 고치는 날 갈리고, 갈려도 에러가 안 난다. 그래서 픽스처의 cron 을 **실물과
#      다른 값**(03:00·21:45)으로 두고, 그 값대로 판정이 움직이는지 본다. 하드코딩이면
#      여기서 바로 빨개진다.
#   ② **기계 시간대가 바뀌어도 판정이 안 뒤집히나** — UTC 로 도는 기계에서는 07:00 KST 가
#      22:00 으로 보인다. 같은 절대시각을 주고 `TZ` 만 셋으로 바꿔 돌려, 답이 같은지 본다.
#
# ── 못 잡는 구간 ──
#
# · `systemctl` 을 대역으로 세운다. 진짜 systemd 가 `is-active` 를 어떻게 답하는지는 안
#   본다 — 여기서 보는 것은 **setup.sh 가 그 답을 어떻게 쓰나**뿐이다.
# · 회차가 실제로 몇 분 도는지는 여기서 못 잰다. 창의 넓이(`DEPLOY_LEAD_MIN`·
#   `DEPLOY_TRAIL_MIN`)가 **맞는 값인지**는 안 보고, 그 값대로 **동작하는지**만 본다.
#   그래서 이 시험은 실물의 두 값을 읽어 와서 쓴다 — 사람이 값을 조정해도 안 깨진다.
# · 관문을 지난 뒤 8·9단계가 실제로 얼마나 걸리는지는 안 본다(앞 10분의 근거).
# · ⑨ 는 `grep` 으로 setup.sh 에 그 줄이 **있는지**만 본다 — 실행이 거기까지 **닿는지**는
#   안 본다. `test-setup-guard.sh` 머리말에 적힌 것과 같은 틈이다.
# · 이 틈을 메우는 것은 빈 VM 에서 setup.sh 를 처음부터 끝까지 밟아 보는 것뿐이고,
#   그건 사람만 할 수 있다.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
# 어긋내기(mutation)로 이 시험이 정말 빨개지는지 볼 때 **고친 사본**을 가리키게 하는 손잡이.
# 실물을 건드리지 않고 재려면 이것이 있어야 한다. 평소엔 쓰지 않는다.
SETUP="${HERMES_SETUP:-$HERE/setup.sh}"
TMP="${TMPDIR:-/tmp}/setup-window.$$"
pass=0; fail=0
ok() { pass=$((pass+1)); echo "  OK  $1"; }
ng() { fail=$((fail+1)); echo "  NG  $1"; }

rm -rf "$TMP"; mkdir -p "$TMP"

# ── 파이썬 찾기 ────────────────────────────────────────────────────────────
# 관문은 `python3` 를 부른다 (VM 에서는 3단계가 그것을 보장한다). 이 시험은 노트북에서도
# 도는데 윈도우에는 `python3` 가 없을 수 있어, 있으면 그대로 쓰고 없으면 `python` 을
# `python3` 라는 이름으로 대준다. **없으면 건너뛰지 않고 실패로 낸다** — 파이썬이 없어
# 관문을 못 돌린 것은 「이상 없음」이 아니다.
PY3=""
if command -v python3 >/dev/null 2>&1 && python3 -c "import zoneinfo" >/dev/null 2>&1; then
  PY3="python3"
elif command -v python >/dev/null 2>&1 && python -c "import zoneinfo" >/dev/null 2>&1; then
  PY3="python"
else
  ng "zoneinfo 가 있는 파이썬을 못 찾아 관문을 한 번도 못 돌렸습니다 (python3 / python)"
  echo ""; echo "통과 $pass · 실패 $fail"; rm -rf "$TMP"; exit 1
fi

# ── 시간대 자료를 이 시험이 직접 들고 온다 ─────────────────────────────────
# **윈도우에는 시간대 자료(tzdata)가 아예 없다** — 2026-09-03 실측: `zoneinfo.TZPATH` 가
# 빈 튜플이고 `ZoneInfo("UTC")` 조차 `ZoneInfoNotFoundError` 다. 데비안(VM)에는
# `/usr/share/zoneinfo` 가 있어 관문은 거기서 정상으로 돌지만, 그 차이 때문에 이 시험이
# 노트북에서 **영영 빨간 줄**이 된다 — 고칠 수 없는 빨강은 곧 화면 전체를 안 읽게 만든다.
#
# 그래서 **+09:00 짜리 TZif 파일 하나를 직접 만들어** `PYTHONTZPATH` 로 물린다. 이러면
# 어느 기계에서든 같은 것을 재고, 덤으로 시간대 이름까지 config.json 에서 온다는 것이
# 드러난다 (`Asia/Seoul` 을 썼다면 어디서 왔는지 구분이 안 됐을 것이다).
ZONE="Test/Plus9"
mkdir -p "$TMP/tz/Test"
# 경로는 **인자로** 넘긴다 — MSYS 가 인자만 윈도우 경로로 바꿔 주고, 파이썬 코드 안에
# 적은 문자열은 안 바꾼다.
"$PY3" -c "import struct,sys
hdr = b'TZif' + b'\x00' + b'\x00'*15 + struct.pack('>6l', 0,0,0,0,1,4)
body = struct.pack('>lBB', 32400, 0, 0) + b'KST\x00'
open(sys.argv[1],'wb').write(hdr+body)" "$TMP/tz/Test/Plus9"
# 환경변수는 MSYS 가 안 바꿔 주므로 여기서 기계 경로로 만든다.
if command -v cygpath >/dev/null 2>&1; then TZDIR=$(cygpath -m "$TMP/tz"); else TZDIR="$TMP/tz"; fi
export PYTHONTZPATH="$TZDIR"
if ! "$PY3" -c "import sys,zoneinfo; zoneinfo.ZoneInfo(sys.argv[1])" "$ZONE" >/dev/null 2>&1; then
  ng "시험이 만든 시간대($ZONE)를 파이썬이 못 읽습니다 — 이 시험은 아무것도 재지 못했습니다"
  echo ""; echo "통과 $pass · 실패 $fail"; rm -rf "$TMP"; exit 1
fi

# ── 실물에서 창 넓이를 읽어 온다 ───────────────────────────────────────────
LEAD=$(LC_ALL=C sed -n 's/^DEPLOY_LEAD_MIN=\([0-9][0-9]*\).*/\1/p'  "$SETUP" | head -1)
TRAIL=$(LC_ALL=C sed -n 's/^DEPLOY_TRAIL_MIN=\([0-9][0-9]*\).*/\1/p' "$SETUP" | head -1)
if [ -z "$LEAD" ] || [ -z "$TRAIL" ]; then
  ng "setup.sh 에서 DEPLOY_LEAD_MIN·DEPLOY_TRAIL_MIN 을 못 읽었습니다 (이름을 고쳤으면 이 시험도 함께 고치세요)"
  echo ""; echo "통과 $pass · 실패 $fail"; rm -rf "$TMP"; exit 1
fi

# ── 관문 토막 뽑기 ─────────────────────────────────────────────────────────
# 뽑히지 않으면 「없다」가 아니라 실패다.
sed -n '/^deploy_window_probe() {/,/^say "8\/9/p' "$SETUP" | sed '$d' > "$TMP/gate.sh"
if ! LC_ALL=C grep -q '^deploy_window_probe() {' "$TMP/gate.sh" \
   || ! LC_ALL=C grep -q 'deploy_window_probe)' "$TMP/gate.sh"; then
  ng "setup.sh 에서 관문 토막을 못 뽑았습니다 (기준: 'deploy_window_probe() {' ~ 'say \"8/9')"
  echo ""; echo "통과 $pass · 실패 $fail"; rm -rf "$TMP"; exit 1
fi

# ── 픽스처 ─────────────────────────────────────────────────────────────────
# **실물 config.json 과 일부러 다른 시각**을 쓴다 (위 머리말 ①).
mkdir -p "$TMP/data" "$TMP/data-moved" "$TMP/data-notz" "$TMP/data-nocron" "$TMP/data-none"
cat > "$TMP/data/config.json" <<JSON
{ "timezone": "$ZONE",
  "digest": {
    "alpha": { "enabled": true,  "cron": "0 3 * * *" },
    "beta":  { "enabled": true,  "cron": "45 21 * * *" },
    "off":   { "enabled": false, "cron": "0 12 * * *" }
  } }
JSON
# 같은 파일에서 시각만 옮긴 판 — 판정이 파일을 따라 움직이는지 보는 짝.
sed 's/0 3 \* \* \*/0 5 * * */' "$TMP/data/config.json" > "$TMP/data-moved/config.json"
echo '{ "digest": { "alpha": { "enabled": true, "cron": "0 3 * * *" } } }' > "$TMP/data-notz/config.json"
echo "{ \"timezone\": \"$ZONE\", \"digest\": { \"alpha\": { \"enabled\": false, \"cron\": \"0 3 * * *\" } } }" > "$TMP/data-nocron/config.json"
# data-none 은 config.json 이 아예 없다 (첫 설치).

# 픽스처 시간대의 벽시계 → epoch 초. 관문에는 **절대시각**을 준다 — 벽시계 문자열로 주면
# 「기계 시간대를 바꿔도 같은 답인가」(⑧)가 애초에 아무것도 안 재게 된다.
epoch() {   # $1 = "YYYY-MM-DD HH:MM" (픽스처 시간대 기준)
  "$PY3" -c "import sys
from datetime import datetime
from zoneinfo import ZoneInfo
print(int(datetime.strptime(sys.argv[1],'%Y-%m-%d %H:%M').replace(tzinfo=ZoneInfo(sys.argv[2])).timestamp()))" "$1" "$ZONE"
}
# 03:00 을 기준으로 창 넓이만큼 떨어진 시각들을 만든다.
shift_hm() {   # $1 = "YYYY-MM-DD HH:MM"  $2 = 더할 분
  "$PY3" -c "import sys
from datetime import datetime, timedelta
t = datetime.strptime(sys.argv[1],'%Y-%m-%d %H:%M') + timedelta(minutes=int(sys.argv[2]))
print(t.strftime('%Y-%m-%d %H:%M'))" "$1" "$2"
}

FIRE='2026-09-07 03:00'                       # 픽스처 alpha 가 도는 시각 (일요일)
EDGE_IN=$(shift_hm "$FIRE" "-$LEAD")          # 딱 창 안 (앞 경계)
SAFE_BEFORE=$(shift_hm "$FIRE" "-$((LEAD+1))")  # 창 밖 — **대조군**
SAFE_AFTER=$(shift_hm "$FIRE" "$((TRAIL+1))")   # 창 밖 (뒤) = 「다시 오세요」로 나와야 할 시각
OFF_TIME='2026-09-07 12:00'                   # 꺼 둔 예약의 시각

# ── 러너 만들기 ────────────────────────────────────────────────────────────
# 실물과 똑같이 `set -euo pipefail` 을 켠다 — 관문이 그 아래서 돌기 때문이다.
make_runner() {   # $1=파일명  $2=FORCE  $3=is-active 종료코드  $4=DATA 폴더
  {
    echo 'set -euo pipefail'
    echo "FORCE=$2"
    echo "DATA='$4'"
    echo "DEPLOY_LEAD_MIN=$LEAD"
    echo "DEPLOY_TRAIL_MIN=$TRAIL"
    echo "GIVEN_ARGS=' git@github.com:t/code.git git@github.com:t/data.git'"
    echo 'say()  { printf "== %s\n" "$*"; }'
    echo 'ok()   { printf "   OK %s\n" "$*"; }'
    echo 'warn() { printf "   ! %s\n" "$*"; }'
    echo "systemctl() { case \"\$*\" in *is-active*) return $3 ;; *) return 0 ;; esac; }"
    [ "$PY3" = "python3" ] || echo "python3() { $PY3 \"\$@\"; }"
    cat "$TMP/gate.sh"
  } > "$TMP/$1"
}

# 관문을 한 번 돌린다. 결과는 전역 $out · $rc.
run_gate() {   # $1=파일명  $2=지금(KST 벽시계)  $3=TZ 환경변수
  local e; e=$(epoch "$2")
  set +e
  out=$(TZ="$3" HERMES_DEPLOY_NOW="$e" bash "$TMP/$1" 2>&1)
  rc=$?
  set -e
}

# ── ① 위험 시각이면 막나 · 종료코드가 3 인가 ───────────────────────────────
make_runner run-normal.sh 0 0 "$TMP/data"
run_gate run-normal.sh "$FIRE" "Asia/Seoul"
[ "$rc" -eq 3 ] && ok "위험 시각($FIRE)에 막고 종료코드 3" \
                || { ng "위험 시각인데 종료코드가 $rc 다 (3 이어야 한다)"; echo "$out" | sed 's/^/        │ /'; }
case "$out" in
  *'"0 3 * * *"'*) ok "겹치는 예약을 config.json 의 cron 그대로 보여준다" ;;
  *) ng "겹치는 예약이 무엇인지 화면에 안 나온다"; echo "$out" | sed 's/^/        │ /' ;;
esac

# 앞 경계도 막아야 한다 — 관문을 지난 뒤 8·9단계가 남아 있기 때문이다.
run_gate run-normal.sh "$EDGE_IN" "Asia/Seoul"
[ "$rc" -eq 3 ] && ok "예약 ${LEAD}분 전($EDGE_IN)도 막는다" \
                || ng "예약 ${LEAD}분 전인데 통과했다 (종료코드 $rc)"

# ── ② 안전 시각이면 통과하나 (대조군) ──────────────────────────────────────
run_gate run-normal.sh "$SAFE_BEFORE" "Asia/Seoul"
if [ "$rc" -eq 0 ]; then
  ok "안전 시각($SAFE_BEFORE)에는 통과 — 대조군"
else
  ng "안전 시각인데 막혔다 (종료코드 $rc) — 관문이 항상 막는 것이라면 ①은 아무것도 안 잰 것이다"
  echo "$out" | sed 's/^/        │ /'
fi
run_gate run-normal.sh "$SAFE_AFTER" "Asia/Seoul"
[ "$rc" -eq 0 ] && ok "회차가 끝난 뒤($SAFE_AFTER)에도 통과" \
                || ng "회차 뒤 ${TRAIL}분+1 인데 막혔다 (종료코드 $rc)"

# 꺼 둔 예약은 안 막아야 한다 (scheduler.js 도 enabled 가 true 인 것만 돌린다).
run_gate run-normal.sh "$OFF_TIME" "Asia/Seoul"
[ "$rc" -eq 0 ] && ok "꺼 둔 예약(enabled:false)의 시각은 안 막는다" \
                || ng "꺼 둔 예약 시각인데 막혔다 (종료코드 $rc)"

# ── ③ --force 면 위험 시각에도 통과하나 ────────────────────────────────────
make_runner run-force.sh 1 0 "$TMP/data"
run_gate run-force.sh "$FIRE" "Asia/Seoul"
[ "$rc" -eq 0 ] && ok "--force 는 위험 시각에도 통과" \
                || { ng "--force 인데 막혔다 (종료코드 $rc) — 빠져나갈 길이 없다"; echo "$out" | sed 's/^/        │ /'; }
case "$out" in
  *"끊길 수 있습니다"*) ok "--force 로 지나갈 때 대가를 화면에 말한다" ;;
  *) ng "--force 가 조용히 지나간다 — 무엇을 감수했는지 아무도 모른다" ;;
esac

# ── ④ 막았을 때 「언제 다시 오면 되는지」가 나오나 ─────────────────────────
run_gate run-normal.sh "$FIRE" "Asia/Seoul"
want_back=$("$PY3" -c "import sys; print(sys.argv[1][5:])" "$SAFE_AFTER")   # MM-DD HH:MM
case "$out" in
  *"다시 오세요"*) ok "막을 때 다시 올 시각을 말한다" ;;
  *) ng "「위험합니다」로만 끝난다 — 사람이 바로 --force 를 친다"; echo "$out" | sed 's/^/        │ /' ;;
esac
case "$out" in
  *"$want_back"*) ok "다시 올 시각이 창 밖의 첫 자리($want_back)로 정확하다" ;;
  *) ng "다시 올 시각이 $want_back 가 아니다"; echo "$out" | sed 's/^/        │ /' ;;
esac
case "$out" in
  *"--force"*) ok "빠져나갈 길(--force)도 함께 보여준다" ;;
  *) ng "막기만 하고 빠져나갈 길을 안 알려준다" ;;
esac
case "$out" in
  *"안 받았고"*|*"안 건드렸습니다"*) ok "지금 상태(아무것도 안 건드림)를 말한다" ;;
  *) ng "무엇이 이미 벌어졌는지 안 말한다 — 사람이 반쯤 배포된 줄 안다" ;;
esac

# ── ⑤ 시각을 config.json 에서 읽나 (여기 또 안 적었나) ─────────────────────
# 픽스처의 cron 만 03:00 → 05:00 으로 옮기면 판정이 따라 움직여야 한다.
make_runner run-moved.sh 0 0 "$TMP/data-moved"
run_gate run-moved.sh "$FIRE" "Asia/Seoul"
[ "$rc" -eq 0 ] && ok "config.json 의 cron 을 옮기니 03:00 이 안전해졌다 (시각을 파일에서 읽는다)" \
                || { ng "config.json 을 고쳤는데 03:00 이 여전히 막힌다 — 시각이 다른 데 박혀 있다"; echo "$out" | sed 's/^/        │ /'; }
run_gate run-moved.sh '2026-09-07 05:00' "Asia/Seoul"
[ "$rc" -eq 3 ] && ok "옮긴 시각(05:00)이 이제 막힌다" \
                || ng "옮긴 시각이 안 막힌다 (종료코드 $rc)"

# 실행되는 줄에 예약 시각이 값으로 박혀 있지 않나 (주석은 봐준다 — 설명이라 갈려도 안 위험하다).
if LC_ALL=C grep -vE '^[[:space:]]*#' "$SETUP" | LC_ALL=C grep -qE '(07:00|17:00|17:30|"0 7 \*|"30 17 \*)'; then
  ng "setup.sh 의 실행되는 줄에 예약 시각이 박혀 있다 — config.json 과 갈리는 날이 온다"
  LC_ALL=C grep -nvE '^[[:space:]]*#' "$SETUP" | LC_ALL=C grep -E '(07:00|17:00|17:30|"0 7 \*|"30 17 \*)' | sed 's/^/        │ /'
else
  ok "setup.sh 의 실행되는 줄에 예약 시각이 안 박혀 있다"
fi

# ── ⑥ 봇이 안 돌면 통과 · config.json 이 없으면 통과 ───────────────────────
make_runner run-dead.sh 0 3 "$TMP/data"          # is-active 가 3 = inactive
run_gate run-dead.sh "$FIRE" "Asia/Seoul"
[ "$rc" -eq 0 ] && ok "봇이 안 돌면 위험 시각이어도 통과 (끊길 회차가 없다)" \
                || ng "봇이 안 도는데 막혔다 (종료코드 $rc) — 첫 설치·복구가 시각에 걸린다"

make_runner run-first.sh 0 0 "$TMP/data-none"
run_gate run-first.sh "$FIRE" "Asia/Seoul"
[ "$rc" -eq 0 ] && ok "자료 저장소가 아직 없으면(첫 설치) 통과" \
                || ng "첫 설치인데 막혔다 (종료코드 $rc)"

# ── ⑦ 판정 불가면 막나 ─────────────────────────────────────────────────────
# 조용히 기계 시각으로 물러서면, 그것이야말로 이 관문이 막으려는 「에러 없이 틀리기」다.
make_runner run-notz.sh 0 0 "$TMP/data-notz"
run_gate run-notz.sh "$FIRE" "Asia/Seoul"
[ "$rc" -eq 3 ] && ok "config.json 에 timezone 이 없으면 막는다 (기계 시각으로 안 물러선다)" \
                || ng "timezone 이 없는데 통과했다 (종료코드 $rc)"

make_runner run-nocron.sh 0 0 "$TMP/data-nocron"
run_gate run-nocron.sh "$FIRE" "Asia/Seoul"
[ "$rc" -eq 3 ] && ok "켜진 예약을 하나도 못 찾으면 막는다 (0개는 조용한 통과가 아니다)" \
                || ng "예약 0개인데 통과했다 (종료코드 $rc)"

# ── ⑧ 기계 시간대가 바뀌어도 판정이 안 뒤집히나 ────────────────────────────
# 같은 **절대시각**을 주고 TZ 만 바꾼다. UTC 기계에서는 03:00 KST 가 전날 18:00 으로
# 보이므로, 관문이 기계 시각을 봤다면 여기서 판정이 뒤집힌다.
flip=""
for z in "Asia/Seoul" "UTC" "America/New_York"; do
  run_gate run-normal.sh "$FIRE" "$z"
  [ "$rc" -eq 3 ] || flip="$flip / 위험@$z→$rc"
  case "$out" in *"2026-09-07 03:00 ($ZONE)"*) : ;; *) flip="$flip / 찍힌시각@$z" ;; esac
  run_gate run-normal.sh "$SAFE_BEFORE" "$z"
  [ "$rc" -eq 0 ] || flip="$flip / 안전@$z→$rc"
done
if [ -z "$flip" ]; then
  ok "TZ 를 Asia/Seoul·UTC·America/New_York 로 바꿔도 판정과 찍히는 시각이 같다"
else
  ng "기계 시간대에 따라 판정이 갈린다:$flip"
  echo "$out" | sed 's/^/        │ /'
fi

# 위는 「지금」을 갈아끼운 길만 잰다. **손잡이 없이 진짜 지금을 읽는 길**도 함께 본다 —
# 거기서 기계 시각을 보면(`datetime.now()` 를 시간대 없이 쓰면) 판정이 통째로 뒤집힌다.
stamps=""
for z in "UTC" "America/New_York" "Asia/Seoul"; do
  want_a=$("$PY3" -c "import sys
from datetime import datetime
from zoneinfo import ZoneInfo
print(datetime.now(ZoneInfo(sys.argv[1])).strftime('%Y-%m-%d %H:%M'))" "$ZONE")
  set +e
  live=$(TZ="$z" bash "$TMP/run-normal.sh" 2>&1)
  set -e
  want_b=$("$PY3" -c "import sys
from datetime import datetime
from zoneinfo import ZoneInfo
print(datetime.now(ZoneInfo(sys.argv[1])).strftime('%Y-%m-%d %H:%M'))" "$ZONE")
  case "$live" in
    *"$want_a ($ZONE)"*|*"$want_b ($ZONE)"*) : ;;
    *) stamps="$stamps / $z(기대 $want_a)" ;;
  esac
done
if [ -z "$stamps" ]; then
  ok "손잡이 없이 진짜 지금을 읽을 때도 config.json 의 시간대로 읽는다 (기계 TZ 무관)"
else
  ng "진짜 지금을 기계 시간대로 읽는다:$stamps"
  echo "$live" | sed 's/^/        │ /'
fi

# ── ⑨ 글자로 보는 것 — 관문이 실제로 불리나 · 자리가 맞나 ──────────────────
if LC_ALL=C grep -qE '^[[:space:]]*probe_out=\$\(deploy_window_probe\)' "$SETUP"; then
  ok "관문을 정의만 하지 않고 실제로 부른다"
else
  ng "deploy_window_probe 를 부르는 줄이 없다 — 정의만 있고 아무도 안 부른다"
fi
gate_ln=$(LC_ALL=C grep -n '^say "7/9' "$SETUP" | head -1 | cut -d: -f1)
pull_ln=$(LC_ALL=C grep -n '^say "8/9' "$SETUP" | head -1 | cut -d: -f1)
rest_ln=$(LC_ALL=C grep -n '^systemctl restart hermes' "$SETUP" | head -1 | cut -d: -f1)
if [ -n "$gate_ln" ] && [ -n "$pull_ln" ] && [ -n "$rest_ln" ] \
   && [ "$gate_ln" -lt "$pull_ln" ] && [ "$pull_ln" -lt "$rest_ln" ]; then
  ok "관문이 코드 pull·npm ci($pull_ln 줄)와 restart($rest_ln 줄)보다 앞에 있다 ($gate_ln 줄)"
else
  ng "관문 자리가 어긋났다 (관문 $gate_ln · pull $pull_ln · restart $rest_ln) — 뒤에 두면 「받고 재시작 안 함」으로 끝난다"
fi

rm -rf "$TMP"
echo ""
echo "통과 $pass · 실패 $fail"
[ "$fail" = 0 ] || exit 1
