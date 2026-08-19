# 서버 배치 런북 — Rocky Linux 8

_TYBot (패키지 `tybot`, Slack 핸들 `@tybot`) · 파일럿 1개 워크스페이스_

이 단계에서는 **DB를 설치하지 않는다.** 아카이브는 MD 파일이고, PostgreSQL/MariaDB 인덱서는
파일럿 검증 후 다음 단계다([db-and-acl.md](../design/db-and-acl.md) 참조).

---

## 0. 사전 확인 (3개)

| 확인 | 명령 / 방법 | 안 되면 |
|---|---|---|
| Python 3.11 설치 가능? | `dnf list available python3.11` | RL8 기본 python3 은 3.6 — 우리 코드는 3.11+ 필수 |
| **아웃바운드 443** 열려 있나? | `curl -sI https://slack.com \| head -1` | 방화벽/프록시 담당자에게 요청 (인바운드는 0개 필요) |
| PyPI 접근 되나? | `curl -sI https://pypi.org \| head -1` | 안 되면 1-B 오프라인 경로로 진행 |

프록시를 써야 하면 [7절](#7-트러블슈팅)의 프록시 설정을 먼저 적용한다.

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
tar --exclude=.git --exclude=.venv --exclude=.env --exclude=archive \
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
ARCHIVE_DIR=/var/lib/tybot/archive     # ← 서버 경로 (install.sh 가 자동 설정)
REALTIME_INGEST=1
EXEC_USERS=
```

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
| `Permission denied: /var/lib/tybot/...` | `sudo chown -R tybot:tybot /var/lib/tybot` |
| SELinux 관련 거부 | `sudo ausearch -m avc -ts recent`. 표준 경로(`/var/lib`)를 쓰면 보통 발생하지 않음. **enforcing 을 끄지 말고** 원인부터 확인 |
| `conversations.history` 실패/느림 | Slack 신규 앱 제한(분당 1요청/15건). 정상 — 백필은 느리고, 실시간 수집이 본선 |
| 비공개 채널이 안 보임 | 해당 채널에서 `/invite @tybot`. 봇은 초대 없이 비공개 채널을 목록조차 못 본다 |
| 답변이 오늘 갑자기 끊김 | 일별 비용 상한. `journalctl -u tybot \| grep 한도` / `DAILY_COST_LIMIT_USD` 조정 |

---

## 8. 운영 명령

| 확인 | 명령 |
|---|---|
| 상태 | `systemctl status tybot` |
| 실시간 로그 | `journalctl -u tybot -f` |
| 오늘 LLM 호출·비용 | `journalctl -u tybot --since today \| grep llm_call` |
| 의도 분류 추이 | `journalctl -u tybot --since today \| grep "intent kind"` |
| 아카이브 용량 | `du -sh /var/lib/tybot/archive` |
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
| `/etc/tybot/tybot.env` | 시크릿 매니저/봉인 문서. 백업 매체 암호화 필수 | 중 (재발급 가능) |
| 코드 | Git 저장소 | 하 |
