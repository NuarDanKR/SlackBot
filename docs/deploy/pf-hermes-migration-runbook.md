# pf-hermes 이관 runbook — 2026-09-23 오전

> 근거: [`pf-hermes-owner-plan.md`](../design/pf-hermes-owner-plan.md) §7·§9·§12,
> [`pf-hermes-migration-handoff.md`](../design/pf-hermes-migration-handoff.md) §5
> 대상: 인프라 소유자(우리). 프금팀 코드 변경은 인계서가 맡는다.

## 0. 이 문서를 쓰는 법

**12:00 의 산출물은 운영 전환된 서비스가 아니다.** 검증 가능한 shadow release 와
오전 증적이다(§12.3). 실제 전환은 24시간 관찰 뒤 별도 승인·별도 창에서 한다.

각 단계는 **통과 기준**과 **실패 시**를 같이 적었다. 통과 기준을 못 채우면 다음
단계로 가지 않는다 — 다음 단계가 앞 단계의 실패를 덮어 버려서, 나중에 어디서
틀렸는지 못 찾게 된다.

명령은 전부 **서버 기준**이다. `python` 은 PATH 에 없다 — venv 절대경로를 쓴다
(CLAUDE.md 「서버 명령은 가상환경 기준으로 적는다」).

증적은 한 파일에 모은다. 나중에 GCP 와 대조할 유일한 근거다.

```bash
sudo install -d -m 0750 -o inframan /var/log/tybot-migration
EVID=/var/log/tybot-migration/2026-09-23.log
exec > >(tee -a "$EVID") 2>&1        # 이 세션의 출력을 전부 남긴다
date '+%F %T  이관 세션 시작'
```

---

## 08:00~08:20 · go/no-go

**하나라도 없으면 당일 목표를 「설치 검증만」 으로 낮춘다**(§12.1).

| # | 확인할 것 | 없으면 |
|---|---|---|
| 1 | 프금팀 고정 tag/commit 과 `package-lock.json` | 이관 중단 |
| 2 | `pf-hermes` 전용 Slack 앱 token | **shadow 를 Slack 에 붙이지 않는다**(아래 주의) |
| 3 | 전용 Anthropic key | 모델 호출 시험 제외 |
| 4 | 코드 Git read-only key · 자료 Git read/write key | 이관 중단 |
| 5 | env/config 변수 목록 (값은 별도 보관) | 이관 중단 |
| 6 | 기존 GCP 의 실행 commit·마지막 event timestamp | 대조 근거 없음 → 중단 |
| 7 | GCP 복귀 권한을 가진 운영자 연락 가능 | 롤백 불가 → 중단 |

> **같은 Slack token 을 두 곳에서 쓰지 않는다.** 전용 앱이 없다면 오전에는
> offline/replay 시험까지만 하고, 실제 Slack 연결은 운영 전환 창에서 기존
> 프로세스를 정지한 뒤 **한 번만** 연다(§12.2 각주).

```bash
date '+%F %T  go/no-go'
# 기존 GCP 쪽 기준값을 먼저 적어 둔다 (나중에 이 값과 대조한다)
echo "GCP 실행 commit: <프금팀 확인>"
echo "GCP 마지막 event: <프금팀 확인>"
```

---

## 08:20~09:00 · 전용 계정·경로·권한

**통과 기준**: `pf-hermes` 계정이 TYBot 의 env·아카이브·DB 에 닿지 못한다.

```bash
sudo /opt/tybot/deploy/setup-pf-hermes-host.sh --dry-run   # 무엇을 만들지 먼저 본다
sudo /opt/tybot/deploy/setup-pf-hermes-host.sh
sudo /opt/tybot/deploy/setup-pf-hermes-host.sh --verify     # 격리 확인만 다시
```

스크립트가 만드는 것과 확인하는 것은 그 파일 머리말에 있다. 핵심은 셋이다.

- 계정·그룹 `pf-hermes`, 경로 넷(`/opt/tybot-subbots`, `/var/lib/...`, `/etc/...`, `/run/...`)
- `/etc/tybot-subbots/pf-hermes.env` 는 `0640 root:pf-hermes` — group/other 읽기 금지
- **격리 검증**: TYBot env·아카이브·DB socket 을 `pf-hermes` 로 읽어 보고 **거부되는지** 본다

**실패 시**: 권한을 고치고 `--verify` 를 다시 돌린다. 통과 전에는 unit 을 만들지 않는다.

---

## 09:00~09:35 · 고정 release 배치

**통과 기준**: lockfile 불변, build/test 통과, 설정 없이 시작하면 fail-closed.

```bash
date '+%F %T  release 배치'
REL=/opt/tybot-subbots/pf-hermes/releases/<commit>
sudo -u pf-hermes git -C "$REL" log -1 --format='%H %ci'   # 고정 commit 확인
sudo -u pf-hermes git -C "$REL" status --porcelain         # 비어 있어야 한다

# 의존성 — lockfile 그대로. `npm install` 은 lockfile 을 고칠 수 있다
sudo -u pf-hermes bash -c "cd $REL && npm ci --omit=dev"
sudo -u pf-hermes bash -c "cd $REL && git diff --exit-code package-lock.json"

# 프금팀 계약 검사 (인계서 §3.6)
sudo -u pf-hermes bash -c "cd $REL && npm run check:offline"
```

**통과 기준 세부**
- `git status` 가 비어 있다 — 서버에서 고친 것이 없다
- `package-lock.json` 이 안 바뀐다
- `check:offline` 이 **TYBot 경로를 가리키는 설정을 실패로 잡는다**

**실패 시**: release 를 폐기한다. 이전 상태를 그대로 둔다. 고쳐서 쓰지 않는다 —
서버에서 고친 release 는 프금팀이 재현할 수 없다.

---

## 09:35~10:10 · 자료 Git

**통과 기준**: GCP 와 archive commit 이 일치하고, 원문 수가 임의로 줄지 않았다.

```bash
date '+%F %T  자료 Git'
ARCH=/var/lib/tybot-subbots/pf-hermes/archive
sudo -u pf-hermes git -C "$ARCH" log -1 --format='%H %ci'
sudo -u pf-hermes git -C "$ARCH" status --porcelain | head
sudo -u pf-hermes git -C "$ARCH" rev-list --count HEAD
find "$ARCH" -name '*.md' | wc -l        # 원문 파일 수 — GCP 값과 대조
```

**실패 시**: Git 문제를 분리하고 **서비스를 기동하지 않는다.** 아카이브가 불완전한
채로 뜨면 그 상태로 답을 하고, 그 답은 「자료가 없다」 로 기록된다.

---

## 10:10~10:40 · env/config 와 systemd preflight

**통과 기준**: 로그에 시크릿이 없고, 쓰기 경로가 state 아래로 제한된다.

```bash
date '+%F %T  preflight'
# 1) 파일 권한
sudo stat -c '%a %U:%G %n' /etc/tybot-subbots/pf-hermes.env   # 640 root:pf-hermes

# 2) 설정 없이 시작하면 죽는가 (fail-closed, 인계서 §5)
sudo -u pf-hermes env -i /usr/bin/node "$REL/src/index.js" 2>&1 | tail -3
#    → 「설정 없음」 으로 즉시 종료해야 한다. 기본값으로 뜨면 안 된다

# 3) unit 문법과 격리 지시문
sudo systemd-analyze verify /etc/systemd/system/pf-hermes.service

# 4) 시크릿이 로그에 새는가 — 값이 아니라 변수명만 나와야 한다
sudo -u pf-hermes bash -c "cd $REL && npm run check:offline" 2>&1 \
  | grep -Ei 'xoxb-|xapp-|sk-ant-|BEGIN [A-Z ]*PRIVATE KEY' && echo "시크릿 노출!" || echo "시크릿 노출 없음"
```

**실패 시**: unit 을 시작하지 않는다.

---

## 10:40~11:10 · shadow 기동 (답변·수집·push 비활성)

**통과 기준**: health 가 2초 안에 나오고, Slack·Anthropic·Git 상태가 구분된다.

```bash
date '+%F %T  shadow 기동'
sudo systemctl start pf-hermes
sleep 5
sudo systemctl status pf-hermes --no-pager | head -12

# health 생성 시간 — Slack·Anthropic 호출 없이 2초 안 (인계서 §9)
time sudo -u pf-hermes cat /var/lib/tybot-subbots/pf-hermes/health.json >/dev/null

# 우리 쪽 계약 검사 — 빠진 칸과 시크릿 혼입을 잡는다
sudo /opt/tybot/.venv/bin/python /opt/tybot/scripts/check_pf_health.py \
  /var/lib/tybot-subbots/pf-hermes/health.json
```

**실패 시**: 즉시 정지하고 로그를 보존한다.

```bash
sudo systemctl stop pf-hermes
sudo journalctl -u pf-hermes --since '-30 min' --no-pager > /var/log/tybot-migration/pf-hermes-fail.log
```

---

## 11:10~11:40 · 영향 점검

**통과 기준**: TYBot 무영향, 같은 token 동시 실행 없음.

```bash
date '+%F %T  영향 점검'
# 1) TYBot 이 멀쩡한가
sudo systemctl is-active tybot tybot-console
sudo journalctl -u tybot --since '-1 hour' -p warning --no-pager | tail -20

# 2) 같은 Slack token 으로 뜬 프로세스가 둘인가
#    GCP 쪽이 아직 돌고 있다면 **전용 앱 token 이 아닌 이상 여기서 멈춘다**
echo "GCP 프로세스 상태: <프금팀 확인>"

# 3) pf-hermes 가 TYBot 을 못 읽는가 (다시 한 번)
sudo /opt/tybot/deploy/setup-pf-hermes-host.sh --verify
```

**실패 시**: shadow 를 종료하고 롤백한다.

---

## 11:40~12:00 · 증적 정리와 변경 동결

```bash
date '+%F %T  변경 동결'
{
  echo "release commit : $(sudo -u pf-hermes git -C "$REL" rev-parse HEAD)"
  echo "archive commit : $(sudo -u pf-hermes git -C "$ARCH" rev-parse HEAD)"
  echo "config hash    : $(sudo sha256sum /etc/tybot-subbots/pf-hermes.env | cut -c1-16)"
  echo "unit hash      : $(sha256sum /etc/systemd/system/pf-hermes.service | cut -c1-16)"
} | tee -a "$EVID"
```

**이 시점부터 13시 시험까지 아무것도 바꾸지 않는다.** 바꾸면 13시에 시험한 대상이
오전에 검증한 대상과 달라지고, 그때 통과/실패의 의미가 사라진다.

---

## 12:00~13:00 · 관찰만

```bash
# 예기치 않은 재시작·쓰기가 없는지
sudo systemctl show pf-hermes -p NRestarts
sudo find /var/lib/tybot-subbots/pf-hermes -newermt '-1 hour' -type f | head
```

재시작이 늘었거나 예상 밖 쓰기가 있으면 **13시 시험을 취소한다.**

---

## 롤백 — 언제든

원문 삭제도 force push 도 필요 없다(§10).

```bash
sudo systemctl stop pf-hermes
sudo systemctl disable pf-hermes
# 자료 Git 은 건드리지 않는다. 우리가 push 한 것이 없으므로 되돌릴 것도 없다.
sudo -u pf-hermes git -C "$ARCH" status --porcelain   # 비어 있어야 한다
# GCP 복귀는 프금팀 운영자가 한다 — 07번 연락처
```

---

## `/pf/` 화면

오늘(2026-09-22) nginx 없이 여는 것으로 결정했다. 두 콘솔 모두 평문이므로
**방화벽에서 출발지를 좁혀 둔 상태여야 한다.**

```bash
curl -s http://127.0.0.1:8788/pf/api/health          # {"ok":true}
sudo firewall-cmd --list-all | grep -E '8787|8788'   # 출발지 제한 확인
```

상태 파일이 붙기 전까지 화면은 **「상태 없음」** 으로 보인다. 오류가 아니다.
`health.json` 이 생기면 자동으로 채워진다.

---

## 되돌아볼 것 (17:00 이후)

- 통과 기준을 못 채웠는데 다음 단계로 간 곳이 있나
- 이 문서에 없어서 즉석에서 판단한 것이 있나 — 있으면 여기 적는다
- GCP 대비 수집 건수·비용·응답시간 기준선 (§12.3 16:30~17:00)
