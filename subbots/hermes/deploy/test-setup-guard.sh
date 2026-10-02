#!/bin/sh
# setup.sh 의 빈-자료-저장소 가드를 **실제로 돌려** 본다.
#
#     sh deploy/test-setup-guard.sh
#
# 왜 필요한가: `git clone <빈 저장소>` 는 종료코드 0 에 경고 한 줄만 낸다(2026-09-01 실측).
# 그래서 setup.sh 가 성공으로 읽고 설치를 끝까지 진행했고, 처음 걸리는 자리가 봇 기동이었다.
#
# setup.sh 는 VM 에 통째로 내려받아 root 로 도는 스크립트라 여기서 전부 돌릴 수 없다.
# **함수 하나만 떼어 내어** 돌린다 — 그래서 이 시험이 보는 것은 그 함수의 판정뿐이고,
# 그 함수를 setup.sh 가 실제로 부르는지는 아래 ③ 이 글자로 본다.
#
# ── 못 잡는 구간 ──
#
# 이 함수는 setup.sh 에서 **뽑아내어 맨 셸에 그대로 source** 한다 — 그래서 setup.sh 가
# 실제로 켜는 `set -euo pipefail` 이 여기선 안 걸려 있다. check_data_ready() 자체의
# 판정(①②)은 그 옵션과 무관해서 이 시험으로 충분히 보이지만, **그 앞뒤 맥락은 안 보인다.**
#
# ③ 은 `grep` 으로 `check_data_ready "$DATA"` 줄이 setup.sh 에 **있는지**만 본다 —
# 실행이 거기까지 **닿는지**는 안 본다. clone_or_pull() 안에서 set -e 가 그 줄보다
# 먼저 스크립트를 죽이면 가드는 글자로는 있지만 아무도 못 부르는데, 이 시험은 그대로
# 통과한다. 2026-09-01 fix round 1 에서 실제로 이랬다 — `pull_err=$(...)` 대입이
# set -e 아래서 실패하면 그 줄에서 바로 죽어 뒤의 코드가 전혀 안 돌았는데, ③ 은 grep
# 이 걸리니 계속 초록이었다. 이 주석은 그 일이 있었기 때문에 여기 있다.
#
# 이 틈을 메우는 것은 빈 VM 에서 setup.sh 를 처음부터 끝까지 실제로 한 번 밟아 보는
# 것뿐이고, 그건 사람만 할 수 있다.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
SETUP="$HERE/setup.sh"
TMP="${TMPDIR:-/tmp}/setup-guard.$$"
pass=0; fail=0
ok() { pass=$((pass+1)); echo "  OK  $1"; }
ng() { fail=$((fail+1)); echo "  NG  $1"; }

rm -rf "$TMP"; mkdir -p "$TMP/empty" "$TMP/ready/slack-export" "$TMP/half"
echo '{}' > "$TMP/ready/config.json"
echo '# 아카이브 색인' > "$TMP/ready/slack-export/index.md"
# `half` = 설정만 손으로 복사하고 `npm run init-archive` 를 안 돌린 저장소.
# **이것이 대조군이다** — 2026-09-01 에 가드가 config.json 만 보던 동안 이 모양이 그대로
# 통과했고, 그때는 이 갈래가 여기 없어서 시험도 함께 초록이었다.
echo '{}' > "$TMP/half/config.json"

# 함수만 뽑는다. 뽑히지 않으면 「없다」가 아니라 실패다.
sed -n '/^check_data_ready()/,/^}/p' "$SETUP" > "$TMP/fn.sh"
if [ ! -s "$TMP/fn.sh" ]; then
  ng "setup.sh 에서 check_data_ready() 를 뽑지 못했습니다"
  echo ""; echo "통과 $pass · 실패 $fail"; rm -rf "$TMP"; exit 1
fi
warn() { echo "   ! $*"; }   # setup.sh 의 헬퍼를 흉내 낸다 — 실물(setup.sh:38)도 stdout 이다
. "$TMP/fn.sh"

# **함수가 진짜로 들어왔나.**
#
# 아래 `if check_data_ready …` 는 함수가 없어도 「명령이 없다」로 0 이 아닌 값을 내고,
# 그 값은 곧바로 else 로 가서 **「빈 자료 저장소면 멈춘다  OK」** 를 찍는다. 가드가
# 통째로 사라진 판이 초록으로 나가는 모양이라 여기서 한 번 막는다.
#
# **다만 그 상황을 실제로 만들지는 못했다** (2026-09-01, 세 가지로 시도).
# 함수 **안**의 문법 오류는 sourcing 이 성공해서 함수가 멀쩡히 들어오고,
# sed 범위가 깨져 뒤엣것까지 딸려 오면 `set -u` 가 먼저 스크립트를 죽이며,
# 따옴표를 안 닫으면 bash 가 종료코드 2 로 통째로 죽는다 — 셋 다 시끄럽게 실패한다.
# 그러니 이 줄은 **재현된 결함을 고친 것이 아니라 못 본 갈래를 닫아 둔 것**이다.
# 지우지는 않는다. 값이 싸고, 「함수가 있다」는 이 시험 전체의 전제라 명시할 값어치가 있다.
if ! command -v check_data_ready > /dev/null 2>&1; then
  ng "check_data_ready() 를 불러들이지 못했습니다 — 뽑은 조각에 문법 오류가 있을 수 있습니다"
  echo ""; echo "통과 $pass · 실패 $fail"; rm -rf "$TMP"; exit 1
fi

# ① 빈 저장소면 막는다
if check_data_ready "$TMP/empty" 2>"$TMP/err"; then
  ng "빈 자료 저장소인데 통과했다"
else
  ok "빈 자료 저장소면 멈춘다"
  grep -q "init-archive" "$TMP/err" || ng "무엇을 해야 하는지 안 알려준다"
fi

# ② 설정과 틀이 둘 다 있으면 통과한다 — 막기만 하는 가드는 설치를 못 하게 만든다
if check_data_ready "$TMP/ready" 2>/dev/null; then
  ok "설정과 아카이브 틀이 있으면 통과한다"
else
  ng "설정과 틀이 다 있는데 막혔다"
fi

# ②' 설정만 있고 틀이 없으면 막는다 (위 `half` 대조군)
if check_data_ready "$TMP/half" 2>"$TMP/err2"; then
  ng "설정만 있고 아카이브 틀이 없는데 통과했다"
else
  ok "설정만 있고 틀이 없으면 멈춘다"
  grep -q "init-archive" "$TMP/err2" || ng "무엇을 해야 하는지 안 알려준다"
  # 「없다」와 「반만 있다」를 사람이 가려 읽을 수 있어야 한다. 둘이 같은 문구로 나오면
  # config.json 을 다시 복사하다가 진짜 원인인 init-archive 를 계속 안 돌리게 된다.
  grep -q "config.json 은 있는데" "$TMP/err2" \
    || ng "「설정이 아예 없다」와 문구가 같아 무엇이 빠졌는지 안 보인다"
fi

# ③ setup.sh 가 그 함수를 실제로 부르나
grep -q '^check_data_ready "\$DATA"' "$SETUP" \
  && ok "setup.sh 가 자료 복제 뒤 그 함수를 부른다" \
  || ng "함수는 있는데 부르는 곳이 없다"

rm -rf "$TMP"
echo ""
echo "통과 $pass · 실패 $fail"
[ "$fail" = 0 ] || exit 1
