# B-83 VIPBot 활성화 절차 · 준비 점검

작성 2026-11-06 · 신규 2 + 이 문서. **런타임 코드 수정 0.** 커밋하지 않았다.

| 파일 | |
|---|---|
| `scripts/check_vipbot_readiness.py` | 읽기 전용 준비 점검 |
| `tests/test_check_vipbot_readiness.py` | 104건 + skip 2(posix 전용) |
| `deploy/tybot-vipbot.conf.example` | 전용 설정 파일 본보기 |

`src/tybot/vipbot_runtime.py` 와 `tests/test_vipbot_runtime_role_db.py` 는
읽기만 했다.

## 왜 점검이 따로 필요한가

조각마다 시험이 있고 전부 초록이다. 그런데 시험은 **운영 서버의 실제 상태**를
모른다 — 역할이 안 만들어졌는지, 자격증명 파일이 0644 인지, 유닛이 필요한 설정을
안 주는지.

그 어긋남은 `systemctl start` 를 치는 순간에야 보인다. 그때는 이미 Slack 에
붙으려다 실패한 뒤고, 실패가 journal 한 줄로만 남는다. 점검은 그 순간을 **앞으로
당긴다.**

## 돌리는 법

```bash
cd /opt/tybot && sudo -u tybot env \
  VIPBOT_WORKSPACE=<워크스페이스> \
  VIPBOT_ARCHIVE_DIR=/var/lib/tybot/archive \
  VIPBOT_DAILY_LIMIT_USD=<금액> \
  /opt/tybot/.venv/bin/python /opt/tybot/scripts/check_vipbot_readiness.py

# DB 에 못 붙는 자리에서는 파일·유닛만
… /opt/tybot/scripts/check_vipbot_readiness.py --skip-db
# 콘솔·자동화에 물릴 때
… /opt/tybot/scripts/check_vipbot_readiness.py --json
```

종료 코드: `0` 막는 것 없음 · `1` 준비 안 됨 · `2` 입력 오류.

## 무엇을 보나

| | 보는 것 |
|---|---|
| DB 역할 | `tybot_vipbot`·`tybot_vipbot_definer` 는 NOLOGIN, `tybot_vipbot_runtime` 은 LOGIN. 과한 속성 없음. 멤버십은 **양방향**으로 본다 — group 의 멤버는 runtime 하나, definer 는 멤버 0·상위 0, ADMIN OPTION 0 |
| 창구 함수 | `vipbot_connection_config(text)`·`vipbot_authorization(text,text)` 가 있고, **소유자가 definer**, runtime 이 실행 가능, PUBLIC 은 불가(ACL 의 `grantee=0` 을 직접 본다) |
| 기반 표 | runtime 이 `bot_connection`·`bot_connection_secret`·`employee`·`user_identity`·`bot_access`·`bot_source_channel` 을 **직접 못 읽는다** |
| 연결 | 그 워크스페이스의 VIPBot 연결이 **정확히 1건**, team_id·bot_user_id·토큰쌍이 다 찼는가 |
| 자격증명 3 | 유닛이 적은 경로 그대로 존재·권한(0600)·**모양**. DSN 은 역할이 runtime 인지, 키는 `sk-ant-` 접두인지, Fernet 키가 유효한지 |
| archive | `VIPBOT_ARCHIVE_DIR` 이 절대경로이고 실제 디렉터리인가 |
| 설정 | `VIPBOT_WORKSPACE`·`VIPBOT_DAILY_LIMIT_USD`(0·음수·`1_0`·`+10`·`.5` 거절)·`VIPBOT_MODEL` |
| 설정 파일 | `/etc/tybot/tybot-vipbot.conf` 존재·**root 소유**·모드(0640 기대)·파싱 가능. **허용 네 키만**, 빠진 키와 모르는 키를 **이름만** 보고 |
| 유닛 | 공용 `EnvironmentFile` 안 들임(허용 경로는 VIP conf 하나), `DATABASE_URL`·`ARCHIVE_DIR`·`QA_LOG_DIR`·`TYBOT_ENV_FILE` 안 들임, 자격증명 3개 선언, **값을 박지 않음** |

## 안 하는 것

- **고치지 않는다.** `systemctl start/enable` · `GRANT`/`REVOKE`/`ALTER`/`INSERT`
  /`UPDATE`/`DELETE`/`DROP` · 파일 권한 변경이 **코드에 한 줄도 없다**(시험이 고정)
- **Slack 에 안 붙는다.** `auth_test` 도 안 부른다. 그건 기동의 일이고, 여기서
  부르면 「점검이 봇을 깨웠다」 가 된다
- **비밀 값을 출력하지 않는다.** DSN·토큰·API 키는 **모양만** 보고 버린다.
  남는 것은 경로·권한비트·역할명·개수·고정 낱말뿐이다. 이 출력은 티켓에 붙는다

창구 함수를 부르면 토큰 암호문이 프로세스로 돌아오지만 **세기만 하고 버린다.**
어디에도 적지 않는다.

## 지금 돌리면 나오는 것 (2026-11-06)

개발 PC 에서 `--skip-db` 로 돌린 실제 출력:

```
준비 안 됨: 켜기 전에 할 일 9건:
  - credential_missing: tybot-vipbot-anthropic-api-key
  - credential_missing: tybot-vipbot-database-url
  - credential_missing: tybot-workspace-secret-key
  - archive_unconfigured: VIPBOT_ARCHIVE_DIR
  - workspace_unconfigured: VIPBOT_WORKSPACE
  - budget_unconfigured: VIPBOT_DAILY_LIMIT_USD
  - unit_settings_missing: VIPBOT_WORKSPACE
  - unit_settings_missing: VIPBOT_ARCHIVE_DIR
  - unit_settings_missing: VIPBOT_DAILY_LIMIT_USD
```

앞의 여섯은 개발 PC 라서 나온다(자격증명은 서버에 있고, 설정은 명령줄로 준다).

**뒤의 셋은 진짜다.** 런타임은 세 설정을 **필수**로 받는데 유닛은 하나도 주지
않는다. 지금 유닛을 그대로 켜면 `vipbot_workspace_unconfigured` 로 멎는다 —
Slack 에 붙기도 전에. 활성화 4단계가 그것을 메운다.

## 활성화 순서

각 단계 뒤에 **점검을 다시 돌린다.** 줄어드는 것이 보여야 다음으로 간다.

### 1. 스키마와 권한 그룹

```bash
sudo /opt/tybot/deploy/apply-schema.sh
```
`tybot_vipbot`·`tybot_vipbot_definer` 를 만들고 창구 함수와 GRANT 를 건다.
이미 낯선 멤버나 ADMIN OPTION 이 붙어 있으면 **스키마 적용 전에 거절**한다.

### 2. 전용 LOGIN 계정

```bash
/opt/tybot/deploy/setup-tybot-vipbot-runtime.sh                 # 계획만
sudo /opt/tybot/deploy/setup-tybot-vipbot-runtime.sh --apply
sudo /opt/tybot/deploy/setup-tybot-vipbot-runtime.sh --set-password
sudo /opt/tybot/deploy/setup-tybot-vipbot-runtime.sh --verify
```
비밀번호는 인자로 넘기지 않는다(`ps` 에 남는다). `--verify` 가 7권한·PUBLIC·
다른 봇 창구까지 되검사한다.

### 3. 자격증명 세 개

```bash
sudo install -o tybot -g tybot -m 0600 /dev/null /etc/tybot/tybot-vipbot-database-url
sudo -u tybot vi /etc/tybot/tybot-vipbot-database-url
#   postgresql://tybot_vipbot_runtime:<비밀번호>@/tyslackai?host=/run/postgresql&port=55432

sudo install -o tybot -g tybot -m 0600 /dev/null /etc/tybot/tybot-vipbot-anthropic-api-key
sudo -u tybot vi /etc/tybot/tybot-vipbot-anthropic-api-key

ls -l /etc/tybot/workspace-secret.key    # 이미 있어야 한다(TYBot 과 같은 키)
```

> `workspace-secret.key` 는 TYBot 과 **같은 Fernet 키**다. 토큰을 넣은 쪽과 푸는
> 쪽이 같아야 하기 때문이고, 그래서 이 키만은 공유다. DB 계정·모델 키·앱은 전부
> 따로다.

### 4. 전용 설정 파일

**유닛에 값을 박지 않는다.** 박으면 서버마다 다른 값을 쓰는 순간 저장소의
유닛과 서버의 유닛이 갈리고, 그 갈림은 아무 데도 안 보인다. 유닛은 파일을
가리키기만 한다(`EnvironmentFile=/etc/tybot/tybot-vipbot.conf`, 이미 들어 있다).

```bash
sudo install -o root -g tybot -m 0640 \
    /opt/tybot/deploy/tybot-vipbot.conf.example /etc/tybot/tybot-vipbot.conf
sudo vi /etc/tybot/tybot-vipbot.conf
```

**네 키만 들어간다.** 그 밖의 줄은 점검이 `settings_key_forbidden` 으로 잡는다.

| 키 | |
|---|---|
| `VIPBOT_WORKSPACE` | 필수 |
| `VIPBOT_ARCHIVE_DIR` | 필수 · **절대경로** |
| `VIPBOT_DAILY_LIMIT_USD` | 필수 · `0`·음수·`1_0`·`+10`·`.5` 거절 |
| `VIPBOT_MODEL` | 선택 |

**시크릿은 절대 여기 적지 않는다.** `EnvironmentFile=` 은 이 파일의 **모든 줄**을
환경변수로 만든다 — 그래서 이것은 「설정」 이 아니라 **「프로세스의 환경 전체」**
다. `DATABASE_URL` 한 줄이면 전용 DB 계정이 무의미해지고, 토큰 한 줄이면 0640
파일에 시크릿이 눕는다. DSN·Slack 토큰·API 키는 `LoadCredential` 로만 들어온다
(0400, 유닛이 멎으면 사라짐).

허용 목록 방식이다 — **모르는 키는 전부 거절**한다. 금지 목록이면 내일 생기는
비밀 이름을 못 막는다.

`/etc/tybot/tybot.env` 는 **여전히 금지**다. 점검이 `unit_shared_env` 로 잡는다.
허용되는 `EnvironmentFile=` 경로는 `/etc/tybot/tybot-vipbot.conf` 하나뿐이다.

### 5. Slack 앱 설치

`docs/pilot/vipbot-slack-app-manifest.yaml` 로 **별도 앱**을 만든다. 스코프 6개·
이벤트 2개뿐이고 Socket Mode 만 쓴다. 설치 뒤 봇·앱 토큰을 콘솔에서 VIPBot
연결로 저장한다(`bot_connection`, `bot_key='vipbot'`).

### 6. 근거 채널과 사용자 권한

- 콘솔 > 사용자: `bot_access(bot='vipbot')` 에 쓸 사람을 등록
- 콘솔 > 봇 근거 채널: `bot_source_channel(bot='vipbot')` 에
  `(workspace, channel_id)` 를 **정확히** 등록

둘 중 하나라도 비면 VIPBot 은 답하지 않는다(`no_grant` · `no_sources`).

### 7. 점검이 조용해질 때까지

```bash
cd /opt/tybot && sudo -u tybot env VIPBOT_WORKSPACE=… VIPBOT_ARCHIVE_DIR=… \
  VIPBOT_DAILY_LIMIT_USD=… /opt/tybot/.venv/bin/python \
  /opt/tybot/scripts/check_vipbot_readiness.py
```
`준비됨` 이 나와야 다음으로 간다.

### 8. 격리 DB 시험 — **2026-11-06 통과**

```bash
cd /opt/tybot && sudo -u tybot env TEST_DATABASE_URL='postgresql://…/tyslackai_test' \
  /opt/tybot/.venv/bin/python -m pytest tests/test_vipbot_runtime_role_db.py -q \
  -p no:cacheprovider
```
**2026-11-06 폐기 가능한 PostgreSQL 18 에서 65 passed.** 기록은
[`2026-11-06-vipbot-runtime-role-db.md`](2026-11-06-vipbot-runtime-role-db.md).
재배포·스키마 변경 뒤에는 다시 돌린다.

**운영 DB 를 쓰지 않는다.** 65건이 definer 소유권·제3자 ACL 제거·6표×7권한·다른
SECURITY DEFINER 차단·퇴직/만료/회수/근거없음을 실제로 거절받아 본다.

### 9. 한 번 띄워 보기 (아직 enable 아님)

```bash
sudo install -m 0644 /opt/tybot/deploy/tybot-vipbot.service \
  /etc/systemd/system/tybot-vipbot.service
sudo systemctl daemon-reload
sudo systemctl start tybot-vipbot
sudo journalctl -u tybot-vipbot -n 50 --no-pager
```
`VIPBot 기동 ws=… team=… bot=… model=…` 한 줄이 나오면 신원 세 곳이 맞은 것이다.
틀리면 `vipbot_identity_mismatch` 로 **붙기 전에** 멎는다.

### 10. 부팅에 걸기 — 마지막

유닛 끝에 더한다:
```ini
[Install]
WantedBy=multi-user.target
```
그리고 `tests/test_vipbot_runtime_setup.py` 의
`test_the_draft_cannot_be_enabled_until_the_runtime_module_exists` 를 고친다.
**그 시험을 고치는 것이 「켜기로 했다」 를 저장소에 남기는 방법이다.**

```bash
sudo systemctl enable --now tybot-vipbot
```

## 되돌리기

```bash
sudo systemctl disable --now tybot-vipbot
```
멈추면 끝이다. 아카이브에 쓴 것이 없고(읽기 전용), 수집도 안 했다. 남는 것은
`/var/lib/tybot-vipbot/` 의 비용 원장과 이벤트 중복 제거 DB 뿐이다.

## 시험

114건 + skip 8(파일 모드·소유자는 posix 에서만). DB·Slack·systemctl **0회**.

**돌연변이 30종 전부 잡힘**(생존 0). 다섯 계약을 각각 무너뜨려 봤다.

| 계약 | 무너뜨린 방법 |
|---|---|
| 비밀 비출력 | 사유에 파일 내용 넣기 · `render` 가 자격증명 묶음을 통째로 찍기 · DSN 을 stdout 으로 · API 키를 stdout 으로 · API 키를 로그로 · 키 값을 사실 묶음에 저장 |
| 읽기 전용 | `chmod` · 자격증명 파일 다시 쓰기 · 빌린 `os.environ` 을 안 되돌리기 |
| 변경 금지 | `GRANT` · `INSERT` · `DROP` · `systemctl start` · `auth_test` 호출 |
| 설정 파일 경계 | 허용 목록을 금지 목록으로 · 빠진 키 무시 · 사유에 값까지 붙이기 · 아무 `EnvironmentFile` 이나 허용 · 유닛 하드코딩 무시 · 설정 파일 미지정 무시 · 유닛이 값을 박음 · 유닛이 공용 env 를 들임 |
| 권한 질의의 정확성 | `has_function_privilege('public', …)` 로 되돌리기 · `acldefault` 누락(NULL proacl) · `grantee` 를 0 아닌 값으로 · 멤버십 질의를 child 방향만 · 판정에서 `members` 방향 제거 · ADMIN OPTION 무시 · 설정 파일 소유자 검사 제거 · 모드 마스크를 쓰기 비트만 |

### 2026-11-06 리뷰에서 나온 세 결함

| 결함 | 왜 조용했나 |
|---|---|
| `has_function_privilege('public', …)` | **PUBLIC 은 `pg_roles` 의 행이 아니다.** 그 호출은 터지거나, 안 터지면 엉뚱한 역할을 물어본 것이다. 어느 쪽이든 PUBLIC 이 열려 있어도 보고하지 않는다. 이제 `aclexplode(COALESCE(proacl, acldefault('f', proowner)))` 에서 `grantee=0 AND privilege_type='EXECUTE'` 를 직접 본다 |
| 멤버십을 child 방향만 | 「우리 역할이 누구의 멤버인가」 만 보였다. **그룹에 낯선 역할이 들어와도**, definer 에 멤버가 생겨도 안 보인다. 그 역할들은 창구를 실행할 수 있다. 이제 양방향 + `admin_option` 을 모아 group→runtime 하나·definer 멤버 0·ADMIN OPTION 0 을 강제한다 |
| 설정 파일을 모드만 검사 | `tybot` 소유 0600 이 통과했다. **쓰기 비트가 없어도 소유자면 `chmod` 한 줄로 바꾼다** — 봇이 자기 근거 범위와 예산을 고칠 수 있다. 이제 `st_uid != 0` 을 보고, 모드 마스크도 `0o022`(쓰기만) 에서 `0o027` 로 좁혀 0644 를 막는다 |

첫 회차에서 **셋이 살아남았다** — DSN·API 키를 `print` 하거나 로그에 찍는 돌연변이,
그리고 `render` 가 구조를 통째로 찍는 돌연변이. 그때의 시험은 「출력에 비밀이
없다」 만 봤는데, 사실 묶음에 값이 없으니 통과해 버렸다.

고친 방향이 중요하다 — 「비밀이 없다」 가 아니라 **「아무것도 안 찍는다」** 로
바꿨다. `credential_facts` 가 도는 동안 stdout·stderr·로그가 **완전히 비어
있어야** 한다. 값을 쥐었다 버리는 자리라, 한 줄이라도 찍히면 그게 유출이다.

판정(`evaluate`)은 **순수 함수**라 사실 묶음을 손으로 만들어 전 분기를 돌린다.
바깥 세계를 만지는 것은 `unit_facts`·`credential_facts`·`settings_facts`·
`database_facts` 넷뿐이고, 앞의 셋은 임시 디렉터리로 덮었다.

「읽기 전용」 은 설명이 아니라 시험이다 — `systemctl`·`subprocess`·`os.system`·
`auth_test`·`chmod(`·`unlink(` 과 7개 쓰기 SQL 문이 **코드에 없음**을 주석과
docstring 을 걷어낸 뒤 확인한다.

## 아직 못 한 것

1. **서버에서 한 번도 안 돌렸다.** 개발 PC 에는 PostgreSQL 이 없어
   `--skip-db` 경로만 실행했다. DB 분기(`database_facts`)는 시험에서 대역으로만
   덮였고 **진짜 질의는 미실행**이다
2. ~~격리 DB 65건 미실행~~ → **2026-11-06 PostgreSQL 18 에서 65 passed**
   ([기록](2026-11-06-vipbot-runtime-role-db.md)). 권한 경계는 이제 설계가 아니라
   **실제로 거절받아 본 사실**이다
3. **유닛 설정 3줄은 안 넣었다.** 활성화 4단계이고, 그건 활성화 결정이 난 뒤의
   일이라 점검이 가리키기만 한다
