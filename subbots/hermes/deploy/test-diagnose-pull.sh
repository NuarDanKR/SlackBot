#!/bin/sh
# setup.sh 의 `diagnose_pull_failure()` 를 **실제로 돌려** 본다.
#
#     sh deploy/test-diagnose-pull.sh
#
# 왜 필요한가: 이 함수는 지금까지 아무 시험도 안 지났다. 하는 일은 화면에 글자를 찍는
# 것뿐이고 판정이 틀려도 원문을 그대로 내므로 피해는 작지만, **그래서 틀려도 아무도
# 모른다.** 이 함수가 막으려는 상황(원격이 비어 있을 때 clone 해서 브랜치 이름이 어긋난
# 저장소)은 재실행 안내를 그대로 따라도 매번 같은 자리에서 다시 죽는 모양이라, 진단이
# 안 나오면 사람이 거기서 선다.
#
# setup.sh 는 VM 에 통째로 내려받아 root 로 도는 스크립트라 여기서 전부 돌릴 수 없다.
# **함수 하나만 떼어 내어** 돌린다 — `test-setup-guard.sh` 와 같은 방식이다.
#
# ── 못 잡는 구간 ──
#
# · 함수를 맨 셸에 source 하므로 setup.sh 가 켜는 `set -euo pipefail` 이 여기선 안 걸린다.
#   이 함수의 판정(①②③)은 그 옵션과 무관하지만 **앞뒤 맥락은 안 보인다.**
# · ④ 는 `grep` 으로 부르는 줄이 setup.sh 에 **있는지**만 본다 — 실행이 거기까지 **닿는지**
#   는 안 본다. `test-setup-guard.sh` 머리말에 적힌 것과 같은 틈이다.
# · `sudo` 를 대역으로 세운다. 진짜 setup.sh 는 `sudo -u hermes` 로 서비스 계정이 되어
#   git 을 부르는데, 여기서는 그냥 지금 사용자로 부른다. **git 판정은 같고 권한만 다르다.**
# · 이 틈을 메우는 것은 빈 VM 에서 setup.sh 를 처음부터 끝까지 밟아 보는 것뿐이고,
#   그건 사람만 할 수 있다.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
SETUP="$HERE/setup.sh"
TMP="${TMPDIR:-/tmp}/diagnose-pull.$$"
pass=0; fail=0
ok() { pass=$((pass+1)); echo "  OK  $1"; }
ng() { fail=$((fail+1)); echo "  NG  $1"; }

rm -rf "$TMP"; mkdir -p "$TMP"

# setup.sh 안의 헬퍼·변수를 흉내 낸다. 실물(setup.sh)의 warn 은 stdout 이다.
warn() { echo "   ! $*"; }
SVC_USER="hermes"
GIVEN_ARGS=" git@github.com:teamx/code.git git@github.com:teamx/data.git"
# `sudo -u <사용자> <명령…>` 에서 앞 두 토큰만 걷어내고 나머지를 그대로 돌린다.
sudo() { while [ "${1:-}" = "-u" ]; do shift 2; done; "$@"; }

# 함수만 뽑는다. 뽑히지 않으면 「없다」가 아니라 실패다.
sed -n '/^diagnose_pull_failure()/,/^}/p' "$SETUP" > "$TMP/fn.sh"
if [ ! -s "$TMP/fn.sh" ]; then
  ng "setup.sh 에서 diagnose_pull_failure() 를 뽑지 못했습니다"
  echo ""; echo "통과 $pass · 실패 $fail"; rm -rf "$TMP"; exit 1
fi
. "$TMP/fn.sh"

# **함수가 진짜로 들어왔나.** 없으면 아래 호출이 「명령이 없다」로 끝나고, 출력이 비어서
# ①② 의 「원문만 낸다」류 판정이 **통째로 초록으로 나간다.** 이 시험 전체의 전제다.
if ! command -v diagnose_pull_failure > /dev/null 2>&1; then
  ng "diagnose_pull_failure() 를 불러들이지 못했습니다 — 뽑은 조각에 문법 오류가 있을 수 있습니다"
  echo ""; echo "통과 $pass · 실패 $fail"; rm -rf "$TMP"; exit 1
fi

# 판이 셋. 이름이 곧 상태다.
#   filled  — 커밋이 있는 정상 저장소
#   unborn  — 원격이 비어 있을 때 clone 한 모양 (커밋 0개, HEAD 가 아직 아무것도 안 가리킴)
#   broken  — .git 은 있는데 저장소로 안 읽히는 것
git init -q "$TMP/filled" 2>/dev/null
(cd "$TMP/filled" && git -c user.email=t@t -c user.name=t commit -q --allow-empty -m first) 2>/dev/null
git init -q -b hermes-default-branch "$TMP/unborn" 2>/dev/null
mkdir -p "$TMP/broken/.git"

REF_ERR="fatal: couldn't find remote ref main
error: pathspec ... no such ref was fetched"
OTHER_ERR="fatal: Could not read from remote repository."

# ① 커밋이 있는 저장소는 **원인이 다르다** — 진단하지 않고 원문만 낸다.
#    이 갈래가 없으면 네트워크·권한 실패에 「브랜치 이름이 어긋났다」는 엉뚱한 안내가 붙는다.
out=$(diagnose_pull_failure "$TMP/filled" "코드" "$REF_ERR" 2>&1)
if echo "$out" | grep -q "비어 있었고"; then
  ng "커밋이 있는 저장소인데 「비어 있었다」고 진단했다"
else
  echo "$out" | grep -q "no such ref was fetched" \
    && ok "커밋이 있으면 진단하지 않고 git 원문을 그대로 낸다" \
    || ng "커밋이 있을 때 git 원문이 안 나왔다 — 사람이 볼 것이 사라진다"
fi

# ② 커밋이 없어도 **에러 모양이 다르면** 진단하지 않는다. 둘이 같이 있어야 확신이 선다.
out=$(diagnose_pull_failure "$TMP/unborn" "자료" "$OTHER_ERR" 2>&1)
if echo "$out" | grep -q "비어 있었고"; then
  ng "에러 모양이 다른데 「브랜치 이름이 어긋났다」고 진단했다"
else
  echo "$out" | grep -q "Could not read from remote" \
    && ok "에러 모양이 다르면 진단하지 않고 git 원문을 그대로 낸다" \
    || ng "진단 안 할 때 git 원문이 안 나왔다"
fi

# ③ 둘 다 맞으면 진단한다 — **그리고 실제 브랜치 이름을 집어야 한다.**
#    `알 수 없음` 으로 떨어지면 사람은 무엇이 어긋났는지 못 본다.
out=$(diagnose_pull_failure "$TMP/unborn" "자료" "$REF_ERR" 2>&1)
if echo "$out" | grep -q "비어 있었고"; then
  ok "커밋 0개 + ref 못 받음 이면 원인을 진단한다"
  echo "$out" | grep -q "hermes-default-branch" \
    || ng "로컬 브랜치 이름을 못 집었다 (무엇이 어긋났는지 안 보인다)"
  echo "$out" | grep -q "알 수 없음" \
    && ng "브랜치 이름이 있는데 「알 수 없음」으로 떨어졌다"
  # 되돌리는 법이 없으면 진단만 하고 사람을 세워 둔다.
  echo "$out" | grep -q "rm -rf $TMP/unborn" \
    || ng "지우고 새로 받으라는 실제 경로가 안 나온다"
  # **인자를 그대로 되돌려 줘야 한다.** 인자 없이 다시 돌리면 원저자 저장소로 간다.
  echo "$out" | grep -q "teamx/code.git" \
    || ng "재실행 안내가 넘긴 저장소 주소를 안 되돌려 준다"
else
  ng "커밋 0개 + ref 못 받음 인데 진단하지 않았다"
fi

# ③' git 이 아예 안 읽히는 폴더면 브랜치 이름을 못 집는다 — 그때는 「알 수 없음」이 맞다.
#     빈 값이 그대로 새어 `로컬 브랜치: ` 로 끝나면 사람은 줄이 잘린 줄 안다.
out=$(diagnose_pull_failure "$TMP/broken" "자료" "$REF_ERR" 2>&1)
echo "$out" | grep -q "로컬 브랜치: 알 수 없음" \
  && ok "브랜치 이름을 못 집으면 「알 수 없음」으로 적는다" \
  || ng "브랜치 이름을 못 집었는데 빈칸으로 나갔다"

# ④ setup.sh 가 그 함수를 실제로 부르나. 부르는 곳이 없으면 위 판정은 전부 죽은 코드다.
grep -q 'diagnose_pull_failure "\$2" "\$3" "\$pull_err"' "$SETUP" \
  && ok "setup.sh 가 pull 실패 자리에서 그 함수를 부른다" \
  || ng "함수는 있는데 pull 실패 자리에서 부르는 곳이 없다"

rm -rf "$TMP"
echo ""
echo "통과 $pass · 실패 $fail"
[ "$fail" = 0 ] || exit 1
