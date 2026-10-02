#!/bin/sh
# push 관문 시험 — `pre-push` 와 `push-approve` 를 **완전히 별개인 임시 저장소**에서 돌린다.
#
# 이 워크스페이스 저장소에서는 시험하지 않는다. `CLAUDE.md` 하드 룰:
# 「검증 목적도 예외가 아니고 임시 워크트리도 예외가 아니다 — 스크래치패드에
#  `git init` 으로 완전히 별개인 저장소를 만들어 거기서 한다.」
# 워크트리는 같은 저장소라 예외가 아니다.
#
# 돌리는 법:  sh .githooks/test-push-gate.sh
#
# **손으로 안 쳐도 돈다** (2026-08-31 부터). `pre-commit`·`pre-push`·`push-approve`·이 파일이
# 커밋에 담기면 `pre-commit` 이 **인덱스에서 꺼낸 사본**으로 이 시험을 돌리고, 실패하면 커밋을
# 막는다. 방아쇠를 「관문을 손댔다」로 둔 것은 이 시험이 오래 걸려서다 — `npm run check`
# 에 붙이면 그 명령이 2분 반이 되고, 그러면 안 치게 된다.
#
# **`pre-commit` 이 방아쇠에 든 것은 2026-09-06 부터고, 그때 15·16번을 함께 붙였다.**
# 그 시험을 부르는 자리가 `pre-commit` 인데 거기서는 안 돌아서, 호출부를 깨도 조용했다.
# 다만 방아쇠만 더하는 것으로는 안 잡힌다 — 실측으로 확인했다(아래 15번 주석).
#
# **재는 자리에 따라 값이 다르다** (2026-08-31 실측): 여기서 그냥 돌리면 **81·88초**,
# `pre-commit` 이 인덱스 사본으로 돌리면 **59초**다. 사본 옆에는 `.claude/skills/` 가
# 없어서 아래 임시 저장소들이 파이썬 관문을 안 거치기 때문이다. 커밋이 1분 만에
# 끝났다고 **시험이 안 돈 것으로 읽지 마세요** — 그게 정상 값이다.
#
# **막는 것뿐 아니라 통과하는 것도 시험한다.** 늘 막기만 하는 관문은 시험이
# 전부 초록인데도 쓸모가 없고, 그 고장은 「우회가 습관이 됐다」로만 드러난다.
set -u

# ── 바깥의 git 환경변수를 끊는다 (2026-09-06) ─────────────────────────────
#
# 이 시험은 **자기 저장소를 스스로 만든다.** 그런데 `GIT_INDEX_FILE` 이 환경에 걸려
# 있으면 그 아래 `git init`·`git add` 가 전부 **남의 인덱스**에 쓴다 — 시험 저장소의
# 파일이 그 인덱스에 섞이고, blob 은 시험이 끝나며 지워지는 임시 저장소에 남는다.
#
# **이건 가상의 상황이 아니라 `git commit -- <경로>` 의 기본 동작이다.** 경로를 지정해
# 커밋하면 git 이 임시 인덱스를 만들어 훅에 `GIT_INDEX_FILE` 로 물려준다. 그리고 그
# 형태는 **다른 창의 세션이 무언가를 스테이징해 둔 날 반드시 쓰게 되는 형태**다 —
# 내 것만 경로로 지정해 담아야 남의 변경이 안 실린다. 실측 2026-09-06 — 이 줄이 없으면
# 그 조건에서 이 시험이 **16 통과 · 19 실패**로 무너지고, 커밋이 막히는데 **막힌 이유가
# 그 커밋의 내용과 아무 상관이 없다.**
#
# **이 파일은 두 저장소가 글자까지 같아야 한다** (`check-hook-drift.py`). 그래서 이
# 주석은 어느 쪽에도 없는 파일을 가리키지 않는다.
unset GIT_INDEX_FILE GIT_DIR GIT_WORK_TREE GIT_OBJECT_DIRECTORY \
      GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_COMMON_DIR 2>/dev/null || true

HOOKS=$(cd "$(dirname "$0")" && pwd)
# **15·16 번(아래)이 쓰려고 원래 값을 먼저 챙겨 둔다.** 이 환경에서 `TMP` 은 이미
# 내보내진(exported) 윈도우 변수라, 바로 다음 줄이 그 값을 이 시험용 경로로 덮어써서
# 이 스크립트가 띄우는 모든 자식 프로세스가 그 값을 물려받는다. 15·16 번은 실제
# `pre-commit` 을 훅으로 진짜로 부르는데, 그 자식이 새로 뜰 때 (비어 있던) `TMPDIR` 을
# 이 물려받은 `TMP` 값으로 스스로 채우면 `pre-commit` 의 `${TMPDIR:-/tmp}` 자리들이
# 1~14 번이 이미 지운 디렉터리를 가리켜 죽는다 (파급 관문 시험에서 실제로 겪은 자리).
ORIG_TMP="${TMP:-}"
TMP="${TMPDIR:-/tmp}/push-gate-test.$$"
OUT="$TMP/out.txt"
pass=0
fail=0

ok() { pass=$((pass + 1)); echo "  OK  $1"; }
ng() { fail=$((fail + 1)); echo "  NG  $1"; }

# 임시 저장소를 새로 만든다. **기준점 push 를 먼저 하고 그 다음에 훅을 건다** —
# 이렇게 하면 준비 단계에서 `--no-verify` 를 쓸 일이 없다.
setup() {
  rm -rf "$TMP"
  mkdir -p "$TMP"
  git init --bare -q "$TMP/remote.git"
  git init -q "$TMP/work"
  cd "$TMP/work" || exit 1
  git config user.email t@example.invalid
  git config user.name Tester
  git config core.autocrlf false  # 줄바꿈 경고가 시험 결과를 덮지 않게
  git remote add origin "$TMP/remote.git"
  echo base >f.txt
  git add f.txt
  git commit -qm base
  git branch -M main
  git push -q origin main
  git branch -q --set-upstream-to=origin/main main
  git config core.hooksPath "$HOOKS"
}

mk() { # mk <메시지> — 커밋 하나를 쌓는다
  echo "$1" >>f.txt
  git add f.txt
  git commit -qm "$1"
}

approve() { sh "$HOOKS/push-approve"; }

# 원격만 한 칸 앞서게 만든다 — 다른 기계(VM)가 먼저 올린 상태.
# 커밋을 올린 뒤 로컬을 한 칸 되돌리는 것으로 만든다. 저장소를 하나 더 두는 것보다
# 짧고, 결과(로컬이 뒤처짐)는 같다.
# **훅을 잠깐 끄는 것은 흉내가 아니라 사실에 가깝다** — VM 은 `core.hooksPath` 를
# 안 걸어 두므로(CLAUDE.md) 실제로 이 관문 없이 push 한다.
remote_ahead() {
  git config --unset core.hooksPath
  # **`mk` 와 다른 파일을 쓴다.** 같은 파일이면 10번 시험의 병합이 충돌해서,
  # 재려던 것(들여온 뒤 통과하나) 대신 충돌 처리를 재게 된다.
  echo vm >>vm.txt
  git add vm.txt
  git commit -qm "VM 자동 반영"
  git push -q origin main
  git reset -q --hard HEAD~1
  git config core.hooksPath "$HOOKS"
}

echo "push 관문 시험 — $TMP"

# ── 1. 승인 기록이 없으면 막는다 ────────────────────────────────────────────
setup
mk mine1
if git push origin main >"$OUT" 2>&1; then
  ng "승인 기록이 없는데 push 가 통과했다"
else
  ok "승인 기록이 없으면 막는다"
  grep -q "push-approve" "$OUT" || ng "막으면서 기록 명령을 안 알려준다"

  # **안내한 경로가 실제로 있어야 한다.** 이 시험 저장소는 `core.hooksPath` 로 남의
  # `.githooks` 를 가리키는데, 자료 저장소가 바로 그 모양이다. 예전에는 안내가
  # `.githooks/push-approve` 를 글자로 박아서 거기서는 「그런 파일이 없습니다」가 났고,
  # 막히긴 했으니 아무 검사에도 안 걸렸다 (2026-09-03 실제로 겪음).
  hinted=$(sed -n 's/.*승인받으려면: sh "\(.*\)".*/\1/p' "$OUT" | head -1)
  if [ -z "$hinted" ]; then
    ng "안내가 칠 수 있는 경로를 따옴표로 안 찍는다"
  elif [ -f "$hinted" ]; then
    ok "안내한 경로에 push-approve 가 실제로 있다"
  else
    ng "안내한 경로가 없다: $hinted"
  fi
fi

# ── 2. 승인한 그대로면 통과한다 ────────────────────────────────────────────
approve >/dev/null 2>&1
if git push -q origin main 2>"$OUT"; then
  ok "승인한 목록 그대로면 통과한다"
else
  ng "승인했는데도 막혔다: $(cat "$OUT")"
fi

# ── 3. 승인 뒤 커밋이 늘면 막고, 그 커밋을 지목한다 (2026-08-06 형태) ──────
setup
mk mine1
approve >/dev/null 2>&1
mk other1
OTHER=$(git rev-parse --short HEAD)
if git push origin main >"$OUT" 2>&1; then
  ng "승인 뒤 커밋이 늘었는데 통과했다"
else
  ok "승인 뒤 커밋이 늘면 막는다"
  if grep -q "$OTHER" "$OUT"; then
    ok "늘어난 커밋을 SHA 로 지목한다 ($OTHER)"
  else
    ng "늘어난 커밋을 지목하지 않는다"
  fi
fi

# ── 4. 승인 시점에 남의 커밋이 이미 아래 있으면 목록에 함께 보인다 ────────
#    2026-08-05 의 진짜 고침이 이것이다. 그때 본 것은 `git status` 였고
#    거기에는 **이미 커밋된 남의 이력이 안 보인다.**
setup
mk other_earlier
EARLIER=$(git rev-parse --short HEAD)
mk mine1
if approve 2>&1 | grep -q "$EARLIER"; then
  ok "아래 깔린 남의 커밋이 승인 목록에 함께 보인다 ($EARLIER)"
else
  ng "아래 깔린 남의 커밋이 승인 목록에 안 보인다"
fi

# ── 5. 커밋이 사라져도 막는다 (amend·rebase) ──────────────────────────────
setup
mk mine1
approve >/dev/null 2>&1
git commit -q --amend -m mine1-고침
if git push origin main >"$OUT" 2>&1; then
  ng "amend 로 목록이 바뀌었는데 통과했다"
else
  ok "커밋이 바뀌어 사라지면 막는다"
  # amend 로 밀려난 커밋도 **객체로는 남아 있다**(reflog 가 잡는다). 그래도 사유는
  # amend·rebase 가 맞아야 한다 — 13번(이미 원격에 들어간 것)과 가르는 기준은
  # 「실재하나」가 아니라 「원격 끝의 조상인가」다.
  if grep -q "amend·rebase" "$OUT"; then
    ok "사라진 사유를 amend·rebase 로 부른다"
  else
    ng "amend 였는데 사유에 amend·rebase 가 안 나온다"
  fi
fi

# ── 6. main 이 아닌 브랜치는 승인 없이 통과한다 ───────────────────────────
#    WHK 결정 2026-08-29. 안 나가는 것까지 막으면 우회가 습관이 되고 장치가 죽는다.
setup
git checkout -q -b sdd/시험가지
mk 가지작업
if git push -q origin sdd/시험가지 2>"$OUT"; then
  ok "작업 브랜치는 승인 없이 통과한다"
else
  ng "작업 브랜치가 막혔다: $(cat "$OUT")"
fi

# ── 7. 훅 파일이 없는 저장소는 아무것도 안 막는다 ─────────────────────────
#    `pre-commit` 과 같은 원칙 — 스킬을 일부만 가져간 곳에서 남의 push 를 막지 않는다.
setup
git config --unset core.hooksPath
mk 훅없음
if git push -q origin main 2>"$OUT"; then
  ok "훅을 안 걸면 통과한다"
else
  ng "훅을 안 걸었는데 막혔다: $(cat "$OUT")"
fi

# ── 8. 원격이 앞서 있으면 승인을 적지 않고 멈춘다 ────────────────────────
#    2026-08-30 실물: `push-approve` 가 fetch 해 놓고 「뒤처졌나」를 안 봐서
#    「이 목록 그대로일 때만 통과합니다」라고 적어 놓고 push 가 거절됐다.
setup
remote_ahead
mk mine1
if approve >"$OUT" 2>&1; then
  ng "원격이 앞서 있는데 승인 기록을 적었다"
else
  ok "원격이 앞서 있으면 멈춘다"
  grep -q "git pull\|git merge" "$OUT" || ng "들여오는 명령을 안 알려준다"
  grep -q "VM 자동 반영" "$OUT" || ng "들어와야 할 커밋을 이름으로 지목하지 않는다"
fi
if [ -f .git/push-approved ]; then
  ng "멈췄는데 승인 기록이 생겼다"
else
  ok "멈출 때 승인 기록을 안 적는다"
fi

# ── 9. 내 커밋이 없어도 「origin 과 같습니다」라고 하지 않는다 ────────────
#    이쪽이 더 나쁘다 — 「같다」고 단정한다.
setup
remote_ahead
if approve >"$OUT" 2>&1; then
  ng "원격이 앞서 있는데 통과했다"
else
  ok "내 커밋이 0건이어도 멈춘다"
fi
if grep -q "같습니다" "$OUT"; then
  ng "origin 이 앞서 있는데 「같습니다」라고 말한다"
else
  ok "「같습니다」라고 단정하지 않는다"
fi

# ── 10. 들여온 뒤에는 정상으로 통과한다 ──────────────────────────────────
#    막기만 하고 길을 안 열어주면 우회가 습관이 된다.
setup
remote_ahead
mk mine1
git fetch -q origin main
git merge -q --no-edit FETCH_HEAD
if approve >"$OUT" 2>&1; then
  ok "들여온 뒤에는 승인 목록이 나온다"
else
  ng "들여왔는데도 막혔다: $(tail -2 "$OUT")"
fi
if git push -q origin main 2>"$OUT"; then
  ok "그 목록으로 push 가 통과한다"
else
  ng "승인했는데 push 가 막혔다: $(cat "$OUT")"
fi

# ── 11. 원격이 비어 있어도(첫 push) 승인을 받을 수 있다 ──────────────────
#    2026-08-31 실물: `push-approve` 가 fetch 부터 해서, 새로 만든 빈 저장소에서는
#    「origin 을 읽지 못했다」로 멈췄다. 승인 기록을 만들 길이 없으니 **첫 push 만
#    관문 밖**이 되는데, 그날이 이력 전부가 한 번에 나가는 날이다.
setup_empty() {
  rm -rf "$TMP"
  mkdir -p "$TMP"
  git init --bare -q "$TMP/remote.git"   # main 이 없는 빈 원격
  git init -q "$TMP/work"
  cd "$TMP/work" || exit 1
  git config user.email t@example.invalid
  git config user.name Tester
  git config core.autocrlf false
  git remote add origin "$TMP/remote.git"
  git checkout -q -b main
  git config core.hooksPath "$HOOKS"
}

setup_empty
mk first1
mk first2
if git push origin main >"$OUT" 2>&1; then
  ng "승인 기록이 없는데 첫 push 가 통과했다"
else
  ok "첫 push 도 승인 없이는 막는다"
  # 막을 때 나갈 커밋이 찍혀야 한다. 전에는 원격에 main 이 없어 `git log 000…000..HEAD`
  # 가 `Invalid revision range` 로 죽고 `2>/dev/null` 이 그것을 지워 **한 줄도 안
  # 찍혔다** (2026-09-02 발견). 같은 상황을 `push-approve` 는 「첫 push 라 이력 N건이
  # 통째로 나갑니다」로 다룬다 — 두 파일이 같은 갈래에서 갈려 있었다.
  if grep -q "first1" "$OUT"; then
    ok "첫 push 를 막으면서 나갈 커밋을 이름으로 찍는다"
  else
    ng "첫 push 를 막으면서 나갈 커밋 목록을 한 줄도 못 찍는다"
  fi
fi
if approve >"$OUT" 2>&1; then
  ok "원격이 비어 있어도 승인 목록이 나온다"
  grep -q "첫 push" "$OUT" || ng "첫 push 라는 것을 화면에 안 알린다"
else
  ng "빈 원격에서 승인이 막혔다: $(tail -3 "$OUT")"
fi
if git push -q origin main 2>"$OUT"; then
  ok "그 목록으로 첫 push 가 통과한다"
else
  ng "첫 push 를 승인했는데 막혔다: $(cat "$OUT")"
fi

# ── 12. 원격을 못 읽는 것은 「비어 있다」와 다르다 ────────────────────────
#    가르지 않으면 네트워크가 끊긴 날 「나갈 것 없음」이나 「전부 나감」으로 읽는다.
setup_empty
mk first1
git remote set-url origin "$TMP/nowhere.git"
if approve >"$OUT" 2>&1; then
  ng "원격을 못 읽는데 승인이 나왔다"
else
  ok "원격을 못 읽으면 멈춘다"
  grep -q "읽지 못해" "$OUT" || ng "못 읽었다는 사유를 안 알려준다"
fi
if [ -f .git/push-approved ]; then
  ng "못 읽었는데 승인 기록을 적었다"
else
  ok "못 읽을 때 승인 기록을 안 적는다"
fi

# ── 13. 이미 원격에 들어간 커밋을 「amend·rebase 로 바뀐 것」이라고 부르지 않는다 ──
#    2026-09-02 실물(`4fac5a6`·`bc55d7a`): 다른 세션이 승인받아 push 한 커밋은 다음
#    사람의 나갈 목록에서는 빠지지만 승인 기록에는 남는다. 그때 사유를 amend·rebase 라
#    부르면 사람이 reflog 같은 엉뚱한 곳을 본다. `git cat-file -e` 로는 못 가른다 —
#    amend 로 밀려난 커밋도 객체로는 남는다(5번 주석). 「원격 끝의 조상인가」로 가른다.
setup
mk 남이승인한것
approve >/dev/null 2>&1
git push -q origin main 2>/dev/null   # 다른 세션이 승인대로 push 까지 마친 상태
mk mine1                              # 다음 사람의 새 커밋 — 승인 기록은 낡은 채다
if git push origin main >"$OUT" 2>&1; then
  ng "승인 기록이 낡았는데 통과했다"
else
  ok "승인 기록이 낡으면 막는다"
  if grep -q "이미 원격에 들어간" "$OUT" && grep -q "남이승인한것" "$OUT"; then
    ok "이미 원격에 들어간 커밋은 그렇게 부르고 제목까지 찍는다"
  else
    ng "이미 원격에 들어간 커밋의 사유가 틀리거나 제목이 없다"
  fi
  if grep -q "amend·rebase" "$OUT"; then
    ng "이미 원격에 들어간 커밋을 amend·rebase 라고 부른다"
  else
    ok "amend·rebase 라고 부르지 않는다"
  fi
fi

# ── 14. 훅 경로가 역슬래시(윈도우 절대경로)여도 안내한 명령이 실제로 돈다 ─────
#    2026-09-03 실측: `core.hooksPath` 를 역슬래시로 걸면 `--git-path hooks` 도
#    역슬래시를 그대로 준다. 1번 시험은 슬래시 hooksPath 라 이 갈래를 한 번도 안
#    지났고, `test -f` 까지만 봤지 안내한 명령을 `sh` 로 실제 실행해 본 적이 없었다.
#    cygpath 가 없는 곳(리눅스·VM)에는 역슬래시 경로 자체가 안 생기므로 건너뛴다.
if command -v cygpath >/dev/null 2>&1; then
  setup
  git config core.hooksPath "$(cygpath -w "$HOOKS")"
  mk mine1
  if git push origin main >"$OUT" 2>&1; then
    ng "역슬래시 hooksPath 에서 push 가 통과했다"
  else
    ok "역슬래시 hooksPath 에서도 막는다"
    hinted=$(sed -n 's/.*승인받으려면: sh "\(.*\)".*/\1/p' "$OUT" | head -1)
    if [ -n "$hinted" ] && sh "$hinted" >"$OUT" 2>&1; then
      ok "역슬래시 안내 경로를 sh 로 실제 실행하면 돈다"
      grep -q "나갈 커밋" "$OUT" || ng "실행은 됐는데 승인 목록이 안 나온다"
    else
      ng "안내한 경로가 sh 로 실행되지 않는다: $hinted"
    fi
  fi
else
  echo "  --  14번(역슬래시 hooksPath)은 cygpath 가 없어 건너뜀"
fi

cd / || exit 1
rm -rf "$TMP"

# ── 15·16 은 위 1~14 와 다른 종류다. 여기까지는 `pre-push`·`push-approve` 의 행동을 잰다.
# 15·16 은 `pre-commit` 이 **push 관문을 고치는 커밋에서 이 시험을 부르는 자리**(인덱스에서
# 꺼낸 스테이징된 판을 재는 것 · 시험 파일이 지워지면 막는 것)를 잰다. 파급 관문 시험의
# 8·9 번과 같은 구조이고, 같은 이유로 `pre-commit`·`test-push-gate.sh` 를 함께 추적하는
# **새 임시 저장소**가 필요하다.
#
# **왜 붙였나** (2026-09-06 실측): 방아쇠에 `pre-commit` 을 더하는 것만으로는 아무것도
# 안 잡혔다. `pre-commit` 의 방아쇠 목록에서 `pre-push` 를 빼는 고장을 넣고 돌려도 위
# 1~14 는 **통과 27 · 실패 0** 그대로였고(파급 관문 시험도 통과 10 · 실패 0), 여기 15번을
# 붙이자 그 고장과 「스테이징된 판 대신 디스크 판을 부르게 바꾼 고장」이 둘 다 NG 로 잡혔다.
# 성한 판에서는 둘 다 OK 였다(대조군).

META="${TMPDIR:-/tmp}/push-gate-meta.$$"
META_OUT="$META/out.txt"  # $OUT 은 이미 지운 $TMP 밑이라 못 쓴다 — $META 밑에 따로 둔다

meta_setup() {
  # **`TMP` 을 원래 값으로 되돌린다** (위 `ORIG_TMP` 주석 참고). 이 함수 뒤에 `git commit`
  # 이 진짜 `pre-commit` 을 훅으로 부르고, 그 자식이 이 값을 물려받는다.
  TMP="$ORIG_TMP"
  # **`HERMES_GATE_TEST_RUNNING` 을 지운다.** 이 스크립트가 `pre-commit` 의 자기수정
  # 시험으로(즉 그 값이 1 로 걸린 채) 불렸을 수 있다 — 남아 있으면 아래 메타 저장소에
  # 진짜로 거는 `pre-commit` 도 물려받아 **자기 방아쇠 감지 블록 전체를 건너뛴다**.
  # 그러면 15번은 아무것도 안 재고 통과하고 16번은 막아야 할 삭제를 통과시킨다.
  # 파급 관문 시험 8·9 번이 같은 자리에서 겪은 사고다.
  unset HERMES_GATE_TEST_RUNNING
  unset HANDOFF_GATE_TEST_RUNNING
  rm -rf "$META"
  mkdir -p "$META/.githooks"
  cd "$META" || exit 1
  git init -q .
  git config user.email t@example.invalid
  git config user.name Tester
  git config core.autocrlf false
  cp "$HOOKS/pre-commit" .githooks/pre-commit
  cp "$HOOKS/pre-push" .githooks/pre-push
  cp "$HOOKS/push-approve" .githooks/push-approve
  # **이 파일(이 스크립트) 자체는 복사해 넣지 않는다.** 넣으면 아래 커밋이 진짜
  # `pre-commit` 을 거치면서 이 시험을 다시 부르고, 그 안의 15·16 번이 또 메타 저장소를
  # 만들어 겹겹이 재귀한다. 15·16 이 재는 것은 「자기수정 판정이 맞게 도나」뿐이라 시험
  # 파일 내용 자체는 무관하다 — 늘 통과하는 최소 스텁으로 대신한다.
  printf '#!/bin/sh\nexit 0\n' >.githooks/test-push-gate.sh
  chmod +x .githooks/pre-commit .githooks/test-push-gate.sh
  git add .githooks
  git commit -qm "기준판"
  # **훅은 기준판을 커밋한 뒤에 건다.** 먼저 걸면 이 최초 커밋 자체가 자기수정으로
  # 잡혀 재려는 것과 다른 것을 재게 된다.
  git config core.hooksPath "$META/.githooks"
}

# ── 15. 자기수정 시험은 스테이징된 판을 잰다 (디스크 판이 아니라) ────────
#    기준판의 (늘 통과하는 스텁) `test-push-gate.sh` 를 **디스크에서만 늘 실패하는
#    것으로 바꿔치고 스테이징은 안 한다.** `pre-push` 만 고쳐 커밋한다 — 통과해야
#    스테이징된(원래 스텁) 판을 잰 것이고, 막히면 디스크의 깨진 판을 잰 것이다.
meta_setup
printf '#!/bin/sh\nexit 1\n' >.githooks/test-push-gate.sh
echo "# 시험용 표시" >>.githooks/pre-push
git add .githooks/pre-push
if git commit -qm "push 관문 손댐" >"$META_OUT" 2>&1; then
  # **통과했다는 것만으로는 부족하다.** 방아쇠 감지가 통째로 안 걸려도 시험을 아예
  # 안 부르고 그냥 통과하는데, 그 모양도 여기서는 "통과"로 보인다 — 「하나도 안 잰 것」과
  # 「스테이징된 판을 재서 통과한 것」을 안내 줄로 가른다.
  LC_ALL=C grep -q "push 관문을 고쳤습니다" "$META_OUT" \
    && ok "자기수정 시험이 스테이징된 판을 잰다 (디스크의 깨진 판이 아니라)" \
    || ng "통과는 했지만 자기수정 시험이 아예 안 불린 것으로 보인다 — 아무것도 안 잰 것과 같다: $(cat "$META_OUT")"
else
  ng "디스크의 깨진 판을 잰 것으로 보인다 — 스테이징된 판을 재야 한다: $(cat "$META_OUT")"
fi
cd / || exit 1
rm -rf "$META"

# ── 15.5. `pre-commit` 만 고치는 커밋에서도 이 시험이 돈다 ───────────────
#    15번은 `pre-push` 를 고치는 커밋을 내므로, **`pre-commit` 이 방아쇠 목록에서
#    빠지는 퇴행은 15번이 못 잡는다** — 2026-09-06 에 더한 그 한 줄을 지키는 자리가
#    여기다. 그 줄이 없으면 호출부를 고치는 커밋이 다시 조용해진다.
meta_setup
echo "# 시험용 표시" >>.githooks/pre-commit
git add .githooks/pre-commit
if git commit -qm "호출부만 손댐" >"$META_OUT" 2>&1; then
  LC_ALL=C grep -q "push 관문을 고쳤습니다" "$META_OUT" \
    && ok "pre-commit 만 고치는 커밋에서도 push 관문 시험이 돈다" \
    || ng "pre-commit 이 방아쇠에서 빠졌다 — 호출부를 고쳐도 시험이 안 돈다: $(cat "$META_OUT")"
else
  ng "pre-commit 만 고쳤는데 커밋이 막혔다: $(cat "$META_OUT")"
fi
cd / || exit 1
rm -rf "$META"

# ── 16. push 관문 시험 파일을 지우면서 관문을 고치면 막는다 ──────────────
#    `git rm .githooks/test-push-gate.sh` 한 커밋은 그 파일이 사라지므로
#    `ls-files --error-unmatch` 가 실패해 방아쇠가 조용히 꺼질 수 있다 — 관문을
#    없애는 커밋만 검사를 안 받는 사각지대다 (`pre-commit` 의 `gate_gone`).
meta_setup
git rm -q .githooks/test-push-gate.sh
echo "# 시험용 표시" >>.githooks/pre-push
git add .githooks/pre-push
if git commit -qm "시험 파일째 지움" >"$META_OUT" 2>&1; then
  ng "push 관문 시험 파일을 지웠는데 통과했다"
else
  ok "push 관문 시험 파일을 지우면서 고치면 막는다"
  LC_ALL=C grep -q "test-push-gate.sh 가 없습니다" "$META_OUT" || ng "막으면서 이유를 안 밝힌다"
fi
cd / || exit 1
rm -rf "$META"

echo ""
echo "통과 $pass · 실패 $fail"
[ "$fail" = 0 ] || exit 1
