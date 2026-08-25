# 서버 배치 런북 — Rocky Linux 8

_TYBot (패키지 `tybot`, Slack 핸들 `@tybot`) · 파일럿 1개 워크스페이스_

이 단계에서는 **DB를 설치하지 않는다.** 아카이브는 MD 파일이고, PostgreSQL/MariaDB 인덱서는
파일럿 검증 후 다음 단계다([db-and-acl.md](../design/db-and-acl.md) 참조).

---

## 0. 사전 확인 · 시스템 준비 (새 VM 기준)

### 0-1. 확인
```bash
cat /etc/rocky-release
curl -sI https://slack.com | head -1     # HTTP/2 200 이어야 함 (아웃바운드 443)
curl -sI https://pypi.org  | head -1     # 안 되면 1절 오프라인 경로
```
인바운드 포트는 **0개** 필요하다(Socket Mode 아웃바운드 전용). 프록시 환경이면 [7절](#7-트러블슈팅) 참조.

### 0-2. 필요한 시스템 패키지 — 이것뿐이다
```bash
sudo dnf install -y python3.11 python3.11-pip git rsync
```

| 패키지 | 왜 |
|---|---|
| `python3.11` | RL8 기본 `python3` 은 3.6. 우리 코드는 3.11+ |
| `python3.11-pip` | venv 안에서 의존성 설치 |
| `git` | 저장소 clone / 업데이트 |
| `rsync` | `install.sh` 가 코드를 `/opt/tybot` 으로 동기화 |

**필요 없는 것**: Node.js(순수 Python 스택), gcc·python3.11-devel(`pydantic_core`·`jiter` 는 리눅스 휠 제공 → 컴파일 없음),
DB(이 단계는 MD 파일만), nginx·httpd(인바운드 없음).

### 0-3. 시간대 (필수)
요약 기간 계산이 **서버 로컬 날짜**를 쓴다. KST 가 아니면 "오늘/이번주" 경계가 틀어진다.
```bash
sudo timedatectl set-timezone Asia/Seoul
timedatectl | head -3
```

### 0-4. (권장) 보안 기본값
```bash
sudo dnf update -y            # 새 VM 이면 한 번
sestatus | head -1            # enforcing 유지 — 끄지 않는다
sudo firewall-cmd --state     # 인바운드는 열 필요 없음
```

---

## 1. 코드 서버로 옮기기 — 둘 중 하나

### A. 사내 Git 저장소 경유 (권장 — 이후 업데이트가 `git pull` 한 줄)
```bash
# 로컬 PC에서: 사내 GitLab/GitHub 비공개 저장소 생성 후
git remote add origin <저장소 URL>
git add -A && git commit -m "chore: TYBot 파일럿 서버 배치 준비"
git push -u origin master
```
```bash
# 서버에서
sudo dnf install -y git
sudo git clone <저장소 URL> /tmp/tybot-src
```
> `.env` 와 `archive/` 는 `.gitignore` 로 빠진다 — **시크릿과 원문은 저장소에 안 올라간다.**

### B. 압축 전송 (Git 저장소가 아직 없을 때)
```bash
# 로컬 PC (Git Bash)에서
# 주의: --exclude=archive 로 쓰면 src/tybot/archive/ 까지 빠진다. './archive' 로 루트 고정.
tar --exclude=./.git --exclude=./.venv --exclude=./.env --exclude=./archive \
    --exclude=__pycache__ --exclude=.pytest_cache --exclude='*.egg-info' \
    -czf tybot.tar.gz -C /d/200_TYDEV/SlackBot .

scp tybot.tar.gz <계정>@<서버>:/tmp/
```
```bash
# 서버에서
mkdir -p /tmp/tybot-src && tar -xzf /tmp/tybot.tar.gz -C /tmp/tybot-src
```

### 인터넷 없는 서버라면 (추가 단계)
```bash
# 인터넷 되는 PC에서 (리눅스용 휠을 받는다)
bash deploy/wheelhouse.sh          # → wheels/ 생성
# wheels/ 를 위 tar 에 포함하거나 별도로 scp
```

---

## 2. 설치 (한 줄)

```bash
cd /tmp/tybot-src
sudo bash deploy/install.sh              # 온라인
# 또는
sudo OFFLINE=1 bash deploy/install.sh    # wheels/ 사용
```

스크립트가 하는 일 (멱등 — 재실행 안전):

| 단계 | 내용 |
|---|---|
| 1 | `python3.11` 설치 |
| 2 | `tybot` 시스템 계정(로그인 불가) + `/opt/tybot`, `/etc/tybot`, `/var/lib/tybot/{archive,cache}` |
| 3 | 코드를 `/opt/tybot` 으로 동기화 (`.env`·`archive`·`.git` 제외) |
| 4 | `/opt/tybot/.venv` 생성 + `requirements.txt` 고정 버전 설치 |
| 5 | 코드 소유권 `root:tybot`, 봇은 **읽기·실행만** (자기 코드 수정 불가) |
| 6 | `/etc/tybot/tybot.env` 생성(0640) + `tybot.service` 등록 |

### 경로 표준 — 왜 이렇게 나누나
| 용도 | 경로 | 이유 |
|---|---|---|
| 코드 | `/opt/tybot` | 재배포로 갈아엎히는 영역 |
| 시크릿 | `/etc/tybot/tybot.env` | 설정은 `/etc`. `0640 root:tybot`, 저장소와 물리 분리 |
| | | 앱이 python-dotenv 로 직접 읽는다(유닛의 `EnvironmentFile=` 미사용) |
| **원문 아카이브** | `/var/lib/tybot/archive` | **코드와 분리** — `git pull`·재클론이 원문을 건드리면 안 된다 |
| 로그 | journald | 로테이션 자동 |

---

## 3. 시크릿 입력 (사람이 직접)

```bash
sudo vi /etc/tybot/tybot.env
```
로컬 `.env` 에서 그대로 옮긴다 — **Slack 앱 재설치·토큰 재발급 불필요**:
```
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
ANTHROPIC_API_KEY=sk-ant-...
DEFAULT_MODEL=claude-haiku-4-5-20251001
DAILY_COST_LIMIT_USD=5
PILOT_WORKSPACE=pilot
BOT_NAME=tybot
ARCHIVE_DIR=/var/lib/tybot/archive     # ← 서버 경로. 빠뜨리면 /opt/tybot/archive 로 가고
QA_LOG_DIR=/var/lib/tybot/qa-log       #    거기엔 쓰기 권한이 없어 수집이 저장되지 않는다
QA_LOG_MD=1
REALTIME_INGEST=1
EXEC_USERS=
```
> `install.sh` 는 **새로 만들 때만** 이 두 경로를 채운다. 이미 `tybot.env` 가 있으면 건드리지 않으니
> 직접 확인해야 한다. 기동 로그의 `경로 점검 통과 - archive=... qa_log=...` 줄로 확인하고,
> `상태` 에 `🛑 쓰기 불가` 가 보이면 이 두 줄이 빠진 것이다.

> ⚠️ **값 뒤에 인라인 주석을 쓰지 마세요.** `BOT_NAME=tybot   # 설명` 처럼 쓰면 파서에 따라
> 주석까지 값에 포함된다. 설명은 줄 앞에 단독으로 둔다. `export` 접두사도 쓰지 않는다.
>
> 앱은 이 파일을 python-dotenv 로 직접 읽는다. 유닛의 `EnvironmentFile=` 을 쓰지 않는 이유는
> systemd 파서가 `export`·`=` 주변 공백·인라인 주석을 다르게 처리해서, **점검은 통과하는데
> 기동은 실패하는** 어긋남이 생기기 때문이다.

확인 (값은 마스킹되어 출력 — 공유 안전):
```bash
sudo -u tybot /opt/tybot/.venv/bin/python /opt/tybot/scripts/check_env.py
```
`[OK]` 필수 3개 + 패키지 3개가 모두 떠야 한다.

---

## 4. ⚠️ 로컬 봇 먼저 끄기

**같은 봇 토큰으로 두 프로세스가 붙으면 Slack 이 양쪽에 이벤트를 보낸다** — 중복 답변,
중복 수집(원문은 멱등이라 중복 저장은 안 되지만), LLM 비용 2배.

서버 기동 **전에** 로컬 PC 의 `python -m tybot.slack.pilot` 을 `Ctrl+C` 로 종료한다.

---

## 5. 기존 아카이브 이관 (선택)

파일럿에서 로컬에 쌓인 원문을 이어서 쓰려면:
```bash
# 로컬 PC
scp -r archive/channels <계정>@<서버>:/tmp/
# 서버
sudo cp -r /tmp/channels/. /var/lib/tybot/archive/channels/
sudo chown -R tybot:tybot /var/lib/tybot/archive
```
안 옮겨도 된다 — 실시간 수집이 새로 쌓는다. 다만 지금까지 모은 원문은 사라진다.

---

## 6. 기동 및 검증

```bash
sudo systemctl enable --now tybot
journalctl -u tybot -f
```

기동 로그에 이 줄이 떠야 한다:
```
tybot.slack INFO 파일럿 봇 기동 - workspace=pilot archive=/var/lib/tybot/archive realtime=True exec=0명
```

### Slack 에서 순서대로
| 순서 | 입력 | 기대 |
|---|---|---|
| 1 | `@tybot 상태` | 연결·가동·모델·아카이브 통계. **워크스페이스 이름이 실제와 일치**해야 함 |
| 2 | 채널에 아무 대화 몇 줄 | 로그에 실시간 수집 흔적, `상태` 의 원문 줄 수 증가 |
| 3 | `@tybot 이번주 요약` | 채널별 정리 + `출처:` |
| 4 | `@tybot 없는키워드zzz` | "추측으로 답하지 않습니다" + 문서 목록 |
| 5 | 서버에서 `ls -R /var/lib/tybot/archive` | MD 파일이 실제로 생성됨 |

### 재시작 내구성 확인
```bash
sudo systemctl restart tybot && sleep 5 && systemctl is-active tybot   # active
sudo reboot                                                            # 부팅 후 자동 기동 확인
```

---

## 7. 트러블슈팅

| 증상 | 원인 / 조치 |
|---|---|
| `Failed to start` / `ModuleNotFoundError` | `sudo -u tybot /opt/tybot/.venv/bin/python -c "import tybot, slack_bolt"` 로 확인. 실패면 `install.sh` 재실행 |
| 기동은 되는데 Slack 응답 없음 | 아웃바운드 443 차단. `sudo -u tybot curl -sI https://slack.com` |
| 프록시 환경 | `/etc/systemd/system/tybot.service.d/proxy.conf` 생성:<br>`[Service]`<br>`Environment=HTTPS_PROXY=http://프록시:포트`<br>`Environment=NO_PROXY=localhost,127.0.0.1`<br>후 `systemctl daemon-reload && systemctl restart tybot` |
| 설정을 고쳤는데 반영 안 됨 | `journalctl -u tybot \| grep "환경설정 출처"` 로 읽은 파일 확인. 인라인 주석·`export` 접두사·`=` 주변 공백을 제거 |
| `Start request repeated too quickly` | `sudo systemctl reset-failed tybot` 후 재시작(재시작 폭주 차단이 걸린 상태) |
| `Permission denied: /var/lib/tybot/...` | `sudo chown -R tybot:tybot /var/lib/tybot` |
| 응답은 정상인데 **문서 0건 · 수집 0건** | `tybot.env` 에 `ARCHIVE_DIR`/`QA_LOG_DIR` 누락. 기본값(`/opt/tybot/archive`)은 봇에게 쓰기 권한이 없다. 기동 로그의 `쓸 수 없습니다` 에러 확인 |
| SELinux 관련 거부 | `sudo ausearch -m avc -ts recent`. 표준 경로(`/var/lib`)를 쓰면 보통 발생하지 않음. **enforcing 을 끄지 말고** 원인부터 확인 |
| `conversations.history` 실패/느림 | Slack 신규 앱 제한(분당 1요청/15건). 정상 — 백필은 느리고, 실시간 수집이 본선 |
| 비공개 채널이 안 보임 | 해당 채널에서 `/invite @tybot`. 봇은 초대 없이 비공개 채널을 목록조차 못 본다 |
| 답변이 오늘 갑자기 끊김 | 일별 비용 상한. `journalctl -u tybot \| grep 한도` / `DAILY_COST_LIMIT_USD` 조정 |

---

## 7-A. 질의응답 기록 보기

요청 1건마다 **journald 1줄 + 파일 2건**이 남는다. 경로마다 로그가 달라지지 않게 한 곳으로 모았다.

```bash
# 질문·의도·근거·비용이 한 줄에 (상태 질문도 질문 텍스트가 남는다)
journalctl -u tybot -f | grep " qa "

# 사람이 읽는 일자별 기록
sudo cat /var/lib/tybot/qa-log/$(date +%F).md

# 기계 분석용 (월별 JSONL)
sudo jq -r '[.ts, .user_name, .intent_kind, .reason, .hits, .cost_usd, .question] | @tsv' /var/lib/tybot/qa-log/qa-$(date +%Y-%m).jsonl

# 오늘 비용 합계
sudo jq -s 'map(.cost_usd)|add' /var/lib/tybot/qa-log/qa-$(date +%Y-%m).jsonl

# 근거를 못 찾은 질문만 (검색 품질 점검용)
sudo jq -r 'select(.reason=="no_hits")|.question' /var/lib/tybot/qa-log/qa-$(date +%Y-%m).jsonl
```

**아카이브와 분리되어 있다.** `qa-log/` 는 `archive/channels/` 밖이고, `ArchiveStore` 는 이 파일을
읽지 않는다 — 봇 답변이 다시 근거가 되면 요약 재귀가 발생하기 때문이다(원칙 1).
회귀 테스트로 고정돼 있다(`test_md_is_not_read_as_archive`).

기록에는 질문 원문이 그대로 들어간다. `/var/lib/tybot` 은 `0750 tybot:tybot` 이고,
백업 시 이 디렉터리도 사내 자료로 취급해야 한다.

---

## 7-A2. 아카이브 점검 타이머 (권장)

아카이브는 **조용히 고장난다.** 스키마가 깨져 검색에서 통째로 빠지거나, 채널 하나만 며칠째
멈춰 있어도 사람이 `상태` 를 물어야 드러난다. 15분마다 점검해서 로그와 리포트로 남긴다.

```bash
sudo systemctl enable --now tybot-tidy.timer
systemctl list-timers tybot-tidy
sudo systemctl start tybot-tidy.service      # 즉시 1회
journalctl -u tybot-tidy -n 30
```

한 줄 요약이 매회 남는다:
```
tidy docs=12 lines=1043 errors=0 warns=1
tidy #프로젝트-업데이트: 4.2일째 수집 없음 - 봇 초대·권한 확인
```

| 검사 | 심각도 | 왜 위험한가 |
|---|---|---|
| 스키마 위반 | 오류 | 그 파일이 **검색에서 통째로 빠진다** |
| 파싱 안 되는 원문 줄 | 오류 | 그 줄만 조용히 누락된다 |
| N일째 수집 없음 | 경고 | 봇이 채널에서 빠졌거나 권한이 끊겼다 |
| 원문 0줄 / 중복 라인 | 경고 | 수집 경로·멱등성 문제 |

사람이 읽는 리포트: `sudo cat /var/lib/tybot/reports/tidy-$(date +%F).md` (30일 보관 후 자동 정리)

**점검 잡은 원문을 절대 수정하지 않는다.** systemd 유닛에서도 아카이브를 `ReadOnlyPaths` 로
묶고 리포트 경로만 쓰기를 허용한다. 리포트는 `archive/` 밖에 쓴다 — 안에 쓰면 그게 다시
근거로 검색된다(요약 재귀).

`TIDY_STALE_DAYS` 로 밀림 판정 기준(기본 3일)을 조정한다.

---

## 7-B. 협업자 push 자동 배포

협업자가 push 할 때마다 사람이 `git pull` 하지 않아도 되게 타이머를 쓴다.
**인바운드 포트가 필요 없다**(서버가 GitHub 로 나가서 확인하는 방식).

```bash
sudo cp /opt/tybot/deploy/tybot-update.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tybot-update.timer
systemctl list-timers tybot-update    # 다음 실행 시각 확인
```

동작 순서 — [update.sh](../../deploy/update.sh):
1. `git fetch` → 변경 없으면 아무것도 안 함
2. 새 커밋 있으면 **소스 클론에서 테스트 먼저 실행**
3. **통과한 커밋만** `install.sh` → `systemctl restart tybot`
4. 실패 시 배포 중단 — 운영 프로세스는 그대로 유지되므로 롤백이 필요 없다

기본 주기는 업무시간(월~금 09~19시) 10분 간격이다. 야간엔 사람이 지켜볼 수 없으니 돌리지 않는다.
주기·브랜치 변경:
```bash
sudo systemctl edit tybot-update.service   # Environment=TYBOT_BRANCH=deploy 등
sudo systemctl edit tybot-update.timer     # OnCalendar 변경
```

```bash
# 수동 실행 / 결과 확인
sudo bash /opt/tybot/deploy/update.sh
journalctl -u tybot-update -n 40
```

> **자동 배포의 유일한 안전장치가 테스트다.** 검토 없이 운영에 들어가는 게 부담이면
> `TYBOT_BRANCH=deploy` 로 바꿔 사람이 머지한 브랜치만 배포하게 하는 편이 안전하다.

---

## 7-C. 정기 백필 타이머 (선택)

실시간 수집이 본선이지만, 봇 재시작·네트워크 단절 구간을 메우려면 정기 백필을 켠다.

```bash
sudo systemctl enable --now tybot-collect.timer
systemctl list-timers tybot-collect
sudo systemctl start tybot-collect.service    # 즉시 1회 실행
journalctl -u tybot-collect -n 40
```

- 업무시간 **07~19시 매시 정시**(하루 13회) 실행.
- Slack 신규 앱 제한은 **"하루 15회"가 아니라 `conversations.history` 요청당 15건 · 분당 1요청**이다.
  따라서 채널당 하루 최대 약 195건까지 메울 수 있고, 그보다 활발한 채널은 실시간 수집이 담당한다.
- 채널이 N개면 페이싱 때문에 **최소 N분**이 걸린다(`COLLECT_PACE_SECONDS=65`).
  채널이 40개를 넘으면 정시 간격 안에 못 끝나므로 그때는 채널을 나눠 돌리거나 주기를 늘린다.
- 재수집은 멱등이다 — 이미 있는 원문 라인은 다시 쓰지 않는다.

## 8. 운영 명령

| 확인 | 명령 |
|---|---|
| 상태 | `systemctl status tybot` |
| 실시간 로그 | `journalctl -u tybot -f` |
| 오늘 LLM 호출·비용 | `journalctl -u tybot --since today \| grep llm_call` |
| 의도 분류 추이 | `journalctl -u tybot --since today \| grep "intent kind"` |
| 아카이브 용량 | `du -sh /var/lib/tybot/archive` |
| 질의응답 기록 | `journalctl -u tybot -f \| grep " qa "` / `sudo cat /var/lib/tybot/qa-log/$(date +%F).md` |
| 자동 배포 이력 | `journalctl -u tybot-update -n 40` |
| 아카이브 점검 | `journalctl -u tybot-tidy \| grep " tidy "` / `sudo cat /var/lib/tybot/reports/tidy-$(date +%F).md` |
| 환경변수 점검 | `sudo -u tybot /opt/tybot/.venv/bin/python /opt/tybot/scripts/check_env.py` |

`intent ... src=regex` 가 계속 보이면 LLM 분류 호출이 실패하는 것이다(규칙 폴백으로 동작 중).
같은 시각의 경고 로그를 확인한다.

## 9. 업데이트

```bash
cd /tmp/tybot-src && sudo git pull      # 또는 새 tar 를 풀고
sudo bash deploy/install.sh
sudo systemctl restart tybot
journalctl -u tybot -n 30
```
아카이브(`/var/lib/tybot`)와 시크릿(`/etc/tybot`)은 건드리지 않는다.

## 10. 백업

| 대상 | 방법 | 우선순위 |
|---|---|---|
| `/var/lib/tybot/archive` | 중앙 Git 비공개 저장소로 push (쓰기 권한 필요) 또는 파일 백업 | **최우선 — 재생성 불가** |
| `/var/lib/tybot/qa-log` | 감사 기록 — 재생성 불가. 질문 원문 포함이라 사내 자료로 취급 | **높음** |
| `/etc/tybot/tybot.env` | 시크릿 매니저/봉인 문서. 백업 매체 암호화 필수 | 중 (재발급 가능) |
| 코드 | Git 저장소 | 하 |
