#!/usr/bin/env bash
# setup.sh 의 **마지막 토막**(서비스 시작 → 상태 출력 → 안내)을 실제로 돌려 본다.
#
#     bash deploy/test-setup-tail.sh
#
# 왜 필요한가 — 두 가지가 여기서 조용히 어긋났다.
#
#   ① `systemctl enable --now hermes` 는 **이미 도는 유닛에 아무 일도 안 한다.**
#      `--now` 가 부르는 것이 start 이고, start 는 active 유닛에 무동작이기 때문이다.
#      그런데 setup.sh 는 7단계에서 코드를 실제로 pull 한다 — 그래서 재실행하면 새 코드를
#      받아 놓고 옛 코드가 계속 도는 채로 「완료」를 찍었다. `git log` 에는 새 커밋이 보이고
#      pull 도 성공해서 **에러도 경고도 안 난다.**
#   ② `systemctl status` 는 유닛이 active 가 아니면 **종료코드 3** 을 낸다. `| head -5` 를
#      붙여도 `set -euo pipefail` 아래서는 그 3 이 파이프라인 값이 되어 스크립트가 거기서
#      죽고, **아래 안내(로그 보는 법·재시작·점검·배포)가 한 줄도 안 나온다** — 봇이 안 떴을
#      때 사람에게 가장 필요한 네 줄이 하필 그때 사라진다.
#
# setup.sh 는 VM 에 통째로 내려받아 root 로 도는 스크립트라 여기서 전부 돌릴 수 없다.
# **마지막 토막만 떼어 내어** 돌린다 — `test-setup-guard.sh` 와 같은 방식이다.
#
# ── 못 잡는 구간 ──
#
# · `systemctl` 을 대역으로 세운다. 실제 systemd 가 restart 를 어떻게 다루는지는 안 본다 —
#   여기서 보는 것은 **setup.sh 가 무엇을 부르고 실패를 어떻게 다루나**뿐이다.
# · 「restart 가 정말 새 코드를 붙이나」는 VM 에서만 확인된다. 여기서는 부르는지만 본다.
# · 토막을 `sed` 로 뽑으므로, 뽑는 자리(`systemctl enable hermes` 줄)를 고치면 이 시험이
#   먼저 「못 뽑았다」로 실패한다. 조용히 지나가지는 않는다.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
SETUP="$HERE/setup.sh"
TMP="${TMPDIR:-/tmp}/setup-tail.$$"
pass=0; fail=0
ok() { pass=$((pass+1)); echo "  OK  $1"; }
ng() { fail=$((fail+1)); echo "  NG  $1"; }

rm -rf "$TMP"; mkdir -p "$TMP"

# ── ① 글자로 보는 것 — 무엇을 부르나 ─────────────────────────────────────────
#
# 실행 시험(②)만으로는 `enable --now` 로 되돌린 판을 못 잡는다. 대역 systemctl 은
# `enable --now hermes` 에도 순순히 0 을 돌려주기 때문이다. 그래서 한 번 글자로 못 박는다.

grep -qE '^systemctl restart hermes( |$)' "$SETUP" \
  && ok "실행되는 restart 가 스크립트에 있다 (안내 문자열 말고)" \
  || ng "실행되는 restart 가 없다 — 재실행이 새 코드를 못 붙인다"

# **실행되는 줄만 본다** (`^` — 들여쓰기 없음). 안내 문자열 속의
# `3) 시작:  sudo systemctl enable --now hermes` 는 그대로 둬야 한다 — 그 갈래는 토큰이
# 아직 빈 경우라 봇이 **한 번도 안 뜬** 상태이고, 거기서는 `enable --now` 가 맞다.
# `hermes-archive-pull.timer` 도 그대로 둔다 (타이머는 재시작 대상이 아니다).
if grep -qE '^systemctl enable --now hermes( |$)' "$SETUP"; then
  ng "봇 유닛을 아직 'enable --now' 로 켠다 — 이미 도는 유닛에는 무동작이다"
else
  ok "봇 유닛을 'enable --now' 로 켜지 않는다"
fi

# ── ② 실제로 돌려 보는 것 — 실패를 어떻게 다루나 ────────────────────────────

# 마지막 토막을 뽑는다. 뽑히지 않으면 「없다」가 아니라 실패다.
sed -n '/^systemctl enable hermes/,$p' "$SETUP" > "$TMP/tail.sh"
if [ ! -s "$TMP/tail.sh" ]; then
  ng "setup.sh 에서 마지막 토막을 뽑지 못했습니다 (기준 줄: 'systemctl enable hermes')"
  echo ""; echo "통과 $pass · 실패 $fail"; rm -rf "$TMP"; exit 1
fi

# 대역과 변수를 앞에 붙인다. **`set -euo pipefail` 을 실물과 똑같이 켠다** — 이 시험이
# 보려는 것이 바로 그 옵션 아래의 동작이라, 여기서 빼면 ②가 통째로 무의미해진다.
make_runner() {   # $1=restart 가 낼 종료코드
  {
    echo 'set -euo pipefail'
    echo 'SVC_USER=hermes'
    echo 'APP=/opt/hermes/code'
    echo "RESTART_RC=$1"
    echo 'say()  { printf "== %s\n" "$*"; }'
    echo 'ok()   { printf "   OK %s\n" "$*"; }'
    echo 'warn() { printf "   ! %s\n" "$*"; }'
    echo 'sleep() { :; }'
    # 유닛이 안 떴을 때를 재현한다 — status 는 종료코드 3 이다.
    echo 'systemctl() {'
    echo '  case "$*" in'
    echo '    *restart*) return "$RESTART_RC" ;;'
    echo '    *status*)  echo "● hermes.service - Hermes"; echo "   Active: inactive (dead)"; return 3 ;;'
    echo '    *)         return 0 ;;'
    echo '  esac'
    echo '}'
    cat "$TMP/tail.sh"
  } > "$TMP/run-$1.sh"
}

# ②-a 재시작이 성공했는데 status 가 3 을 내는 경우 (유닛이 곧바로 죽은 모양).
#      안내 네 줄이 다 나와야 하고, 종료코드는 재시작 결과인 0 이어야 한다.
make_runner 0
out=$(bash "$TMP/run-0.sh" 2>&1); rc=$?
missing=""
for line in "journalctl -u hermes -f" "sudo systemctl restart hermes" "npm run check" "git -C /opt/hermes/code pull"; do
  case "$out" in *"$line"*) : ;; *) missing="$missing / $line" ;; esac
done
if [ -n "$missing" ]; then
  ng "status 가 3 을 내자 아래 안내가 안 나왔다 (빠진 것:$missing)"
  echo "$out" | sed 's/^/        │ /'
else
  ok "status 가 3 을 내도 안내 네 줄이 다 나온다"
fi
[ "$rc" -eq 0 ] && ok "재시작이 성공하면 종료코드 0" \
                || ng "재시작은 성공했는데 종료코드가 $rc 다 (status 의 3 이 새어 나왔다)"

# ②-b 재시작 자체가 실패한 경우. 안내는 그대로 나오되 **종료코드로 사실을 말해야 한다** —
#      여기서 0 으로 끝나면 「완료」가 거짓말이 되고, 부르는 사람이 성공으로 읽는다.
make_runner 1
out=$(bash "$TMP/run-1.sh" 2>&1); rc=$?
case "$out" in
  *"journalctl -u hermes -f"*) ok "재시작이 실패해도 로그 보는 법은 알려준다" ;;
  *) ng "재시작이 실패했는데 안내가 안 나왔다 — 사람이 볼 것이 없다" ;;
esac
case "$out" in
  *"재시작이 실패"*) ok "재시작 실패를 화면에 말한다" ;;
  *) ng "재시작이 조용히 실패했다" ;;
esac
[ "$rc" -eq 1 ] && ok "재시작이 실패하면 종료코드로도 알린다" \
                || ng "재시작이 실패했는데 종료코드가 $rc 다"

rm -rf "$TMP"
echo ""
echo "통과 $pass · 실패 $fail"
[ "$fail" = 0 ] || exit 1
