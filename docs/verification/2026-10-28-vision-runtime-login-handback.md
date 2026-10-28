# B-81 Vision 전용 LOGIN 계정과 자격 설치 경계 (2026-10-28)

Claude 구현분. **커밋하지 않았다.** 독립 리뷰·수정·전체 검증·커밋은 Codex.

정책은 OFF 그대로다. 모델 호출·원본 조회·운영 DB 변경은 하지 않았다.
worker 서비스·유닛·포트는 만들지 않았다. 카탈로그·approval·clearance 행도 만들지 않았다.

| 파일 | 내용 |
|---|---|
| `deploy/setup-tybot-vision-runtime.sh` | 접속 계정 프로비저닝 (기본 계획 모드) |
| `src/tybot/console/vision_dsn.py` | Vision 전용 DSN 해석. 폴백 없음 |
| `scripts/check_vision_runtime.py` | 읽기 전용 대조기 |
| `tests/test_setup_tybot_vision_runtime.py` | 28건 |
| `tests/test_vision_dsn.py` | 36건 (POSIX 전용 8건은 개발 PC 에서 skip) |
| `tests/test_check_vision_runtime.py` | 60건 |

기존 Codex 파일 중 고친 것은 **`src/tybot/console/image_description_admin.py` 의
`_connect()` 한 곳뿐**이다(요구사항 2). env 직접 읽기를 `vision_dsn` 호출로 바꿨다.

작업 중 Codex 가 올린 `image_description_clearance_admin.py` 는 같은 `_connect()` 를
부르므로 **고치지 않아도 같은 경계를 쓴다.** 그 파일은 건드리지 않았다.

---

## 1. 왜 역할이 둘인가

`tybot_vision` 은 NOLOGIN 그룹이다. 2026-10-28 운영 DB 확인에서 **멤버가 0 명**이었다 —
권한은 다 붙어 있는데 그 권한을 행사할 주체가 없다. 지금은 그것이 가장 닫힌 상태라
맞지만, 활성화하려면 접속할 누군가가 필요하다.

그 누군가를 그룹 자체로 만들지 않는다. 하나로 합치면 **권한을 쥔 역할이 곧 접속
자격을 갖는다.** 그러면 비밀번호를 바꾸거나 계정을 끊는 일이 권한 구조를 건드리는
일이 되고, 운영 중에는 그것을 하기 어려워진다. 둘로 나누면 계정은 끊어도 권한
선언은 그대로다.

```
tybot_vision          NOLOGIN  ← 권한이 붙는 자리 (스키마가 선언)
      ▲ 멤버십 1건 · ADMIN OPTION 없음
tybot_vision_runtime  LOGIN INHERIT, 그 밖은 전부 NO…  ← 접속하는 자리
```

### 속성을 만들 때와 고칠 때 같은 목록으로 쓴다

```bash
ATTRS="LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
CREATE ROLE $ROLE_RUNTIME $ATTRS   # 없으면 만든다
ALTER  ROLE $ROLE_RUNTIME $ATTRS   # 있으면 모양을 맞춘다
```

두 줄이 같은 변수에서 온다. 손으로 만든 역할이 섞여 있어도 이 스크립트를 돌리면
같은 모양이 된다 — 멱등이 「두 번 돌려도 안 깨진다」 를 넘어 **「어떤 상태에서
돌려도 계약대로 끝난다」** 가 된다.

`INHERIT` 가 요구사항에 있는 이유도 적어 둔다. `NOINHERIT` 면 그룹 권한을 쓰려면
`SET ROLE` 이 필요한데 코드는 그것을 하지 않는다. 권한이 **「있는데 안 되는」**
상태가 되고, 권한 목록만 보는 검사는 그것을 통과시킨다.

### 기존 역할은 **바꾸기 전에** 판정한다 (2026-10-28 리뷰 반영)

처음 판에는 순서 결함이 있었다. `ALTER ROLE … LOGIN` 을 먼저 돌리고 멤버십·속성은
나중에 검증했다. 역할이 이미 있고 **비밀번호도 이미 있으면**, 검증이 실패하는
동안에도 과한 권한으로 로그인할 수 있는 창이 열린다.

지금은 `CREATE`·`ALTER`·`GRANT` 가 한 줄도 돌기 전에 본다.

| 기존 상태 | 결과 |
|---|---|
| `tybot_vision` 외 다른 그룹의 멤버 | **거절** — 자동으로 떼지 않는다 |
| `tybot_vision` 에 ADMIN OPTION | **거절** |
| SUPERUSER·CREATEDB·CREATEROLE·REPLICATION·BYPASSRLS 중 하나라도 | **거절** |
| 멤버십이 0건 | 통과 — `--apply` 가 중간에 끊긴 뒤 다시 돌리는 경우다 |

고치지도 않는다. 추가 멤버십을 떼거나 속성을 낮추면 **남이 다른 용도로 만든
역할을 말없이 빼앗는** 일이 된다. 무엇이 걸렸는지 말하고 멈춘다.

`--set-password` 도 같은 검토 뒤에 있다. 과한 권한을 가진 역할에 비밀번호를
붙이는 것이 바로 그 노출이기 때문이다.

> 앞 판의 「어떤 상태에서 돌려도 계약대로 끝난다」 는 **더 이상 맞지 않는다.**
> 깨끗하거나 이 스크립트가 만든 역할이면 수렴하고, 그 밖이면 멈춘다.

### 비밀번호가 이 스크립트를 지나가지 않는다

인자로 받으면 `ps` 와 shell 이력에 남는다. 그래서 받지 않는다.

```bash
sudo /opt/tybot/deploy/setup-tybot-vision-runtime.sh --set-password
# → psql 의 \password 를 띄운다. 입력값은 psql 이 클라이언트에서 SCRAM 해시로 바꿔 보낸다
```

**정직하게 적는다**: `log_statement=all` 이 켜져 있으면 서버 로그에
`ALTER ROLE … PASSWORD 'SCRAM-SHA-256$…'` 이 남는다. 평문은 아니지만 **해시는 남는다.**
`\password` 가 막는 것은 평문 노출과 프로세스 목록·이력이지 로그 자체가 아니다.

### 기본이 계획 모드다

```bash
/opt/tybot/deploy/setup-tybot-vision-runtime.sh          # 아무것도 바꾸지 않는다
```

인자를 잊은 사람이 운영 DB 를 바꾸면 안 된다. 계획 모드는 root 도 psql 도 쓰지
않고, 돌릴 SQL 을 그대로 보여 주고 끝난다. 시험이 그 블록에 `psql` 이 한 번도
없다는 것을 소스로 못 박는다.

### 대상 확인에 `current_setting('port')` 을 쓴다

`inet_server_port()` 는 **유닉스 소켓 접속에서 NULL** 이다. 그걸로 대조하면
`NULL != '55432'` 가 아니라 비교 자체가 NULL 이 되어 **늘 통과**한다. 운영 DB 는
소켓으로 붙으므로 이 함정은 가정이 아니다.

---

## 2. DSN 은 전용 값만 쓴다

### 폴백을 코드에서 없앴다

콘솔은 이미 `DATABASE_URL` 을 들고 있다. 그 값으로 B-81 표를 읽으면 화면은
정상으로 보이고, 그 순간 Master 권한으로 Vision 표를 만지는 길이 생긴다.
`tybot_vision` 을 따로 둔 이유가 통째로 사라지는데, **그 사실은 화면이 정상인
동안 드러나지 않는다.**

```python
resolve_vision_database_url()
# 1. TYBOT_VISION_DATABASE_URL            (값 직접)
# 2. TYBOT_VISION_DATABASE_URL_FILE       (경로 명시)
# 3. CREDENTIALS_DIRECTORY/tybot-vision-database-url   (systemd LoadCredential)
# 없으면 예외. DATABASE_URL 로 내려가지 않는다.
```

여기에 둘을 더 걸었다.

**①** 해석된 값이 `DATABASE_URL` 과 같으면 거절한다
(`vision_database_shared_credential`). 복사해 붙인 경우는 접속이 성공하므로 어떤
검사도 안 걸린다. 값이 같다는 사실만이 그것을 잡는다.

**②** 문자열 비교로는 부족하다. `DATABASE_URL` 이 비어 있거나 달라도 **다른
관리자 계정으로 붙는 DSN** 이면 전용 역할을 만든 의미가 없다. 그래서 DSN 을
구조로 파싱해 접속 user 가 `tybot_vision_runtime` 인지 본다
(`vision_database_wrong_role`).

> **실측으로 잡은 구멍 (2026-10-28).** userinfo 만 보면 부족하다.
>
> ```
> postgresql://tybot_vision_runtime@host/db?user=postgres
> ```
>
> libpq 는 URI 를 키워드로 펼칠 때 userinfo 를 먼저 넣고 **질의 문자열로
> 덮어쓴다.** 위 DSN 은 실제로 `postgres` 로 접속하는데, 첫 판에서는 **통과했다.**
> 지금은 질의 쪽을 나중에 읽어 libpq 와 같은 순서로 덮는다. `user=` 가 여러 번
> 나오면 마지막이 이긴다 — 이것도 libpq 와 같다.

`host=… user=…` **키워드 형식은 받지 않는다.** libpq 는 받지만 형식마다 파서를
따로 두면 둘 중 하나만 고쳐지는 날이 오고, 그때 조용히 통과하는 쪽이 생긴다.
설치 안내도 URI 하나만 적고, 그 예시가 실제로 통과하는지 시험이 확인한다.

파싱이 실패하면 예외를 흘리지 않고 `wrong_role` 로 거절한다 — 흘리면 콘솔이
503 이 아니라 500 을 낸다.

### 코드 다섯을 가른다

| 코드 | 뜻 | 고칠 자리 |
|---|---|---|
| `vision_database_unconfigured` | 아무것도 설정 안 됨 (기존 계약 유지) | 설정을 넣는다 |
| `vision_database_credential_mode` | 파일에 group/other 권한이 있다 | `chmod 0600` |
| `vision_database_credential_unreadable` | 명시한 경로를 못 읽거나 내용이 비었다 | 경로·내용 |
| `vision_database_shared_credential` | Master/Archiver 와 같은 DSN | 전용 계정으로 바꾼다 |
| `vision_database_wrong_role` | 접속 user 가 `tybot_vision_runtime` 이 아니다 | DSN 의 user |

「없음」 과 「잘못됨」 을 같은 코드로 보내면 운영자가 고칠 자리를 못 찾는다.
다만 **`CREDENTIALS_DIRECTORY` 안에 우리 파일이 없는 것**은 `unconfigured` 로 둔다 —
systemd 는 그 디렉터리를 늘 주므로, 파일이 없다는 것은 아직 안 넣었다는 뜻이지
설정이 틀렸다는 뜻이 아니다. 경로를 **명시**했는데 없으면 그때는 `unreadable` 이다.

### 값도 경로도 밖으로 내보내지 않는다

오류는 ASCII 고정 코드만 들고 있다. 여기서 실측으로 하나 배웠다.

> `raise … from None` 은 `__cause__` 만 지운다. **`__context__` 에는 원래
> `OSError` 가 그대로 남고, 그 객체의 `filename` 이 경로다.** 트레이스백 출력은
> `__suppress_context__` 를 존중하지만 예외 체인을 따라가는 로거는 그렇지 않다.
>
> 그래서 `_read_file` 은 코드를 모아 두었다가 **`except` 블록 밖에서** 던진다.
> 활성 예외가 없으므로 `__context__` 도 `None` 이다. 시험이 그것을 건다.

모듈은 로그를 남기지 않는다. 남길 만한 것이 값과 경로뿐이기 때문이고, 시험이
소스에 `logging`·`print(` 가 없는 것까지 센다.

### POSIX 권한 검사는 분리했다

Windows 는 group/other 비트가 뜻을 갖지 않는다 — 늘 `0o666` 류라 거기서 재면
**개발 PC 에서만 빨개지는 검사**가 된다. 권한 검사는 `os.name == "posix"` 에서만
돌고, 그 시험 8건은 개발 PC 에서 이유와 함께 skip 된다.

한 가지는 플랫폼과 무관하게 돌려야 한다 — **파일이 있는지**다. 처음엔 플랫폼
분기를 앞에 두는 바람에 Windows 에서 `stat()` 을 건너뛰었고, 「없음」 이
「못 읽음」 으로 나왔다(2026-10-28 실측, 시험이 잡았다). 지금은 `stat()` 을 먼저
부르고 **비트만** 플랫폼으로 가른다.

---

## 3. 대조기 — 「권한 정상」 과 「쓸 주체가 있다」 를 가른다

`check_schema_drift.py` 는 `tybot_vision` 에 권한이 붙었나를 본다. 그 그룹은
NOLOGIN 이고 멤버가 0 명이었는데 검사기는 `exit 0` 이었다. 그 차이가
`scripts/check_vision_runtime.py` 다.

```bash
sudo -u tybot env TYBOT_ENV_FILE=/etc/tybot/tybot.env \
  /opt/tybot/.venv/bin/python /opt/tybot/scripts/check_vision_runtime.py

# 활성화 직전 — 계정이 없으면 「준비 상태」 가 아니라 실패다
… /opt/tybot/scripts/check_vision_runtime.py --require-login
```

| 보는 것 | 문제 코드 |
|---|---|
| 그룹 존재·NOLOGIN | `group_role_missing` · `group_role_can_login` |
| 접속 계정 속성 | `runtime_cannot_login` · `runtime_does_not_inherit` · `runtime_has_*` |
| 멤버십 정확히 1건 | `runtime_membership_mismatch` · `runtime_membership_admin_option` |
| Master/Archiver 가 Vision 표 권한을 얻었나 | `foreign_grant:<역할>:<표>:<권한>` |
| PUBLIC 이 얻었나 | `public_grant_present` |
| 계약된 권한을 **실제로 상속**하나 | `not_inherited:<표>:<권한>` |
| clearance 는 철회 3칸만 | `clearance_table_update_granted` · `clearance_update_columns_too_wide` · `…_missing` |

### 「준비 상태」 와 「실패」 를 가른다

접속 계정이 아직 없는 것은 **지금은 정상**이다. 정책이 OFF 이고 worker 도 없다.
그러나 활성화를 하러 와서 같은 상태를 보면 그것은 실패다. 둘을 같은 색으로
보고하면 운영자가 활성화를 「됐다」 로 읽는다. `--require-login` 이 그 선이다.

그룹 쪽 문제는 **접속 계정 유무와 무관하게** 본다. 계정이 없다고 넘어가면 그룹이
LOGIN 으로 바뀐 사고를 활성화 날까지 못 본다.

### 권한 목록을 베끼지 않는다

`check_schema_drift` 의 `VISION_TABLES`·`VISION_REQUIRED`·`VISION_COLUMN_REQUIRED` 를
그대로 가져온다. 베껴 적으면 한쪽만 고쳐지는 날이 오고, **갈라진 검사는 없는
안전을 보고한다.** 시험이 두 모듈이 같은 객체를 보는지(`is`) 확인한다.

---

## 4. 운영자가 실행할 명령

순서대로다. **각 단계는 다른 완료다.**

```bash
# 0) 무엇을 할지만 본다 (DB 에 접속하지 않는다)
/opt/tybot/deploy/setup-tybot-vision-runtime.sh

# 1) 접속 계정을 만든다 — 비밀번호는 아직 없다(= 아직 접속 불가)
sudo /opt/tybot/deploy/setup-tybot-vision-runtime.sh --apply

# 2) 비밀번호. 대화형이고 인자로 받지 않는다
sudo /opt/tybot/deploy/setup-tybot-vision-runtime.sh --set-password

# 3) DSN 을 0600 파일로 둔다. 값은 사람이 적는다
sudo install -o tybot -g tybot -m 0600 /dev/null /etc/tybot/tybot-vision-database-url
sudo -u tybot vi /etc/tybot/tybot-vision-database-url

# 4) 콘솔 서비스에 넘긴다 — 둘 중 하나
#    LoadCredential=tybot-vision-database-url:/etc/tybot/tybot-vision-database-url
#    TYBOT_VISION_DATABASE_URL_FILE=/etc/tybot/tybot-vision-database-url

# 5) 확인
sudo /opt/tybot/deploy/setup-tybot-vision-runtime.sh --verify
sudo -u tybot env TYBOT_ENV_FILE=/etc/tybot/tybot.env \
  /opt/tybot/.venv/bin/python /opt/tybot/scripts/check_vision_runtime.py
```

5번이 `✅` 라도 **worker 는 없고 정책은 OFF 다.** 계정이 선 것뿐이다.

---

## 5. 시험 124건 · Ruff

| 파일 | |
|---|---|
| `tests/test_setup_tybot_vision_runtime.py` | 28 passed |
| `tests/test_vision_dsn.py` | 28 passed · 8 skipped (POSIX 전용) |
| `tests/test_check_vision_runtime.py` | 60 passed |

`python -m ruff check src tests scripts` **0건**. 기존 콘솔·드리프트 시험
32건도 그대로 통과한다.

### 설치 스크립트 시험은 **소스**를 센다 — 한계를 적어 둔다

`--apply`·`--verify`·`--set-password` 는 root 와 psql 이 있어야 돈다. `$EUID` 는
bash 에서 읽기 전용이라 우회할 수 없고, 우회로를 만드는 것은 이 스크립트가
막으려는 것을 스스로 여는 일이다. 그래서 실행으로 재는 것은 **계획 모드뿐**이고,
사전 검토는 소스로 건다 — 블록이 `--apply` 보다 **앞에 있는가**,
`psql_run` 이 한 번도 없는가, 다섯 속성을 다 보는가, 거절 문구가 있는가.

같은 판정 로직의 **동작 시험**은 `check_vision_runtime.evaluate` 쪽에 있다.
그쪽은 DB 없이 돌고 과권한·추가 멤버십·ADMIN OPTION 을 실제로 판정한다.
운영 DB 에서 실제로 거절하는지는 서버에서 한 번 확인해야 한다(§6).

시험이 거는 것들:
- 비밀번호·DSN 이 stdout·stderr·예외·`repr`·트레이스백에 없다
- 잘못된 DB/포트 거절 (소켓에서도 작동하는 비교로)
- `tybot_vision` 이 LOGIN 이면 거절
- 접속 계정에 superuser/createdb/createrole/replication/bypassrls 가 있으면 거절
- 멤버십 누락·과다·ADMIN OPTION 거절
- 자격 파일 `0600`/`0400` 허용, group/other 비트가 있으면 거절
- Windows 에서는 POSIX 권한 검사를 skip 하고, 그 사실을 이유와 함께 남긴다
- 계획 모드가 DB 를 건드리지 않는다 (실행 + 소스 양쪽)
- 사전 검토가 `--apply`·`--set-password` **앞에** 있고 읽기만 한다
- 추가 멤버십·ADMIN OPTION·과권한 기존 역할을 거절하고, 멤버십 0건은 거절하지 않는다
- `?user=` 로 덮어쓴 DSN 거절, 맞는 계정으로 덮어쓴 것은 허용, 파싱 실패도 거절
- 설치 안내가 보여 주는 DSN 예시가 실제로 통과한다

---

## 6. 미검증 · 남은 것

- **운영 DB 에서 `--apply` 를 돌리지 않았다.** 계획 모드와 단위시험만 돌렸다.
  실제 역할 생성·권한 상속은 서버에서 확인해야 한다
- **사전 검토가 실제로 거절하는지 서버에서 안 봤다.** 개발 PC 에서는 root·psql 이
  없어 계획 모드만 돈다. 활성화 전에 격리 DB 에서 한 번 재현하는 것이 맞다 —
  일부러 `tybot_vision_runtime` 을 다른 그룹 멤버로 만들어 두고 `--apply` 가
  거절하는지, 그때 `ALTER ROLE` 이 **돌지 않았는지**(`rolcanlogin` 이 그대로인지)
- **콘솔 화면이 없다.** 운영 기능은 콘솔에 둔다(CLAUDE.md). 지금 이 절차는
  SSH 전용이라 「쓸 수 있는 사람이 한 명」 이다. WC-30 에 자격 상태 표시를
  붙이는 것이 다음이다 — 값은 보이지 않고 **준비/미준비와 코드만** 보인다
- **DB 레벨 `CONNECT` 는 건드리지 않았다.** PostgreSQL 은 `CONNECT` 를 PUBLIC 에
  기본 부여하므로, 새 LOGIN 역할은 `tyslackai` 에 붙을 수 있다. 표 권한은 없다.
  이것을 좁히려면 Master/Archiver 까지 영향이 가므로 이번 범위 밖으로 두었다 —
  **결정이 필요한 항목**이다
- `tybot-console.service` 에 `LoadCredential=` 을 아직 넣지 않았다. 유닛 수정은
  worker 경계와 섞이지 않도록 Codex 판단에 맡긴다

---

## 7. 별도 제안 — `clearance_code` 를 `reserve` 전에 떼는 편이 맞다

**이번 작업과 섞지 않았다. 코드는 그대로다.** 제안만 적는다.

지금 managed batch 는 `checks` 에 `clearance_code` 를 실어 `store.reserve` 와
`store.begin_dispatch` 로 넘긴다. 받는 쪽 계약이 둘로 갈라져 있다.

| 경로 | 모르는 키 |
|---|---|
| `image_description_runner._checks` | `ValueError` — "`pii_blocked=False` 를 호출자가 아닌 곳에서 받는 길을 막는다" |
| `ImageDescriptionStore._checks` | 화이트리스트 4키만 집고 **조용히 버린다** |

지금 샘은 없다. `clearance_code` 는 preflight 에 닿지 않고 DB 에도 안 들어간다.
문제는 **엄격 가드가 선언한 의도가 managed batch 경로에는 적용되지 않는다**는
것이다. 나중에 누가 `checks` 에 다른 것을 실어도 조용히 지나간다.

제안: 코디네이터가 `_fresh_check_blocker` 로 판정을 끝낸 **직후** `clearance_code`
를 떼고, `reserve` 에는 `CALLER_CHECKS` 만 넘긴다. 그러면 store 쪽 `_checks` 를
엄격하게 바꿔도 managed batch 가 깨지지 않는다 — 두 경로의 계약이 같아진다.

판단은 Codex 몫이다. 고치려면 `image_description_coordinator.py` 한 곳이면 된다.
