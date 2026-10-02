#!/usr/bin/env bash
#
# Hermes VM 부트스트랩 (Debian 12 기준).
#
#   sudo bash setup.sh                        # 우리 저장소
#   sudo bash setup.sh <코드저장소> <자료저장소>   # 다른 팀
#   sudo bash setup.sh <코드저장소> <자료저장소> --force   # 예약 회차 시각에도 강행
#
# 여러 번 돌려도 안전하다. 중간에 멈추면(예: deploy key 미등록) 그 단계만 안내하고
# 끝나므로, 조치한 뒤 같은 명령을 다시 실행하면 이어서 진행한다.
set -euo pipefail

# ── 인자 ────────────────────────────────────────────────────────────
# `--force` 는 아래 7단계(배포 금지 시각) 관문 **하나만** 끈다. 저장소 주소보다 먼저
# 걷어낸다 — 안 걷어내면 `--force` 가 $1 이 되어 **코드 저장소 주소로 읽힌다.**
FORCE=0
_args=()
for _a in "$@"; do
  case "$_a" in
    --force) FORCE=1 ;;
    *)       _args+=("$_a") ;;
  esac
done
set -- ${_args[@]+"${_args[@]}"}
unset _args

# 저장소가 둘이다. 코드는 팀끼리 공유하고, 자료는 팀마다 따로다.
#
# **기본값은 원저자 저장소다.** 다른 팀이 인자 없이 돌리면 남의 private 저장소를 읽으려다
# 실패하는데, 그 실패는 「키가 없다」로만 보인다 — 「주소가 틀렸다」고는 아무도 말해주지
# 않는다. 그래서 아래 6단계가 **기본값을 쓰는 중인지**를 함께 적는다 (2026-08-31).
CODE_REPO="${1:-git@github.com:wkimclementia/hermes.git}"
DATA_REPO="${2:-git@github.com:wkimclementia/hermes-archive-tec.git}"
# 재실행 안내에 그대로 되돌려 주려고 남긴다. 인자 없이 돌았으면 빈 문자열이고,
# 그때는 안내도 인자 없는 모양이 되어야 **지금 무엇으로 돌고 있는지**와 어긋나지 않는다.
# **`--force` 는 일부러 안 싣는다** — 재실행 안내가 우회를 함께 물고 다니면, 며칠 뒤
# 그 안내를 복사한 사람이 자기도 모르게 시각 관문을 끈 채로 돌린다.
GIVEN_ARGS="${1:+ $1}${2:+ $2}"
USING_DEFAULTS=0
[ $# -ge 2 ] || USING_DEFAULTS=1
SVC_USER="hermes"
HOME_DIR="/opt/hermes"
APP="$HOME_DIR/code"          # 코드 저장소 — 봇 소스와 배포 파일
DATA="$HOME_DIR/archive"      # 자료 저장소 — config.json·slack-export·documents
# 키도 둘이다. 이유는 6단계 주석 참조.
KEY_CODE="$HOME_DIR/.ssh/id_ed25519_code"
KEY_DATA="$HOME_DIR/.ssh/id_ed25519_data"
HOST_CODE="hermes-code"
HOST_DATA="hermes-data"
NODE_MAJOR=22
TZ_NAME="Asia/Seoul"

# ── 배포 금지 창 (분) ───────────────────────────────────────────────
# 아래 7단계가 쓴다. 예약된 회차 시각 **앞뒤로** 얼마나 넓게 막나.
#
# **앞 10분** — 이 관문을 지나도 8단계(`git pull`·`npm ci`)가 남아 있어 재시작은 그
#   뒤에 떨어진다. **2026-09-03 에 VM 에서 실제로 쟀다**: `npm ci --omit=dev` 가
#   캐시가 거의 빈 상태(최악)에서 **7초**, 캐시가 있으면 **4초**(3회 측정: 7·4·4).
#   e2-micro 메모리(969MB)도 측정 중 여유가 있었고 봇은 계속 살아 있었다.
#   재는 법 — 봇의 `node_modules` 를 안 건드리게 `/tmp` 에 `package.json` ·
#   `package-lock.json` 만 복사해 hermes 유저로 같은 명령을 돌렸다.
#
#   **그래서 10분은 실측의 약 85배다 — 알고 그대로 둔다** (WHK 결정 2026-09-03).
#   좁히면 돌던 회차를 끊을 수 있고, 그러면 `rollback()` 이 안 돌아 자료 저장소가
#   더러운 채 남는다. 넓어서 생기는 손해는 「배포하려면 10분 기다린다」뿐이라
#   값이 아니라 **근거**를 채우는 쪽으로 정했다. 여전히 못 잰 것이 하나 있다 —
#   회차의 **진짜 끝**(커밋 뒤 push·DM 보고까지)은 아래 뒤 15분 주석과 같은 사각이다.
# **뒤 15분** — 회차가 끝나는 데 얼마나 걸리나. 실측은 이렇다:
#     · 자동 반영 — 자료 저장소의 자동 반영 커밋이 예약 시각 **+12초 ~ +145초**
#       (07·17시 회차 58건 전수). 다만 커밋(⑤) 뒤에 push 와 DM 보고(⑥)가 남으므로
#       회차의 끝은 그보다 뒤다 — **얼마나 뒤인지는 못 쟀다.**
#     · 주간 요약 — 예약 16:00, 자료 저장소 대화 로그의 발송 시각이 16:02~16:03 (4건).
#     · 일일 요약 — 예약 17:30, 발송 17:30~17:31.
#     · 여기에 `README.md` 가 적은 과부하 재시도(즉시·1분·3분 + 대체 모델)가 붙으면
#       **최대 4분**이 더 늦어질 수 있다.
#   그 합(약 7분)보다 넉넉하게 잡았다. 넓히면 배포할 수 있는 시간대가 줄고, 좁히면
#   회차를 끊는다 — 끊는 쪽이 훨씬 비싸서 넓은 쪽으로 틀렸다.
DEPLOY_LEAD_MIN=10
DEPLOY_TRAIL_MIN=15

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '   \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '   \033[33m!\033[0m %s\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "sudo 로 실행하세요: sudo bash setup.sh"; exit 1; }

# ── 1. 시각 ────────────────────────────────────────────────────────
# 앱은 config.json 의 timezone(Asia/Seoul)을 명시적으로 쓰므로 VM 시각과 무관하다.
# 그래도 맞춰두면 journalctl 로그를 읽을 때 한국 시각으로 보여 편하다.
say "1/9  시각"
timedatectl set-timezone "$TZ_NAME"
ok "$(timedatectl show -p Timezone --value) · $(date '+%Y-%m-%d %H:%M')"

# ── 2. 스왑 ────────────────────────────────────────────────────────
# e2-micro 는 메모리 1GB. 평소엔 남지만 npm ci 가 순간적으로 크다.
say "2/9  스왑"
if swapon --show | grep -q '/swapfile'; then
  ok "이미 있음"
else
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap -q /swapfile
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  ok "2GB 생성"
fi

# ── 3. 패키지 ──────────────────────────────────────────────────────
#
# **python3 는 봇의 준비물이다 — 「엑셀 변환용」이 아니다.**
# 봇 자체(`src/index.js`)는 파이썬을 안 부르므로 기동도 되고 `systemctl status` 도
# active 다. 처음 걸리는 자리는 **매일 07:00·17:00 자동 반영**이고, 증상은 에러가 아니라
# 「아카이브가 안 늘어난다」다. `src/ingest/util.js` 의 `python()` 이 `python3`·`python`
# 을 둘 다 못 찾으면 던지고, 그 함수를 자동 반영(`ingest/slack-archive.js`)·백필
# (`ingest/backfill.js`)·파생값 재계산(`ingest/derive.js`)·포맷 관문(`ingest/verify.js`)·
# 위생 점검(`src/archive-health.js`)이 부른다. 설치 문서의 5단계(백필)가 이미 여기 걸린다.
#
# 데비안 이미지에 python3 가 들어 있어 지금까지 굴러왔을 수 있지만, **이미지가 주는 것과
# 설치 스크립트가 보장하는 것은 다르다** — 이미지가 바뀌면 아무 신호 없이 위 증상이 난다.
#
# **`openpyxl` 은 일부러 안 깐다.** VM 이 실제로 부르는 파이썬 스크립트는 **넷**이다
# (insert_messages · sync_index · verify_archive · verify_format — 소비처는 `ingest/*.js` 와
# `archive-health.js`). fetch_slack_files · insert_entry 는 `SKILL_SCRIPTS` 에 경로만
# 선언돼 있고 부르는 자동 경로가 0곳인 **사람이 부르는 doc-archive 스킬 전용**이다
# (2026-09-22 전수 확인 — 옛 주석은 여섯 다 「VM 이 부르는 것」으로 적고 있었다).
# 어느 쪽이든 표준 라이브러리만 쓴다 — openpyxl 을 import 하는 것은 엑셀 변환
# (`xlsx_to_blocks.py`)뿐이고
# 그것은 사람이 노트북에서 `doc-archive` 로 돌린다. 게다가 데비안 12 는 시스템 파이썬에
# `pip install` 을 막으므로(PEP 668) 여기 넣으면 apt 의 `python3-openpyxl` 이나 venv 를
# 끌고 들어와야 한다 — VM 이 안 쓰는 것을 위해 그럴 값어치가 없다. 노트북 쪽 준비물은
# `npm run doctor` 가 따로 본다. 이 판단을 받치는 시험은 `deploy/test-setup-packages.sh` 다.
say "3/9  기본 패키지"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git curl ca-certificates python3 >/dev/null
ok "git $(git --version | awk '{print $3}')"
# `ok "$(python3 --version)"` 로 끝내면 안 된다 — 명령 치환이 실패해도 `ok` 자신은 0 이라
# set -e 가 안 걸리고 **빈 줄이 초록으로 찍힌다.** 없으면 여기서 멈춘다.
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 를 설치했는데 실행할 수 없습니다 — 자동 반영이 매일 여기서 조용히 멈춥니다." >&2
  exit 1
fi
ok "$(python3 --version 2>&1)"

# ── 4. Node ────────────────────────────────────────────────────────
say "4/9  Node.js"
need_node=1
if command -v node >/dev/null 2>&1; then
  cur=$(node -v | sed 's/^v\([0-9]*\).*/\1/')
  [ "$cur" -ge 20 ] && { need_node=0; ok "이미 설치됨 ($(node -v))"; }
fi
if [ "$need_node" -eq 1 ]; then
  curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - >/dev/null 2>&1
  apt-get install -y -qq nodejs >/dev/null
  ok "설치됨 ($(node -v))"
fi

# ── 5. 서비스 계정 ─────────────────────────────────────────────────
# 로그인 불가 전용 계정. 대외비 아카이브와 토큰을 이 계정만 읽는다.
say "5/9  서비스 계정"
if id "$SVC_USER" >/dev/null 2>&1; then
  ok "이미 있음: $SVC_USER"
else
  useradd --system --create-home --home-dir "$HOME_DIR" --shell /usr/sbin/nologin "$SVC_USER"
  ok "생성: $SVC_USER (홈 $HOME_DIR)"
fi
install -d -m 700 -o "$SVC_USER" -g "$SVC_USER" "$HOME_DIR/.ssh"

# ── 6. deploy key ──────────────────────────────────────────────────
# **키가 둘인 이유**: GitHub 은 같은 deploy key 를 두 저장소에 등록하지 못한다
# ("Key is already in use"). 그래서 저장소마다 키를 따로 만들고, 어느 키를 쓸지는
# 별칭 호스트로 가른다. 별칭이 없으면 ssh 가 github.com 에 아무 키나 내밀어
# 한쪽이 조용히 실패한다.
#
# 일반 Host github.com 항목은 일부러 두지 않는다 — git@github.com:… 주소를 그대로
# 쓰면 키가 안 붙어 **바로 실패한다**. 엉뚱한 키로 붙어 나중에 헤매는 것보다 낫다.
say "6/9  GitHub deploy key"
for k in "$KEY_CODE" "$KEY_DATA"; do
  if [ ! -f "$k" ]; then
    sudo -u "$SVC_USER" ssh-keygen -t ed25519 -N '' -C "hermes-vm-$(basename "$k")" -f "$k" -q
    ok "새 키 생성: $(basename "$k")"
  fi
done

cat > "$HOME_DIR/.ssh/config" <<EOF
Host $HOST_CODE
  HostName github.com
  User git
  IdentityFile $KEY_CODE
  IdentitiesOnly yes
  StrictHostKeyChecking accept-new

Host $HOST_DATA
  HostName github.com
  User git
  IdentityFile $KEY_DATA
  IdentitiesOnly yes
  StrictHostKeyChecking accept-new
EOF
chown "$SVC_USER:$SVC_USER" "$HOME_DIR/.ssh/config"
chmod 600 "$HOME_DIR/.ssh/config"

# git@github.com:owner/repo.git → git@<별칭>:owner/repo.git
to_alias() {
  case "$1" in
    git@github.com:*) printf 'git@%s:%s' "$2" "${1#git@github.com:}" ;;
    *)                printf '%s' "$1" ;;
  esac
}
# 저장소 주소에서 deploy key 등록 화면 주소를 만든다.
keys_url() {
  s="${1#git@github.com:}"; s="${s#https://github.com/}"; s="${s%.git}"
  printf 'https://github.com/%s/settings/keys' "$s"
}
CODE_URL="$(to_alias "$CODE_REPO" "$HOST_CODE")"
DATA_URL="$(to_alias "$DATA_REPO" "$HOST_DATA")"
if [ "$CODE_URL" = "$CODE_REPO" ]; then warn "코드 저장소 주소가 git@github.com: 형식이 아니라 별칭을 못 붙였습니다: $CODE_REPO"; fi
if [ "$DATA_URL" = "$DATA_REPO" ]; then warn "자료 저장소 주소가 git@github.com: 형식이 아니라 별칭을 못 붙였습니다: $DATA_REPO"; fi

missing=""
sudo -u "$SVC_USER" git ls-remote "$CODE_URL" -q >/dev/null 2>&1 || missing="$missing 코드"
sudo -u "$SVC_USER" git ls-remote "$DATA_URL" -q >/dev/null 2>&1 || missing="$missing 자료"

if [ -n "$missing" ]; then
  # **주소가 틀렸을 가능성을 먼저 말한다.** 다른 팀이 여기서 막히는 가장 흔한 이유는
  # 키를 안 넣어서가 아니라 인자를 안 넘겨서다 — 그러면 아래 등록 화면 주소가 자기
  # 저장소가 아니라 원저자 저장소를 가리켜, 열어도 볼 수가 없다.
  if [ "$USING_DEFAULTS" -eq 1 ]; then
    cat <<EOF

   ─────────────────────────────────────────────────────────────
   ⚠ 저장소 주소를 안 넘겼습니다 — **원저자 저장소**를 보고 있습니다.

     코드 : $CODE_REPO
     자료 : $DATA_REPO

   다른 팀이라면 이 주소가 틀린 것입니다. 아래 키 등록 화면도 자기 저장소가
   아니라 원저자 저장소를 가리키므로, 열어도 볼 수 없습니다.

   자기 저장소로 다시 실행하세요:

     sudo bash $0 git@github.com:<소유자>/<코드저장소>.git git@github.com:<소유자>/<자료저장소>.git

   원저자 저장소가 맞다면 (TEC 프로젝트금융 팀) 아래대로 키를 등록하세요.
   ─────────────────────────────────────────────────────────────
EOF
  fi
  cat <<EOF

   ─────────────────────────────────────────────────────────────
   아직 읽을 수 없는 저장소가 있습니다:$missing

   아래 공개키를 **각 저장소에 따로** 등록하세요. 같은 키를 두 저장소에
   등록할 수는 없어서 키가 둘입니다.

   1) 코드 저장소 — $(keys_url "$CODE_REPO")
        "Add deploy key"
        Title       : hermes-vm-code
        Allow write : 체크하지 마세요 (코드는 VM 이 읽기만 합니다)
        Key         :

$(cat "${KEY_CODE}.pub")

   2) 자료 저장소 — $(keys_url "$DATA_REPO")
        "Add deploy key"
        Title       : hermes-vm-data
        Allow write : **체크하세요** (자동 반영이 이 저장소에 커밋·push 합니다)
        Key         :

$(cat "${KEY_DATA}.pub")

   3) 등록한 뒤 **같은 인자로** 다시 실행:

        sudo bash $0$GIVEN_ARGS

      (인자를 빼면 위 기본값으로 돌아가 방금 등록한 키가 소용없어집니다)
   ─────────────────────────────────────────────────────────────
EOF
  exit 0
fi
ok "저장소 둘 다 접근 확인됨"

# ── 7. 배포 금지 시각 ───────────────────────────────────────────────
#
# **돌던 회차 위에 재시작을 떨어뜨리지 않는다** (WHK 결정 2026-09-03).
#
# 9단계의 `systemctl restart hermes` 는 돌던 회차를 끊는다. 자동 반영이 끊기면
# `src/ingest/index.js` 의 `rollback()` 이 **안 돈다** — 그건 예외를 잡아서 되돌리는
# 코드라 프로세스가 통째로 죽으면 실행될 기회 자체가 없다. 그러면 자료 저장소 작업
# 트리가 반쯤 고쳐진 채 남고, 다음 회차의 `syncBeforeWork` 가 거기서 멈춘다.
#
# 운영 규칙(「그 시각엔 배포하지 말자」)으로 두는 길도 있었지만 그건 사람 기억에 맡기는
# 것이고, 이 저장소가 이번에 배운 것이 정확히 「사람 기억에 맡긴 것은 안 돌아온다」다.
#
# ── 시각을 어디서 가져오나 ──
#
# **여기 적지 않는다.** 07:00·17:00·17:30 을 이 파일에 또 쓰면 `config.json` 을 고치는
# 날 갈리고, 갈려도 에러가 안 난다. 원본은 자료 저장소 `config.json` 의 `digest.*.cron`
# 이고(봇의 `src/scheduler.js` 가 그것을 node-cron 에 그대로 넘긴다) 여기서도 그 파일을
# 읽는다. **목록을 적지 않고 찾는다** — `cron` 키를 전부 훑으므로 예약이 늘어도 저절로
# 들어온다. `enabled` 가 true 인 것만 세는 것도 scheduler.js 와 같은 규칙이다
# (`if (!spec?.enabled) return;`).
#
# ── 왜 여기인가 (8·9단계 앞) ──
#
# · 돌던 봇을 처음 건드리는 자리가 8단계(`git pull`·`npm ci`)와 9단계(restart)다.
#   9단계 앞에서 막으면 **코드는 받고 재시작은 안 한 상태**로 끝나는데, 그게 바로
#   `scripts/check-deploy-judge.js` 가 잡으려는 모양이다. 아무것도 안 건드린 채 끝내야 한다.
# · 3단계가 python3 를 보장한 뒤라서 `config.json` 을 **진짜 JSON 파서로** 읽을 수 있다.
#   셸에서 grep 으로 흉내 내면 그 흉내가 진짜 파서와 갈리는 날이 온다.
# · 재실행이면 자료 저장소는 지난번 8단계에서 이미 받아져 있다. 없으면 첫 설치이고,
#   첫 설치에는 끊길 회차가 없다.
#
# ── 시간대 ── **여기가 가장 조용히 틀리기 쉬운 자리다.**
#
# **기계 시각을 안 믿는다.** 1단계가 VM 을 Asia/Seoul 로 맞추지만, 이 관문이 거기 기대면
# `timedatectl` 이 실패했거나 사람이 되돌린 기계에서 판정이 통째로 뒤집힌다 — UTC 로 도는
# 기계에서는 07:00 KST 가 22:00 으로 보여, 위험 시각이 안전으로 읽힌다. 그래서 아래
# 파이썬은 `config.json` 의 `timezone` 으로 ZoneInfo 를 만들어 **그 시간대의 지금**을 직접
# 만든다. 봇의 node-cron 이 `{ timezone: config.timezone }` 로 도는 것과 같은 기준이고,
# 이 파일 위쪽의 `TZ_NAME` 과도 일부러 안 엮었다(그건 로그를 읽기 편하게 하는 값이다).
# 그 시간대를 이 기계에서 못 찾으면 **판정 불가로 막는다** — 조용히 UTC 로 물러서는 것이
# 이 관문이 막으려는 「에러 없이 틀리기」 그 자체다.
#
# ── 못 잡는 구간 ──
#
# · 회차가 실제로 몇 분 도는지는 아무도 안 쟀다. 창의 근거는 위 `DEPLOY_*_MIN` 주석의
#   실측뿐이고, 그 실측은 회차의 **끝**이 아니라 커밋·발송 시각이다.
# · 봇이 안 돌고 있으면 그냥 통과시킨다. 예약은 봇 프로세스 안의 node-cron 이라
#   프로세스가 없으면 회차도 없다 — 다만 그 판정은 `systemctl is-active` 한 번이 전부다.
# · 서머타임이 있는 시간대에서는 경계를 넘는 하루의 「다시 오세요」 시각이 한 시간
#   어긋날 수 있다 (분 단위로 절대시각을 더하며 훑기 때문). Asia/Seoul 은 해당 없다.
# · `--force` 는 이 관문만 끈다. 끊긴 회차를 되살려 주지 않는다.
#
# ── 시험 ── `deploy/test-setup-window.sh`

# 안전=0 · 위험=1 · 판정 불가=2. 사람에게 보일 문장은 stdout 으로.
# `HERMES_DEPLOY_NOW` (epoch 초)는 시험이 「지금」을 갈아끼우는 손잡이다. 벽시계 문자열이
# 아니라 절대시각을 받는 이유가 있다 — 기계 시간대를 바꿔도 같은 답이 나와야 하는데,
# 벽시계로 받으면 그 시험 자체가 시간대와 무관해져 아무것도 안 재게 된다.
deploy_window_probe() {
  python3 - "$DATA/config.json" "$DEPLOY_LEAD_MIN" "$DEPLOY_TRAIL_MIN" "${HERMES_DEPLOY_NOW:-}" <<'PY'
import json, sys
from datetime import datetime, timedelta
from functools import lru_cache

# 로케일과 무관하게 한국어를 그대로 찍는다 (윈도우에서 이 관문을 시험할 때 cp949 로 죽는다).
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

cfg_path, lead, trail, now_raw = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]

def undecided(msg):
    print(msg)
    sys.exit(2)

try:
    from zoneinfo import ZoneInfo
except ImportError:
    undecided("이 파이썬에 zoneinfo 가 없어 예약 시각을 판정할 수 없습니다 (python 3.9 이상 필요).")

try:
    with open(cfg_path, encoding="utf-8") as fh:
        cfg = json.load(fh)
except Exception as e:
    undecided("config.json 을 읽지 못했습니다: %s" % e)

tz_name = cfg.get("timezone")
if not tz_name:
    undecided("config.json 에 timezone 이 없습니다 — 봇의 예약도 이 값으로 도는데 그것을 못 읽었습니다.")
try:
    tz = ZoneInfo(tz_name)
except Exception as e:
    undecided("시간대 '%s' 를 이 기계에서 찾지 못했습니다 (%s).\n"
              "   기계 시각으로 대신 판정하지 않습니다 — 그게 조용히 틀리는 길입니다." % (tz_name, e))

# 예약을 **찾는다**. 목록을 여기 적지 않는다.
jobs = []
def walk(node, path):
    if not isinstance(node, dict):
        return
    if isinstance(node.get("cron"), str) and node.get("enabled") is True:
        jobs.append((".".join(path), node["cron"]))
    for k, v in node.items():
        walk(v, path + [k])
walk(cfg, [])

if not jobs:
    undecided("config.json 에서 켜져 있는 예약(cron)을 하나도 못 찾았습니다 —\n"
              "   「예약이 없다」가 아니라 「못 읽었다」로 봅니다.")

@lru_cache(maxsize=None)
def field(spec, lo, hi):
    out = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            x, y = part.split("-", 1)
            a, b = int(x), int(y)
        else:
            a = b = int(part)
        out.update(range(a, b + 1, step))
    return frozenset(out)

def matches(spec, t):
    f = spec.split()
    if len(f) != 5:
        raise ValueError("cron 필드가 5개가 아닙니다: %r" % spec)
    mi, ho, dom, mo, dow = f
    if t.minute not in field(mi, 0, 59): return False
    if t.hour   not in field(ho, 0, 23): return False
    if t.month  not in field(mo, 1, 12): return False
    dok = dom == "*" or t.day in field(dom, 1, 31)
    w = (t.weekday() + 1) % 7          # 파이썬 월=0 → cron 일=0
    wf = field(dow, 0, 7)
    wok = dow == "*" or w in wf or (w == 0 and 7 in wf)
    # cron 규칙: 일(dom)과 요일(dow)이 둘 다 * 가 아니면 **둘 중 하나만 맞아도** 돈다.
    if dom != "*" and dow != "*":
        return dok or wok
    return dok and wok

now = datetime.fromtimestamp(float(now_raw), tz) if now_raw else datetime.now(tz)
now = now.replace(second=0, microsecond=0)

# 앞으로 하루치 발화 시각을 한 번에 뽑아 둔다 (분마다 다시 세면 e2-micro 에서 느리다).
try:
    fires = []
    t = now - timedelta(minutes=trail)
    end = now + timedelta(minutes=60 * 24 + lead)
    while t <= end:
        for name, spec in jobs:
            if matches(spec, t):
                fires.append((t, name, spec))
        t += timedelta(minutes=1)
except Exception as e:
    undecided("cron 식을 읽지 못했습니다: %s" % e)

def conflicts(at):
    lo, hi = at - timedelta(minutes=trail), at + timedelta(minutes=lead)
    seen, out = set(), []
    for ft, name, spec in fires:
        if lo <= ft <= hi and name not in seen:
            seen.add(name)
            out.append((name, spec))
    return out

stamp = now.strftime("%Y-%m-%d %H:%M")
hits = conflicts(now)
if not hits:
    print("배포해도 되는 시각입니다 — %s (%s) · 켜진 예약 %d건과 안 겹칩니다"
          % (stamp, tz_name, len(jobs)))
    sys.exit(0)

nxt = None
for i in range(1, 60 * 24 + 1):
    cand = now + timedelta(minutes=i)
    if not conflicts(cand):
        nxt = cand
        break

print("지금은 %s (%s) 이고, 예약된 회차와 겹칩니다 (앞 %d분 · 뒤 %d분):" % (stamp, tz_name, lead, trail))
for name, spec in hits:
    print('     %-16s "%s"   ← config.json' % (name, spec))
print("")
if nxt:
    print("   다시 오세요:  %s (%s) 이후" % (nxt.strftime("%m-%d %H:%M"), tz_name))
else:
    print("   앞으로 24시간 안에 안 겹치는 자리가 없습니다 — 예약이 너무 촘촘합니다.")
sys.exit(1)
PY
}

say "7/9  배포 금지 시각"
if [ "$FORCE" -eq 1 ]; then
  warn "--force — 시각 관문을 건너뜁니다. 돌던 회차가 끊길 수 있습니다"
elif [ ! -f "$DATA/config.json" ]; then
  ok "자료 저장소가 아직 없습니다 (첫 설치) — 끊길 회차가 없습니다"
elif ! systemctl is-active --quiet hermes; then
  ok "봇이 지금 안 돌고 있습니다 — 끊길 회차가 없습니다"
else
  probe_rc=0
  probe_out=$(deploy_window_probe) || probe_rc=$?
  if [ "$probe_rc" -eq 0 ]; then
    ok "$probe_out"
  else
    # **거부는 실패로 낸다 — 다만 고장과는 다른 번호로.**
    #   0 으로 끝내면 부르는 사람도 스크립트도 「배포했다」로 읽는다. 이 저장소가 방금
    #   배운 것이 그 거짓말이다(받아 놓고 재시작 안 한 채 「완료」).
    #   1 은 진짜 고장(자료 저장소 미준비 등)이 이미 쓰고 있어 섞이면 못 가른다.
    #   그래서 **3 = 「일부러 안 했다」** 로 따로 둔다.
    {
      echo ""
      echo "   ─────────────────────────────────────────────────────────────"
      echo "   ⚠ 지금은 배포하면 안 되는 시각입니다 — 아무것도 건드리지 않고 멈춥니다."
      echo ""
      printf '%s\n' "$probe_out" | sed 's/^/   /'
      echo ""
      echo "   왜 막나: 9단계의 systemctl restart 는 돌던 회차를 끊습니다. 자동 반영이"
      echo "   끊기면 되돌리기(ingest/index.js 의 rollback)가 **안 돕니다** — 프로세스가"
      echo "   통째로 죽으면 그 코드가 돌 기회가 없습니다. 그러면 자료 저장소 작업 트리가"
      echo "   반쯤 고쳐진 채 남고 다음 회차가 git pull 에서 멈춥니다."
      echo ""
      echo "   지금 꼭 해야 하면 (위 대가를 받아들이고):"
      echo ""
      echo "     sudo bash $0$GIVEN_ARGS --force"
      echo ""
      echo "   코드는 아직 안 받았고 봇도 안 건드렸습니다 — 지금 그대로 계속 돕니다."
      echo "   ─────────────────────────────────────────────────────────────"
    } >&2
    exit 3
  fi
fi

# ── 8. 코드·자료·의존성·환경변수 ────────────────────────────────────
say "8/9  코드와 자료, 의존성"
clone_or_pull() {   # $1=주소  $2=폴더  $3=이름
  if [ -d "$2/.git" ]; then
    # 원격이 비어 있을 때 clone 하면 로컬은 커밋 없는 채로(unborn HEAD) git 기본
    # 브랜치 이름(흔히 master)을 갖는다. 그 뒤 팀이 처음 push 를 다른 이름(예: main)으로
    # 하면 이 pull 은 "그런 ref 를 못 받았다"는 git 원문 에러로 set -e 아래서 그냥 죽는다 —
    # 재실행하라는 안내를 그대로 따라도 매번 같은 자리에서 다시 죽는다. 원인을 진단해서
    # 알려준다. 실패를 삼키지 않는다 — 진단하든 못 하든 종료코드는 그대로 전달한다.
    # set -e 아래서 `x=$(cmd)` 는 cmd 가 실패하면 그 줄에서 바로 스크립트를 죽인다
    # (대입 자체의 종료코드가 cmd 의 것이라 `$?` 를 보는 다음 줄까지 못 간다) — 그래서
    # 대입을 `||` 의 왼쪽에 두어 실패를 여기서 받아낸다.
    pull_rc=0
    pull_err=$(sudo -u "$SVC_USER" git -C "$2" pull --ff-only --quiet 2>&1 >/dev/null) || pull_rc=$?
    if [ "$pull_rc" != 0 ]; then
      diagnose_pull_failure "$2" "$3" "$pull_err"
      exit "$pull_rc"
    fi
    ok "$3 갱신 (git pull)"
  else
    sudo -u "$SVC_USER" git clone --quiet "$1" "$2"
    ok "$3 복제 완료"
  fi
}

# $1=폴더  $2=이름  $3=git pull 이 낸 원문 에러(stderr)
#
# 이 진단은 딱 한 가지 상황만 짚는다 — **로컬에 커밋이 하나도 없는데(clone 당시
# 원격이 비어 있었다는 뜻) "그런 ref 를 못 받았다" 류로 pull 이 죽는 경우**다. 그 둘이
# 같이 있어야 "브랜치 이름이 다르다"고 확신할 수 있고, 하나만으로는 다른 원인(네트워크·
# 권한)을 오진단할 수 있다. 조건이 안 맞으면 원문을 그대로 보여준다 — 원인을 못 찾은
# 실패에 잘못된 안내를 붙이는 것보다 원문 그대로가 낫다.
diagnose_pull_failure() {
  if sudo -u "$SVC_USER" git -C "$1" rev-parse --verify -q HEAD >/dev/null 2>&1; then
    echo "$3" >&2
    return
  fi
  case "$3" in
    *"no such ref was fetched"*) : ;;
    *) echo "$3" >&2; return ;;
  esac
  # 같은 이유로 실패를 `||` 로 받는다 — symbolic-ref 가 실패해도 여기서 죽지 않고
  # branch_name 이 빈 채로 남아 아래 ${branch_name:-...} 대체가 실제로 쓰인다.
  branch_name=$(sudo -u "$SVC_USER" git -C "$1" symbolic-ref --short HEAD 2>/dev/null) || branch_name=""
  warn "$2 저장소가 처음 clone 될 때 비어 있었고, 그 뒤 다른 브랜치 이름으로 채워진 것 같습니다."
  {
    echo "   로컬 브랜치: ${branch_name:-알 수 없음} — 원격에 같은 이름의 브랜치가 없습니다"
    echo "   (원격이 비어 있을 때 clone 하면 git 기본 이름으로 남는데, 팀이 첫 push 를"
    echo "    다른 이름(예: main)으로 했으면 이렇게 어긋납니다)."
    echo "   로컬엔 아직 커밋이 없으니 지우고 새로 받으면 됩니다:"
    echo "     sudo rm -rf $1"
    echo "     sudo bash $0$GIVEN_ARGS"
  } >&2
}

# 빈 자료 저장소를 받았나. `git clone <빈 저장소>` 는 종료코드 0 에 경고 한 줄만 내므로
# 위 clone_or_pull 은 성공으로 읽는다 — 그러면 설치가 끝까지 가고 봇 기동에서야 걸린다.
#
# **둘 다 본다.** 2026-09-01 에 실제로 돌려 보니 `config.json` 만 있고 아카이브 틀이
# 없어도 통과했다 — 바로 위 줄이 막겠다고 적은 그 상황이다. 설정만 손으로 복사하고
# `npm run init-archive` 를 안 돌린 저장소가 정확히 그 모양이라, 흔한 쪽을 못 막고 있었다.
#
# `slack-export/index.md` 를 틀의 대표로 삼는다 — init-archive 가 처음 놓는 파일이고
# `assertArchive()` 가 봇 기동에서 찾는 파일도 이것이다.
#
# ── 못 잡는 구간 ── `archivePath` 를 `slack-export` 아닌 값으로 바꾼 팀은 틀이 제대로
# 있어도 여기서 막힌다. 이 함수는 설치 전이라 config.json 을 파싱하지 않는다 — 셸에
# JSON 파서가 없고, 여기서 grep 으로 흉내 내면 그 흉내가 진짜 파서와 갈리는 날이 온다.
# `archivePath` 를 바꾸는 팀은 이 줄도 함께 고쳐야 한다. 빠져나가는 손잡이는 없다.
check_data_ready() {   # $1=자료 저장소 폴더
  [ -f "$1/config.json" ] && [ -f "$1/slack-export/index.md" ] && return 0
  warn "자료 저장소가 아직 준비되지 않았습니다: $1"
  {
    [ -f "$1/config.json" ] \
      && echo "   config.json 은 있는데 아카이브 틀(slack-export/index.md)이 없습니다." \
      || echo "   config.json 이 없습니다."
    echo "   자료 저장소에는 config.json 과 아카이브 틀이 **둘 다** 들어가 있어야 합니다."
    echo "   노트북(코드 저장소)에서:"
    echo "     npm run init-archive -- <자료저장소를 clone 한 폴더>"
    echo "     cp config.example.json <그 폴더>/config.json      # 자기 값으로 채웁니다"
    echo "     (그 폴더에서) git add . && git commit && git push"
    echo "   그다음 이 스크립트를 같은 인자로 다시 실행하세요."
  } >&2
  return 1
}

clone_or_pull "$CODE_URL" "$APP"  "코드"
clone_or_pull "$DATA_URL" "$DATA" "자료"
check_data_ready "$DATA" || exit 1

sudo -u "$SVC_USER" bash -c "cd '$APP' && npm ci --omit=dev --silent"
ok "의존성 설치 완료"

env_ready=1
if [ ! -f "$APP/.env" ]; then
  sudo -u "$SVC_USER" cp "$APP/.env.example" "$APP/.env"
  env_ready=0
fi
chmod 600 "$APP/.env"
chown "$SVC_USER:$SVC_USER" "$APP/.env"
# 예시값(`xoxb-` 접두사만)이 그대로면 아직 채워지지 않은 것으로 본다.
if ! grep -qE '^SLACK_BOT_TOKEN=xoxb-.{10,}' "$APP/.env"; then env_ready=0; fi

# 자료 저장소가 어디인지는 .env 로만 알려줄 수 있다 — config.json 이 그 저장소 **안에**
# 있어서 자기 위치를 알려줄 수 없다(순환). src/config.js 의 DATA_ROOT 가 이 값을 읽는다.
if grep -q '^HERMES_DATA_ROOT=' "$APP/.env"; then
  sed -i "s#^HERMES_DATA_ROOT=.*#HERMES_DATA_ROOT=$DATA#" "$APP/.env"
else
  printf '\n# 자료 저장소 뿌리 (setup.sh 가 적었습니다)\nHERMES_DATA_ROOT=%s\n' "$DATA" >> "$APP/.env"
fi
ok "HERMES_DATA_ROOT=$DATA"

# ── 9. systemd ─────────────────────────────────────────────────────
say "9/9  systemd 등록"
install -m 644 "$APP/deploy/hermes.service"              /etc/systemd/system/
install -m 644 "$APP/deploy/hermes-archive-pull.service" /etc/systemd/system/
install -m 644 "$APP/deploy/hermes-archive-pull.timer"   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now hermes-archive-pull.timer
ok "아카이브 동기화 타이머 (15분 간격)"

if [ "$env_ready" -eq 0 ]; then
  cat <<EOF

   ─────────────────────────────────────────────────────────────
   토큰이 아직 비어 있어 봇은 시작하지 않았습니다.

   1) 편집:  sudo nano $APP/.env
        SLACK_BOT_TOKEN=xoxb-...
        SLACK_APP_TOKEN=xapp-...
        ANTHROPIC_API_KEY=sk-ant-...
      (= 뒤의 예시값을 지우고 붙여넣으세요. 따옴표·공백 없이)
      HERMES_DATA_ROOT 는 이미 적혀 있습니다 — 지우지 마세요.

   2) 점검:  sudo -u $SVC_USER bash -c "cd $APP && npm run check"
   3) 시작:  sudo systemctl enable --now hermes

   ※ 노트북에서 돌던 봇은 반드시 먼저 중지하세요.
      두 개가 같은 토큰으로 붙으면 질문마다 두 번 답하고 요약도 두 번 나갑니다.
   ─────────────────────────────────────────────────────────────
EOF
  exit 0
fi

# **`enable --now` 만으로는 재실행이 새 코드를 못 붙인다.** `--now` 가 부르는 것은 start 라,
# 이미 active 인 유닛에는 아무 일도 안 한다. 그런데 이 스크립트는 8단계의 `clone_or_pull`
# 에서 코드를 **실제로 갱신한다** — 그래서 재실행하면 새 코드를 받아 놓고 옛 코드가 계속
# 도는 채로 「완료」를 찍는다. `git log` 에는 새 커밋이 보이고 pull 도 성공해서 에러도
# 경고도 안 난다. `check-deploy-judge.js` 가 잡으려는 「받았는데 재시작 안 함」을
# setup.sh 자신이 만들던 자리다. restart 는 멎어 있으면 start 와 같아서 첫 설치에도 맞다.
#
# **대가**: 돌던 회차가 끊길 수 있다. 자동 반영(07:00·17:00)이나 요약(17:30) 도중에
# 재실행하면 그 회차가 중간에 죽는다. 반영은 관문 실패 때 되돌리는 길이 있지만
# (`ingest/index.js` 의 `rollback()`), 프로세스가 통째로 죽으면 그 되돌림도 안 돈다 —
# 작업 트리가 더러운 채 남고 다음 회차가 `git pull` 에서 멈춘다. 배포는 그 시각을 피한다.
systemctl enable hermes >/dev/null 2>&1
restart_rc=0
systemctl restart hermes || restart_rc=$?
sleep 3
if [ "$restart_rc" -eq 0 ]; then
  say "완료"
else
  say "완료 — 다만 봇 재시작이 실패했습니다 (종료코드 $restart_rc)"
  warn "아래 로그 명령으로 사유를 보세요. 대개 .env 토큰입니다"
fi
# **`|| true` 가 없으면 여기서 스크립트가 죽는다.** `systemctl status` 는 유닛이 active
# 가 아니면 종료코드 3 을 내고, `set -euo pipefail` 아래서 그 3 이 파이프라인 값이 되어
# (`head` 는 0 이지만 pipefail 은 왼쪽 값을 살린다) 아래 안내가 **한 줄도 안 나온다** —
# 봇이 안 떴을 때 사람에게 가장 필요한 네 줄이 바로 그때 사라진다. 값을 삼키는 것이
# 아니다: 진짜 판정은 위 `restart_rc` 가 들고 있고 마지막 줄에서 종료코드로 나간다.
systemctl --no-pager --lines=0 status hermes | head -5 || true
echo
echo "   로그 보기 :  journalctl -u hermes -f"
echo "   재시작    :  sudo systemctl restart hermes"
echo "   점검      :  sudo -u $SVC_USER bash -c 'cd $APP && npm run check'"
echo "   코드 배포 :  sudo -u $SVC_USER git -C $APP pull && sudo systemctl restart hermes"
# 안내를 다 보인 **뒤에** 진실을 종료코드로 낸다. 재시작이 실패했는데 0 으로 끝나면
# 「완료」가 거짓말이 된다.
exit "$restart_rc"
