# 멀티 워크스페이스 운영

_봇을 두 번째·세 번째 워크스페이스에 올리고, 워크스페이스 간 자료 공유를 켜는 절차._

---

## 1. 설치 방식 — 왜 "앱을 워크스페이스마다 따로" 인가

Slack 에서 한 앱을 여러 워크스페이스에 넣는 방법은 두 가지다.

| | A. 워크스페이스마다 앱 분리 **(채택)** | B. 단일 앱 배포(distribution) |
|---|---|---|
| 토큰 | 워크스페이스마다 봇 토큰 + 앱 토큰 | 앱 토큰 1개 + 워크스페이스별 봇 토큰 |
| 설치 절차 | 각 워크스페이스에서 매니페스트로 앱 생성 → 설치 | OAuth 설치 링크 |
| **인바운드 포트** | **불필요** | **필요** — OAuth 리다이렉트 URL을 받을 HTTPS 엔드포인트 |
| 격리 | 강함(앱·토큰이 완전히 분리) | 약함(한 앱이 모든 설치를 관장) |
| 관리 부담 | 워크스페이스 수만큼 앱 관리 | 앱 하나 |

우리 보안 경계는 **인바운드 포트 0개**(Socket Mode 아웃바운드 전용)다. B는 OAuth 리다이렉트를 받을
공개 엔드포인트가 필요해 그 경계를 깬다. 워크스페이스가 수십 개로 늘면 B + 설치 저장소(InstallationStore)로
가는 게 맞지만, 지금 규모에선 A가 단순하고 격리도 강하다.

봇은 워크스페이스마다 Socket Mode 연결을 하나씩 열고, **한 프로세스**에서 모두 처리한다.
아카이브·감사기록·LLM 게이트웨이는 공유하고, **조회 권한만 워크스페이스 경계로 분리**한다.

---

## 2. 새 워크스페이스 추가 절차

### 2-0. 먼저 '키'를 정한다
워크스페이스 키는 짧은 ASCII 식별자 하나이고, 세 곳에 동시에 쓰인다.

| 쓰이는 곳 | 키가 `mgmt` 일 때 |
|---|---|
| 환경변수 접미사 | `SLACK_BOT_TOKEN_MGMT`, `SLACK_APP_TOKEN_MGMT` |
| 아카이브 디렉터리 | `archive/channels/mgmt/` |
| 프론트매터·권한 판정·감사 로그 | `workspace: mgmt` |

접미사는 키를 대문자로 바꾸고 영숫자 외 문자를 `_` 로 치환한 값이다. 한글·공백을 쓰면 환경변수
이름이 깨지므로 **소문자 ASCII 로 짧게** 정한다. 사람이 볼 이름은 `WORKSPACE_LABEL_*`(한글 가능)로 준다.

⚠️ **이미 수집을 시작한 워크스페이스의 키는 바꾸지 않는다.** 수집된 MD 의 디렉터리명과
프론트매터 `workspace:` 값에 키가 박혀 있어서, 바꾸면 권한 판정이 어긋나 **기존 원문이 안 보인다.**
부득이 바꿀 때는 디렉터리 이동 + 모든 파일의 프론트매터를 함께 고쳐야 한다.

### 2-1. Slack 앱 생성
1. 새 워크스페이스 계정으로 https://api.slack.com/apps → **Create New App → From a manifest**
2. 대상 워크스페이스 선택 → [`docs/pilot/slack-app-manifest.yaml`](pilot/slack-app-manifest.yaml) 붙여넣기
3. **Basic Information → App-Level Tokens → Generate**: 스코프 `connections:write` → `xapp-...`
4. **OAuth & Permissions → Install to Workspace** → `xoxb-...`
5. 수집 대상 채널마다 `/invite @tybot`

### 2-2. 서버 설정
`/etc/tybot/tybot.env` 를 이렇게 바꾼다. 키(`pilot`, `mgmt`)는 **아카이브 디렉터리 이름이자 ACL 식별자**라
한 번 정하면 바꾸기 번거롭다 — 조직 코드에 맞춰 정한다.

```bash
# 기존 단일 워크스페이스 설정(SLACK_BOT_TOKEN/SLACK_APP_TOKEN)은 지운다
WORKSPACES=pilot,mgmt

SLACK_BOT_TOKEN_PILOT=xoxb-...
SLACK_APP_TOKEN_PILOT=xapp-...
WORKSPACE_LABEL_PILOT=파일럿

SLACK_BOT_TOKEN_MGMT=xoxb-...
SLACK_APP_TOKEN_MGMT=xapp-...
WORKSPACE_LABEL_MGMT=경영본부

# 크로스 워크스페이스 열람 화이트리스트 (3절 참조). 비워두면 완전 격리.
CROSS_WS_READ=mgmt:pilot
```

- 환경변수 접미사는 **키를 대문자로 바꾸고 영숫자 외 문자를 `_` 로** 치환한 값이다
  (`team_자금` → `SLACK_BOT_TOKEN_TEAM_`… 처럼 되니, 키는 ASCII 로 짧게 정하는 편이 좋다).
- 토큰이 하나라도 빠지면 **기동을 막는다.** 반쪽만 뜨는 상태가 더 위험하다.

```bash
sudo systemctl restart tybot
journalctl -u tybot -n 30
```

기동 로그에서 워크스페이스별 연결과 크로스 열람 설정을 확인한다:
```
워크스페이스 연결 — pilot(파일럿) bot=xoxb-1234… readable=없음 / 실시간수집=True / 크로스열람=없음
워크스페이스 연결 — mgmt(경영본부) bot=xoxb-5678… readable=['pilot'] / 실시간수집=True / 크로스열람=['pilot']
기동 완료 — 워크스페이스 2개: ['pilot', 'mgmt']
```

Slack 에서 `@tybot 상태` 를 치면 첫 줄에 나온다:
```
*워크스페이스*: 경영본부 (`mgmt`) · 크로스 열람 허용: pilot
```

---

## 3. 크로스 워크스페이스 열람 — 관문 두 개

봇의 목적이 워크스페이스 간 자료 공유지만, 여기가 **유출 사고가 나는 지점**이다.
그래서 자료가 워크스페이스를 넘으려면 **독립된 관문 두 개**를 모두 통과해야 한다.

| 관문 | 누가 정하나 | 무엇을 막나 |
|---|---|---|
| ① `CROSS_WS_READ` 화이트리스트 | 서버 운영자 | 어느 워크스페이스가 어느 워크스페이스를 **볼 수 있는지** |
| ② 문서의 `visibility: public` | 자료를 가진 쪽 사람 | 그중 **무엇을** 내보낼지 |

- **기본값은 완전 격리다.** `CROSS_WS_READ` 를 안 쓰면 워크스페이스 간에 아무것도 안 넘어간다.
- 수집기는 원문을 항상 `visibility: private` 로 저장한다. 즉 **자동으로 넘어가는 자료는 없다.**
  공유하려면 사람이 해당 MD 파일의 프론트매터를 `public` 으로 바꿔야 한다.
- **화이트리스트는 단방향이다.** `mgmt:pilot` 은 "mgmt 가 pilot 을 읽는다"만 뜻한다.
  양방향이 필요하면 `CROSS_WS_READ=mgmt:pilot,pilot:mgmt` 로 둘 다 쓴다.
- 오타가 나면 기동이 막힌다(`WORKSPACES` 에 없는 키). 조용히 권한이 열리거나 닫히는 것을 방지한다.
- `acl` 필드는 **소유 워크스페이스 안의 채널 목록**이라 크로스 판정에 쓰지 않는다.
  (수집기가 `acl=[채널명]` 을 항상 넣으므로, 크로스에 acl 을 걸면 `public` 표시가 무력해진다.)
- `EXEC_USERS` 에 등록된 사용자는 **채널 멤버십과 워크스페이스 경계를 모두 우회**한다. 최소 인원만.

### 자료를 공유하는 실제 절차

전용 도구를 쓴다. 프론트매터의 `visibility` 한 줄만 바꾸고, 변경 전후로 스키마와 원문 라인 수를
검증한다. 원문이 한 글자라도 바뀌면 중단·되돌린다.

```bash
cd /opt/tybot
# 1) 현재 공개 상태 확인
sudo -u tybot ARCHIVE_DIR=/var/lib/tybot/archive .venv/bin/python scripts/share.py --list

# 2) 바뀔 내용 미리보기
sudo -u tybot .venv/bin/python scripts/share.py   /var/lib/tybot/archive/channels/pilot/프로젝트-업데이트.md --public --dry-run

# 3) 실제 전환
sudo -u tybot .venv/bin/python scripts/share.py   /var/lib/tybot/archive/channels/pilot/프로젝트-업데이트.md --public
```

바꾸는 순간부터 `CROSS_WS_READ` 에 등록된 워크스페이스에서 조회된다.
되돌리려면 같은 명령에 `--private` 를 쓴다.

> 손으로 편집해도 되지만(`vi`), 그때는 **`## 원문` 블록을 절대 건드리지 않도록** 주의해야 한다.
> 도구는 그 실수를 코드로 막는다.

### 회귀 테스트로 고정된 규칙
`tests/test_multi_workspace.py` — 화이트리스트 없으면 차단 / 화이트리스트 있어도 비공개는 차단 /
단방향 / 권한 없는 워크스페이스는 **채널명도 노출 안 함**.

---

## 4. 아카이브 구조

```
/var/lib/tybot/archive/channels/
├── pilot/           # 워크스페이스 키가 디렉터리명
│   └── 프로젝트-업데이트.md
└── mgmt/
    └── 경영_주간보고.md
```

워크스페이스별 디렉터리가 **물리 격리 계층**이다. 나중에 "워크스페이스별로 저장소를 쪼갠다"는
결정을 디렉터리 이동만으로 할 수 있다([db-and-acl.md](design/db-and-acl.md) 3절).

---

## 5. 주의사항

- **봇 프로세스는 여전히 한 곳에서만** 기동한다. 워크스페이스가 여러 개여도 프로세스는 하나다.
  같은 토큰으로 두 프로세스가 붙으면 중복 답변 + LLM 비용 2배.
- 워크스페이스마다 앱이 다르므로 **매니페스트를 고치면 모든 앱에 반영**해야 한다.
  한 곳만 바꾸면 그 워크스페이스만 스코프가 달라져 '조용한 고장'이 된다.
- LLM 비용 상한(`DAILY_COST_LIMIT_USD`)은 **전 워크스페이스 합산**이다. 워크스페이스가 늘면 같이 올린다.
- 감사 기록은 워크스페이스 구분 없이 한 파일에 쌓이고, 각 줄에 `workspace` 필드가 있다.
