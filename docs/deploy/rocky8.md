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
sudo dnf install -y python3.11 python3.11-pip git
```

| 패키지 | 왜 |
|---|---|
| `python3.11` | RL8 기본 `python3` 은 3.6. 우리 코드는 3.11+ |
| `python3.11-pip` | venv 안에서 의존성 설치 |
| `git` | 저장소 clone / 업데이트 |
| `tar` | `install.sh` 가 코드를 `/opt/tybot` 으로 배치 (rsync 는 SELinux 가 갇힌 도메인으로 전이시켜 쓰지 않는다) |

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
sudo install -d -o root -g root -m 755 /var/lib/tybot
sudo git clone <저장소 URL> /var/lib/tybot/src
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
sudo mkdir -p /var/lib/tybot/src && sudo tar -xzf /tmp/tybot.tar.gz -C /var/lib/tybot/src
```

### 인터넷 없는 서버라면 (추가 단계)
```bash
# 인터넷 되는 PC에서 (리눅스용 휠을 받는다)
bash deploy/wheelhouse.sh          # → wheels/ 생성
# wheels/ 를 위 tar 에 포함하거나 별도로 scp
```

---

> **소스 클론은 `/var/lib/tybot/src` 에 둔다. `/tmp` 를 쓰지 않는다.**
> `systemd-tmpfiles` 가 오래된 `/tmp` 파일을 지워 체크아웃이 사라지고,
> SELinux 가 `/tmp` 를 `user_tmp_t` 로 라벨해 서비스가 읽지 못하는 일이 생긴다.
> 2026-09-02 에 콘솔 배포가 `rsync ... Permission denied` 로 실패한 것이 그 경우다.
>
> 이미 `/tmp/tybot-src` 를 쓰고 있으면 한 번만 옮긴다:
> ```bash
> sudo systemctl stop tybot-deploy.path
> sudo mv /tmp/tybot-src /var/lib/tybot/src
> sudo chown -R root:root /var/lib/tybot/src  # 배포는 root 로 돈다
> sudo restorecon -Rv /var/lib/tybot/src      # SELinux 라벨 정정
> sudo systemctl start tybot-deploy.path
> ```

## 2. 설치 (한 줄)

```bash
cd /var/lib/tybot/src
sudo bash deploy/install.sh                      # 봇만
sudo WITH_CONSOLE=1 bash deploy/install.sh       # 봇 + 관리 콘솔
# 또는
sudo OFFLINE=1 bash deploy/install.sh            # wheels/ 사용
```

**`WITH_CONSOLE=1` 을 빼면 콘솔이 설치되지 않는다.** `fastapi`·`uvicorn` 은 선택
의존성이라 봇만 돌리는 서버에는 올리지 않는다 — 웹 프레임워크를 얹으면 갱신할 표면과
취약점 대상이 그만큼 늘어난다.

스크립트가 하는 일 (멱등 — 재실행 안전):

| 단계 | 내용 |
|---|---|
| 1 | `python3.11` 설치 |
| 2 | `tybot` 시스템 계정(로그인 불가) + `/opt/tybot`, `/etc/tybot`, `/var/lib/tybot/{archive,cache}` |
| 3 | 코드를 `/opt/tybot` 으로 동기화 (`.env`·`archive`·`.git` 제외) |
| 4 | `/opt/tybot/.venv` 생성 + `requirements.txt` 고정 버전 설치 |
| 5 | 코드 소유권 `root:tybot`, 봇은 **읽기·실행만** (자기 코드 수정 불가) |
| 6 | `/etc/tybot/tybot.env` 생성(0640) + `tybot.service` 등록 |

`WITH_CONSOLE=1` 이면 여기에 더해:

| 단계 | 내용 |
|---|---|
| a | Node 20 설치 (`dnf module enable nodejs:20`) |
| b | `deploy/requirements-console.txt` 설치 (`fastapi`·`uvicorn`) |
| c | `console-web` 빌드 → `console-web/dist` |
| d | `CONSOLE_DIST` 를 `tybot.env` 에 자동 추가 |
| e | `tybot-console.service` 등록 (enable 은 사람이) |

**화면 빌드가 실패해도 설치는 계속된다.** 화면이 안 만들어지는 것과 봇이 못 뜨는 것은
무게가 다르다. 실패하면 마지막에 경고가 뜨고, 콘솔 API 는 뜨되 브라우저에는 404 가 난다.

### 경로 표준 — 왜 이렇게 나누나
| 용도 | 경로 | 이유 |
|---|---|---|
| 코드 | `/opt/tybot` | 재배포로 갈아엎히는 영역 |
| 시크릿 | `/etc/tybot/tybot.env` | 설정은 `/etc`. `0640 root:tybot`, 저장소와 물리 분리 |
| | | 앱이 python-dotenv 로 직접 읽는다(유닛의 `EnvironmentFile=` 미사용) |
| **원문 아카이브** | `/var/lib/tybot/archive` | **코드와 분리** — `git pull`·재클론이 원문을 건드리면 안 된다 |
| 첨부 처리 메타데이터 | `/var/lib/tybot/staging` | 로컬 변환본·PII 거부 상태·원본 승인 상태 |
| 첨부 원본 | `/var/lib/tybot/objects` | 바이너리 원본. 검색 금지·접근권한 최소화 |
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

**키가 실제로 통하는지도 확인한다** — 형식 검사로는 폐기된 키를 구분할 수 없다. 이 절을 건너뛰면 봇은 정상 기동하고 **질문마다** 401 로 죽는다:
```bash
sudo -u tybot /opt/tybot/.venv/bin/python /opt/tybot/scripts/check_env.py --live
```
`[OK]   LLM 실호출 성공` 이 떠야 한다. 1토큰만 쓰므로 비용은 무시할 수준이다.

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
scp -r archive/workspaces <계정>@<서버>:/tmp/
# 서버
sudo cp -r /tmp/workspaces/. /var/lib/tybot/archive/workspaces/
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

## 6-A. 코드 업데이트 — 서버 터미널 없이

매번 `git pull` + `install.sh` 를 치지 않는 방법 3가지. 셋 다 같은 `update.sh` 를 쓰며,
**테스트를 통과하지 못한 커밋은 배포되지 않는다**(운영 프로세스 미변경).

### A. 자동 (권장 — 지금 바로 가능)
```bash
sudo systemctl enable --now tybot-update.timer
systemctl list-timers tybot-update --no-pager
```
업무시간(월~금 09~19시) 10분 간격. push 후 최대 10분이면 반영된다.
야간 배포는 사람이 못 지켜보므로 돌지 않는다.

### B. 콘솔 버튼 (B-25 — 구현됨)
관리 콘솔에서 '지금 배포'. 콘솔은 root 권한 없이 요청 파일만 만들고,
root 로 도는 path 유닛이 그것을 보고 배포한다.
```bash
sudo systemctl enable --now tybot-deploy.path
```
`WITH_CONSOLE=1` 설치·업데이트에서는 이 path 유닛을 자동으로 활성화한다. 관리자는 콘솔의
`운영 관리 > 배포 관리`에서 `업데이트 확인 및 배포`를 누르고 진행 상태를 확인한다.
| 흐름 | |
|---|---|
| 콘솔(`User=tybot`) | `/var/lib/tybot/deploy-request.json` 생성 |
| `tybot-deploy.path` | 파일 생성 감지 → `tybot-deploy.service` 기동 |
| `tybot-deploy.service`(root) | `deploy-runner.sh` → `update.sh` |
| 콘솔 | `/var/lib/tybot/deploy-status.json` 폴링해 진행 표시 |

**요청 파일의 내용은 명령 인자로 쓰이지 않는다.** 브랜치·경로는 유닛에 고정돼 있어
웹에서 온 값이 실행에 영향을 주는 경로가 없다. 콘솔에 sudo·polkit·setuid 를 주지 않는다 —
그 순간 웹 취약점 하나가 서버 장악이 된다.

수동 확인:
```bash
sudo -u tybot bash -c 'echo "{\"actor\":\"test\"}" > /var/lib/tybot/deploy-request.json'
journalctl -u tybot-deploy -f
cat /var/lib/tybot/deploy-status.json
```

### C. 수동 (여전히 유효)
```bash
sudo bash /opt/tybot/deploy/update.sh
```

**"변경 없음" 이 떠도 배포가 안 된 것일 수 있다.** 손으로 `git pull` 을 먼저 하면
소스와 원격이 같아져 새 커밋이 없다고 판단한다. 그래서 배포된 커밋을 따로 기록해
두고 그것과 비교한다 — `/opt` 가 뒤처져 있으면 새 커밋이 없어도 배포한다.

설치 옵션만 바꿔 다시 돌리고 싶으면(예: `WITH_CONSOLE=1` 을 처음 켤 때):

```bash
sudo TYBOT_FORCE=1 WITH_CONSOLE=1 bash /opt/tybot/deploy/update.sh
```

---

## 6-A1. 타이머 켜기 — **설치가 켜 주지 않는다**

`install.sh` 는 타이머 파일을 배치만 하고 `enable` 은 하지 않는다. 운영자가 무엇을
돌릴지 정하게 하려는 것인데, **꺼져 있다는 사실을 아무도 모르는 것**이 실제로 문제였다.
타이머가 안 켜져 있으면 일정 동기화·DM 알림·백필이 통째로 돌지 않는데, 오류가 나지
않으니 몇 주가 지나도 모른다.

설치 마지막에 꺼진 타이머를 이름과 함께 알린다. 그 줄이 보이면 켠다.

```bash
systemctl list-timers 'tybot-*' --no-pager     # 지금 도는 것
```

| 타이머 | 주기 | 없으면 |
|---|---|---|
| `tybot-schedule-sync` | 1분 | Oracle 일정이 안 들어와 `/일정` 이 옛 자료를 보여준다 |
| `tybot-schedule-dm` | 1분 | **일정 DM 알림이 아예 안 간다** |
| `tybot-collect` | 정기 | 과거 대화 백필이 안 돈다 |
| `tybot-update` | 정기 | push 해도 서버가 안 따라온다 |
| `tybot-tidy` | 정기 | 스키마 점검·수집 밀림 경보가 안 돈다 |

```bash
sudo systemctl enable --now tybot-schedule-sync.timer tybot-schedule-dm.timer \
  tybot-collect.timer tybot-update.timer tybot-tidy.timer
```

**일정 DM 은 켜기 전에 손으로 한 번 돌려 본다.** 타이머로 돌면 실패가 조용히 지나간다.

```bash
sudo -u tybot TYBOT_ENV_FILE=/etc/tybot/tybot.env \
  /opt/tybot/.venv/bin/python -m tybot.schedule_dm --plan-only
```

끝에 `일정 DM 발송 sent=N retry=N failed=N skipped=N` 이 나온다.
`sent=0` 이고 아무것도 없으면 큐가 빈 것이고, 원인은 대개 셋이다 —
일정이 동기화되지 않았거나(`schedule_occurrence` 가 비었거나),
사용자가 `/일정 알림` 으로 DM 을 켜지 않았거나, 사번↔Slack 매핑이 없다.

---

## 6-A1. psql 을 부를 때 — **포트를 반드시 붙인다**

우리 PostgreSQL 은 **55432** 를 쓴다. `psql` 은 5432 를 기본값으로 보므로 포트를
빼면 이렇게 나온다 — DB 가 죽은 것처럼 보이지만 포트만 틀린 것이다.

```
psql: error: connection to server on socket "/run/postgresql/.s.PGSQL.5432" failed:
        No such file or directory
```

```bash
# 슈퍼유저 작업(확장 설치·스키마 적용)
sudo -u postgres psql -p 55432 -d tyslackai -f <파일>

# 봇 계정으로 확인만 할 때는 설정 파일의 DATABASE_URL 을 쓴다 — 포트가 들어 있다
sudo -u tybot bash -c 'set -a; . /etc/tybot/tybot.env; set +a; psql "$DATABASE_URL" -c "\dt"'
```

`tyslackai` 계정으로 소켓에 붙으면 `Peer authentication failed` 가 난다.
그때는 `-h 127.0.0.1` 을 붙여 TCP 로 간다.

## 6-A2. 관리 콘솔 (선택)

`WITH_CONSOLE=1` 로 설치했으면 계정을 만들고 켠다.

```bash
# /etc/tybot/tybot.env의 DATABASE_URL을 읽어 스키마를 적용한다
/opt/tybot/.venv/bin/python -m tybot.console.auth init-schema

# 워크스페이스 Slack 토큰용 전용 암호화 키를 한 번만 만든다. 이 파일도 백업 대상이다.
KEY=$(/opt/tybot/.venv/bin/python -c \
  "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
printf '%s\n' "$KEY" | sudo install -o tybot -g tybot -m 0400 /dev/stdin \
  /etc/tybot/workspace-secret.key
unset KEY
# /etc/tybot/tybot.env에 키 파일 경로를 추가한다.
printf '%s\n' 'WORKSPACE_SECRET_KEY_FILE=/etc/tybot/workspace-secret.key' | \
  sudo tee -a /etc/tybot/tybot.env >/dev/null

# 비밀번호는 터미널에서 두 번 숨김 입력한다. DB에는 scrypt 해시만 저장된다.
/opt/tybot/.venv/bin/python -m tybot.console.auth \
  set-password dan@taeyoung.com --name dan --role admin

sudo systemctl enable --now tybot-console
```

로그인 ID는 회사 이메일이다. 위 `set-password`로 만든 최초 관리자의 이메일과 비밀번호로
로그인한다. 활성 DB 계정이 없거나 PostgreSQL에 연결할 수 없으면 콘솔은 기동하지 않는다.
첫 관리자 생성 뒤의 계정 추가·권한 변경·비밀번호 재설정은 관리자 전용 `콘솔 사용자 관리`
화면에서 한다. 기존 `CONSOLE_ACCOUNTS` 줄은 더 이상 사용하지 않으므로
`/etc/tybot/tybot.env`에서 삭제한다.

`workspace-secret.key`를 잃으면 DB에 저장된 Slack 토큰은 복구할 수 없다. DB 백업과 별도
위치에 함께 백업하되 저장소에는 넣지 않는다. 키를 교체하려면 기존 키로 토큰을 복호화해
새 키로 다시 암호화하는 마이그레이션이 먼저 필요하므로 파일만 덮어쓰면 안 된다.

| 역할 | 접근 범위 |
|---|---|
| 게스트 | 데이터 현황·수집 문서 목록·API 사용량 읽기 전용. 아카이브 원문은 열 수 없음 |
| 개발자 | 게스트 범위 + 헬스 체크·서비스 로그·봇 규칙·배포 요청 |
| 관리자 | 전체 메뉴 + 환경변수·워크스페이스·사용자 관리 및 변경 요청 승인 |

확인:

```bash
curl -s localhost:8787/api/health      # {"ok":true,...}
curl -sI localhost:8787/ | head -1     # HTTP/1.1 200 OK  ← 화면 파일까지 붙은 것
```

`/api/health` 는 200 인데 `/` 가 404 면 화면 빌드가 실패했거나 `CONSOLE_DIST` 가 틀렸다.

| 증상 | 원인 / 조치 |
|---|---|
| `Cannot read property 'react' of undefined` | Node 가 낡았다(Rocky 8 기본은 10, npm 6 은 lockfileVersion 3 을 못 읽는다). 아래 명령으로 올린다 |
| `node -v` 가 그대로 10.x | `dnf install` 은 '이미 설치됨' 으로 끝난다. **`distro-sync`** 를 써야 스트림 버전으로 옮겨진다 |
| 화면은 뜨는데 고친 내용이 안 보임 | 빌드를 안 했다. `sudo WITH_CONSOLE=1 bash deploy/install.sh` 재실행 |
| 로그인은 되는데 화면이 비어 있음 | API 오류. `journalctl -u tybot-console -n 50` |

Node 를 손으로 올려야 할 때:

```bash
sudo dnf module reset -y nodejs
sudo dnf module enable -y nodejs:20
sudo dnf distro-sync -y nodejs npm     # install 이 아니라 distro-sync 다
node -v                                 # v20.19 이상
```

### 접속 범위 — 지금은 사내망 전체 (임시)

`tybot-console.service` 는 `--host 0.0.0.0` 이다(오너 승인 2026-09-01).
`http://<서버IP>:8787` 로 사내 어디서나 열린다.

**평문 HTTP 이고 임시 상태다.** 로그인 비밀번호와 세션 쿠키가 사내망을 그대로 지나간다.
이 화면은 아카이브 원문 열람·환경설정 변경·배포 승인을 쥐고 있다.
nginx + TLS 로 옮기는 일이 BACKLOG **B-35** 에 있다.

그때까지 반드시 함께 지킬 것:

| | |
|---|---|
| 계정 | `console_user`의 활성 admin 계정을 최소 1개 유지한다 |
| 방화벽 | 8787 출발지를 필요한 대역으로 좁힌다 |
| 쿠키 | `CONSOLE_COOKIE_SECURE=1` 은 **TLS 를 붙인 뒤에** 켠다. 먼저 켜면 로그인이 안 된다 |

---

## 6-B. 일정 연동 (선택)

`/일정` 은 `schedule_occurrence` 를 읽는다. 채우는 경로는 둘이다.

```bash
# 1) 추출 — Oracle 에서 스냅샷을 만든다(방식 A: 봇 서버가 직접 조회)
sudo -u tybot /opt/tybot/.venv/bin/python /opt/tybot/scripts/schedule_export.py     --out /var/lib/tybot/inbox-schedule --mode live --horizon-hours 48

# 2) 반영 — 먼저 검사만 해 본다
sudo -u tybot /opt/tybot/.venv/bin/python -m tybot.schedulesync --dry-run
sudo -u tybot /opt/tybot/.venv/bin/python -m tybot.schedulesync

# 주기 실행(1분)
sudo systemctl enable --now tybot-schedule-sync.timer
journalctl -u tybot-schedule-sync -f
```

| 증상 | 원인 / 조치 |
|---|---|
| `삭제 판정이 지나치다` | 추출이 부분 실패했을 수 있다. Oracle 쪽을 확인하고, 정말 취소된 일정이면 `--force` |
| `체크섬 불일치` | 전송이 끝나지 않았다. 다음 주기에 다시 시도된다 |
| 폴더가 `미승인폴더` 로 집계됨 | `schedule_folder` 에 승인 등록이 안 됐다. 승인 목록이 곧 허용 목록이다 |
| `/일정` 이 "동기화 지연" 을 표시 | 타이머가 멈췄거나 Oracle 조회 실패. `systemctl status tybot-schedule-sync` |

### 전문 봇 라우팅 · MCP (B-36/B-39)

```bash
sudo -u postgres psql -p 55432 -d tyslackai \
  -f /opt/tybot/deploy/sql/specialist_routing_schema.sql
```

`specialist_bot` 에 세 컬럼을 더한다 — `routing_hint`(라우터가 읽는 설명),
`model`(이 전문가가 쓸 모델), `min_confidence`(부를 최소 신뢰도). 그리고
`specialist_mcp`(외부 MCP 허용 목록)를 만든다.

**전문가가 하나도 `enabled` 가 아니면 라우팅은 아무것도 하지 않는다** — 마스터가
직접 답한다. 그래서 이 스키마를 올려도 답변 동작은 바뀌지 않는다.

MCP 는 스키마에서 두 가지를 막는다.

| 막는 것 | 이유 |
|---|---|
| `http://` · localhost | 사내 질문이 그 URL 로 나간다. 평문이면 도중에 읽힌다 |
| 승인 없이 `enabled` | 코드가 한 곳을 빼먹어도 새지 않게 |

### 검색 색인 (B-40)

원문은 MD 에 그대로 두고, **색인만** Postgres 에 넣는다. 색인은 버려도 되는 사본이라
`content_sha` 로 언제든 재빌드된다.

```bash
# 스키마는 index_schema.sql 에 이미 있다(raw_line + pg_bigm).
sudo systemctl enable --now tybot-index.timer     # 10분 주기
sudo systemctl start tybot-index                  # 지금 한 번
sudo journalctl -u tybot-index -n 20 --no-pager
```

`검색 색인 문서=N 줄=M` 이 나오면 된다.

**색인이 비거나 밀려도 검색은 돌아간다** — 파일 스캔으로 되돌아간다. 그래서 고장이
눈에 안 띈다. 헬스 체크의 답변 품질 절에 「검색 색인이 N분 밀렸습니다」 로 올라온다.

pg_bigm 이 없으면 2글자 검색이 전체 스캔이 된다(3글자 이상만 인덱스). 확인:

```bash
sudo -u postgres psql -p 55432 -d tyslackai -c "\dx" | grep bigm
```

### 요약 검토자 (B-37)

```bash
sudo -u postgres psql -p 55432 -d tyslackai -f /opt/tybot/deploy/sql/reviewer_schema.sql
```

채널에서 `/채널 검토자 @사람 09:00`. **채널 소유자만** 정할 수 있다.
검토자가 없는 채널은 요약을 반영하지 않는다 — 헬스 체크에 목록으로 올라온다.

### LLM API 키를 DB 로 옮기기

`.env` 는 **평문**이다. 서버에 들어갈 수 있는 사람은 누구나 읽고, 백업·복사본에
그대로 따라다니며, 누가 언제 바꿨는지 남지 않는다. DB 에는 암호화해서 넣는다.

```bash
sudo cat /opt/tybot/deploy/sql/llm_secret_schema.sql | \
  sudo -u postgres psql -p 55432 -d tyslackai -v ON_ERROR_STOP=1
```

그 다음 콘솔의 **환경변수 설정 → LLM API 키** 에서 키를 붙여 넣는다.
저장하면 다음 답변부터 DB 값을 쓴다.

암호화 키는 `/etc/tybot/workspace-secret.key`(0400) 다. **DB 안에 두지 않는다** —
DB 백업만으로 풀 수 있으면 평문과 다를 바 없다. 이 파일은 백업 대상이고,
DB 백업과 **다른 곳에** 둬야 한다.

확인이 끝나면 `/etc/tybot/tybot.env` 의 `ANTHROPIC_API_KEY`·`OPENAI_API_KEY` 를
지운다. 남겨 두어도 DB 값이 이기지만, 지워야 평문이 실제로 사라진다.
DB 를 못 읽을 때 되돌아갈 자리로 잠시 남겨 두는 것도 방법이다.

### 개인 DM 알림 (선택)

```bash
sudo -u postgres psql -p 55432 -d tyslackai -f /opt/tybot/deploy/sql/schedule_dm_schema.sql
sudo systemctl enable --now tybot-schedule-dm.timer   # 1분 주기
```

`schedule_folder_org` 에 폴더별 승인 조직을 넣어야 대상자가 생긴다. **비어 있으면
아무에게도 가지 않는다** — 안전한 기본값이지만 기능이 조용히 0건으로 보인다.
사용자는 Slack 에서 `/일정 알림` 으로 직접 켠다(기본 미수신).

**시각 대조를 한 번 해야 한다** — 추출기가 그룹웨어 시각에 `+09:00` 을 붙인다.
데이터가 들어오면 `/일정 오늘` 출력과 그룹웨어 화면의 시각이 같은지 확인한다.

---

## 7. 트러블슈팅

| 증상 | 원인 / 조치 |
|---|---|
| `Failed to start` / `ModuleNotFoundError` | `sudo -u tybot /opt/tybot/.venv/bin/python -c "import tybot, slack_bolt"` 로 확인. 실패면 `install.sh` 재실행 |
| 기동은 되는데 Slack 응답 없음 | 아웃바운드 443 차단. `sudo -u tybot curl -sI https://slack.com` |
| 프록시 환경 | `/etc/systemd/system/tybot.service.d/proxy.conf` 생성:<br>`[Service]`<br>`Environment=HTTPS_PROXY=http://프록시:포트`<br>`Environment=NO_PROXY=localhost,127.0.0.1`<br>후 `systemctl daemon-reload && systemctl restart tybot` |
| 설정을 고쳤는데 반영 안 됨 | `journalctl -u tybot \| grep "환경설정 출처"` 로 읽은 파일 확인. 인라인 주석·`export` 접두사·`=` 주변 공백을 제거 |
| 봇은 뜨는데 질문마다 답이 없거나 `⚠️ LLM 인증에 실패했습니다(401)` | 키가 폐기·삭제됐다. `check_env.py --live` 로 확인 후 console.anthropic.com > API keys 에서 재발급. 형식·길이 검사로는 안 잡힌다 |
| `아카이브에 쓸 수 없어 기동하지 않습니다` | `ARCHIVE_DIR` 누락 또는 권한 문제. `sudo -u tybot /opt/tybot/.venv/bin/python /opt/tybot/scripts/check_env.py` 의 `=== 쓰기 경로 ===` 절 참조. 조회 전용으로 띄우려면 `ALLOW_READONLY_ARCHIVE=1` |
| `락 디렉터리를 쓸 수 없어 임시 경로로 대체` | `ARCHIVE_DIR` 이 비어 락 경로가 코드 경로(`/opt/tybot`, 읽기 전용) 아래로 떨어진 것이다. `tybot.env` 의 `ARCHIVE_DIR=/var/lib/tybot/archive` 확인, 또는 `STATE_DIR=/var/lib/tybot` 지정 |
| `Start request repeated too quickly` | `sudo systemctl reset-failed tybot` 후 재시작(재시작 폭주 차단이 걸린 상태) |
| `Permission denied: /var/lib/tybot/...` | `sudo chown -R tybot:tybot /var/lib/tybot --exclude=src` 가 안 되므로: `sudo find /var/lib/tybot -mindepth 1 -maxdepth 1 ! -name src -exec chown -R tybot:tybot {} +`. **`src` 는 제외한다** — 소스 체크아웃은 root 소유여야 한다(아래 항목) |
| `fatal: detected dubious ownership in repository` | 소스 체크아웃이 root 소유가 아니다. `sudo chown -R root:root /var/lib/tybot/src`. `safe.directory` 로 예외를 두지 않는다 — 그 저장소의 hook 이 root 로 실행될 수 있어 디렉터리에 쓸 수 있는 사람이 서버를 가져갈 수 있다 |
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

**아카이브와 분리되어 있다.** `qa-log/` 는 `archive/workspaces/` 밖이고, `ArchiveStore` 는 이 파일을
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
cd /var/lib/tybot/src && sudo git pull      # 또는 새 tar 를 풀고
sudo bash deploy/install.sh
sudo systemctl restart tybot
journalctl -u tybot -n 30
```
아카이브(`/var/lib/tybot`)와 시크릿(`/etc/tybot`)은 건드리지 않는다.

## 10. 백업

| 대상 | 방법 | 우선순위 |
|---|---|---|
| `/var/lib/tybot/archive` | 중앙 Git 비공개 저장소로 push (쓰기 권한 필요) 또는 파일 백업 | **최우선 — 재생성 불가** |
| `/var/lib/tybot/objects` | 암호화 파일 백업. Git 저장 금지 | **최우선 — 첨부 원본** |
| `/var/lib/tybot/staging` | 검수 진행 상태와 함께 파일 백업. Git 저장 금지 | 높음 |
| `/var/lib/tybot/qa-log` | 감사 기록 — 재생성 불가. 질문 원문 포함이라 사내 자료로 취급 | **높음** |
| `/etc/tybot/tybot.env` | 시크릿 매니저/봉인 문서. 백업 매체 암호화 필수 | 중 (재발급 가능) |
| 코드 | Git 저장소 | 하 |
