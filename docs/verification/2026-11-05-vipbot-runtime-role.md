# B-83 VIPBot 전용 DB 계정과 서비스 경계

작성 2026-11-05 · 신규 4 + 이 문서. **Codex 변경 파일 수정 0.** 커밋하지 않았다.

| 파일 | |
|---|---|
| `deploy/setup-tybot-vipbot-runtime.sh` | LOGIN 역할 설치·검증 |
| `deploy/tybot-vipbot.service` | systemd 유닛 **초안**(`[Install]` 없음) |
| `tests/test_vipbot_runtime_setup.py` | 28건, DB 없이 |
| `tests/test_vipbot_runtime_role_db.py` | 18건, **실제 PostgreSQL 필요** |

Codex 의 `vipbot_dsn.py` · `vipbot_runtime_store.py` · `bot_connection_schema.sql` ·
`apply-schema.sh` 는 읽기만 했다.

> **설치 스크립트는 지금 둘이 같이 고치고 있다.** Codex 가 같은 파일에 접속 host
> 고정(`TYBOT_DB_HOST`)과 그룹 멤버 검토를 넣었고, 계약 시험
> `tests/test_setup_tybot_vipbot_runtime.py` 를 따로 썼다. 그쪽 계약에 맞춰
> 세 가지를 더했다 — 그룹이 **다른 역할의 멤버**인지, 토큰 표에 **직접 GRANT** 가
> 남았는지(`role_table_grants`), 함수 ACL 에 **PUBLIC 줄**이 있는지
> (`acl.grantee = 0`). 두 시험 파일은 겹치지만 보는 각도가 달라 그대로 둔다 —
> 합치는 것은 한 사람이 할 일이다.

## 역할을 나눈 이유

`tybot_vipbot` 은 **NOLOGIN 그룹**이다. 권한만 들고 접속하지 않는다. 접속하는
주체는 `tybot_vipbot_runtime` 이고 그룹의 멤버로서 권한을 상속받는다.

하나로 합치면 권한을 쥔 역할이 곧 접속 자격을 갖는다. 그러면 비밀번호를 바꾸거나
계정을 끊는 일이 **권한 구조를 건드리는 일**이 되고, 운영 중에 그것을 하기 어렵다.

`tybot_vipbot_definer` 는 두 `SECURITY DEFINER` 함수만 소유하는 세 번째 NOLOGIN
역할이다. 로그인할 수 없고 멤버도 없으며, 여섯 기반 표의 SELECT만 갖는다. 함수
소유자를 `postgres`로 두지 않아 함수가 불필요하게 superuser 권한으로 실행되지 않게
한다. 함수 소유자와 역할 속성·멤버십은 설치 검증에서 다시 대조한다.

## 이 계정의 특권 창구

`vipbot_connection_config(text)`와 `vipbot_authorization(text,text)` 두
`SECURITY DEFINER` 함수만 허용한다. 다른 `public` 보안 정의자 함수의 실효 실행
권한과 기반 표의 일곱 권한은 모두 설치·기동 때 거절한다.

첫 함수는 검증된 VIPBot 토큰 쌍만 반환한다. 두 번째 함수는 기존 `user_identity`
매핑, 살아 있는 개인 권한, 등록된 VIP 근거 채널 좌표만 반환한다. 거절된 요청에는
근거 채널 좌표를 반환하지 않는다.

`bot_connection` · `bot_connection_secret` · `employee` · `user_identity` ·
`bot_access` · `bot_source_channel` 직접 접근은 안 된다. 앞의 두 표에는
**모든 봇의 토큰 암호문**이 있다. VIPBot 런타임이 표를 읽을 수 있으면 VIPBot 한
줄의 버그가 Master·Archiver·전문봇 토큰까지 꺼낸다. 함수는 `SECURITY DEFINER` 로
그 한 줄만 돌려주고 조건(`bot_key='vipbot'` · `state='enabled'` · `identity_ok`)은
함수 안에 박혀 있어 호출자가 못 바꾼다.

## 남의 역할을 넘겨받지 않는다

역할이 이미 있으면 **아무것도 바꾸기 전에** 본다.

| 걸리는 것 | 조치 |
|---|---|
| `tybot_vipbot` 말고 다른 그룹 멤버십 | 거절·중단 |
| ADMIN OPTION 이 붙은 멤버십 | 거절·중단 |
| SUPERUSER·CREATEDB·CREATEROLE·REPLICATION·BYPASSRLS | 거절·중단 |

**자동으로 떼거나 낮추지 않는다.** 남이 다른 용도로 만든 역할을 말없이 빼앗는
일이 되기 때문이다. 무엇이 걸렸는지 말하고 멈춘다.

순서도 이유가 있다 — 검증보다 `ALTER ROLE … LOGIN` 이 먼저 돌면, 검증이 실패하는
동안에도 **과한 권한으로 로그인할 수 있는 창**이 열린다. 사전 검토를 통과하기
전에는 `CREATE`·`ALTER`·`GRANT` 가 한 줄도 돌지 않는다(시험이 순서를 고정한다).

## 확인은 「실제로 무엇을 할 수 있나」 를 본다

`--verify` 가 역할·멤버십만 보고 끝내지 않는다. 상속 경로가 끊겨 있으면 역할은
맞는데 런타임이 기동에서 실패하고, 그 실패는 「권한 정상」 보고 **뒤에** 온다.

```
has_function_privilege(runtime, public.vipbot_connection_config(text), EXECUTE)  → t
has_function_privilege(runtime, public.vipbot_authorization(text,text), EXECUTE) → t
has_table_privilege(runtime, 여섯 기반 표, 7개 표 권한)                    → f
has_function_privilege(public, 두 VIPBot 함수, EXECUTE)                    → f
has_function_privilege(runtime, archiver_connection_config(text), EXECUTE) → f
다른 public SECURITY DEFINER 함수의 실효 EXECUTE                           → 0건
information_schema.role_table_grants (여섯 표 · runtime·group·PUBLIC)      → 0행
pg_proc.proacl 의 grantee = 0 (PUBLIC)                                     → 0건
```

위 넷은 **쓸 수 있는가**(상속 포함), 아래 둘은 **직접 준 적이 있는가**다. 직접
GRANT 가 남아 있으면 그룹에서 떼어 내도 권한은 그대로 남는다.

`proacl` 이 `NULL` 이면 「아무에게도 안 줬다」 가 아니라 **기본값**이고, 함수의
기본값은 `EXECUTE TO PUBLIC` 이다. 그래서 NULL 을 「PUBLIC 있음」 으로 센다 —
누군가 `CREATE OR REPLACE FUNCTION` 만 다시 돌리고 `REVOKE` 를 빠뜨리면 바로 그
상태가 되는데, ACL 줄만 세는 검사는 0건으로 통과한다.

## systemd 유닛 — 시크릿은 자격증명으로만

```
LoadCredential=tybot-vipbot-database-url:/etc/tybot/tybot-vipbot-database-url
LoadCredential=tybot-workspace-secret-key:/etc/tybot/workspace-secret.key
Environment=WORKSPACE_SECRET_KEY_FILE=%d/tybot-workspace-secret-key
```

**`/etc/tybot/tybot.env` 를 읽지 않는다.** 그 파일에는 Master 의 `DATABASE_URL` 이
있다. 이 프로세스가 그 값을 들고 있으면 전용 계정을 만든 의미가 사라진다 —
코드 한 줄이면 모든 봇의 토큰 표를 열 수 있고, 그때 `tybot_vipbot_runtime` 은
그냥 쓰이지 않는 역할이 된다.

DSN 은 환경변수로도 넘기지 않는다. `tybot.vipbot_dsn` 이 `CREDENTIALS_DIRECTORY`
에서 `tybot-vipbot-database-url` 이름 그대로 찾는다(2026-11-05 Codex 수정으로
`VIPBOT_DATABASE_URL` 환경변수 경로가 사라졌다 — 이 유닛은 그 뒤의 계약에 맞다).

**`[Install]` 이 없다.** `systemctl enable` 이 거부된다. 런타임 모듈
(`tybot.vipbot_runtime`)이 아직 없는데 부팅에 걸어 두면 재시작만 반복하고 남는
것은 「켜 두었다」 는 기억뿐이다. `ExecStartPre` 가 그 모듈을 import 해 보고,
실패하면 이유가 journal 에 남는다. 모듈이 서는 날 시험이 먼저 깨져서 알려 준다.

`install.sh` 는 유닛을 **이름으로 하나씩** 설치한다. 이 초안은 목록에 없으므로
자동으로 깔리지 않는다(시험이 고정).

아카이브는 `ReadOnlyPaths` 다 — VIPBot 은 수집·백필·첨부 보관 주체가 아니다.

## 시험

**28건(DB 없이)** — 설치 절차와 유닛의 모양. 돌연변이 11종 전부 잡힘(생존 0):
ADMIN OPTION 부여, 사전 검토에서 자동 수정, 속성 목록에 SUPERUSER, 표 권한 검사
축소, PUBLIC 검사 제거, 계획 모드를 root 검사 뒤로, 유닛이 master env 를 읽기,
자격증명 이름 오타, `[Install]` 추가, 아카이브 쓰기 허용, 유닛에 DSN 박기.

**18건(실제 PostgreSQL)** — `TEST_DATABASE_URL` 이 있어야 돈다. 역할을 만들고
`apply-schema.sh` 의 파일 목록을 그 순서대로 적용한 뒤, `SET ROLE` 로 **그 계정이
되어** 돌린다. 비밀번호 없이 진짜 거부를 받아 보는 유일한 길이다.

- 함수 실행 → 1행
- `bot_connection{,_secret}` SELECT → `InsufficientPrivilege`
- PUBLIC·그룹 밖 역할 → 실행 거부
- Master 연결을 같은 워크스페이스에 넣어도 VIP 창구에서 안 나옴
- `identity_ok=false` · `state='disabled'` → 0행
- **스키마 재적용 후 권한 스냅샷 동일**(드리프트 없음)

> ⚠️ **이 18건은 이 세션에서 돌리지 못했다.** 개발 PC 에 PostgreSQL 이 없고
> `TEST_DATABASE_URL` 도 없다(skip). 서버의 폐기 가능한 DB 에서 돌려야 한다:
>
> ```bash
> cd /opt/tybot && sudo -u tybot env TEST_DATABASE_URL='postgresql://…/tyslackai_test' \
>   /opt/tybot/.venv/bin/python -m pytest tests/test_vipbot_runtime_role_db.py -q \
>   -p no:cacheprovider
> ```
>
> **운영 DB 를 쓰지 않는다.** 시험이 `verify_schema_isolated.operational_db_refusal`
> 로 한 번 더 막지만, 이름부터 시험용이어야 한다.

| | 값 |
|---|---|
| 날짜·사람 | *(미기입)* |
| PostgreSQL 버전 | *(미기입)* |
| pass/skip | *(미기입)* |

## 순서

```bash
sudo /opt/tybot/deploy/apply-schema.sh                      # 그룹·함수·GRANT
/opt/tybot/deploy/setup-tybot-vipbot-runtime.sh             # 계획만 본다
sudo /opt/tybot/deploy/setup-tybot-vipbot-runtime.sh --apply
sudo /opt/tybot/deploy/setup-tybot-vipbot-runtime.sh --set-password
sudo install -o tybot -g tybot -m 0600 /dev/null /etc/tybot/tybot-vipbot-database-url
sudo -u tybot vi /etc/tybot/tybot-vipbot-database-url       # DSN 을 사람이 적는다
sudo /opt/tybot/deploy/setup-tybot-vipbot-runtime.sh --verify
```

유닛은 여기서 멈춘다. 런타임 모듈이 서고 `[Install]` 을 더한 뒤에 설치한다.

## 확인 (요청 7번)

`tests/test_vipbot_delivery.py` — import 정렬 ruff 통과, `NO_SOURCES` 전달 코드
포함. `vipbot_delivery.NO_SOURCES == vip_access.NO_SOURCES` 동기화 시험도 있다.

## 안 한 것

1. **`--apply` 를 운영 DB 에서 돌리지 않았다.** 계획 모드와 시험만 돌렸다
2. **`install.sh` 를 고치지 않았다.** 유닛이 자동으로 깔리면 「아직 없다」 와
   「꺼져 있다」 를 구분할 수 없다. 가동할 때 사람이 한 줄 더한다
3. **런타임 모듈은 Codex 몫이다.** 이 유닛은 그 모듈 이름(`tybot.vipbot_runtime`)
   을 가정한다 — 다르게 정해지면 `ExecStart`·`ExecStartPre` 두 줄을 맞춘다
