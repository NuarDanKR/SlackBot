# 개발용 PostgreSQL 로 스키마를 검증한다

> 작성: 2026-09-25
> 대상: Archiving Bot 을 만드는 사람, DB 를 마련해 주는 사람
> **개발 PC 절차다.** 서버 절차가 아니므로 `docs/deploy/` 에 두지 않는다 —
> 섞어 두면 서버에서 그대로 친 사람이 `command not found` 만 보고 원인을
> 모른다(CLAUDE.md). 서버 스키마 적용은 `deploy/apply-schema.sh` 다.
> 스크립트: [`scripts/verify_schema_isolated.py`](../../scripts/verify_schema_isolated.py)
> 관련 결정: [Archiving Bot 착수 전 결정](../design/archiving-bot-decisions-2026-09-25.md)

## 왜 이 문서가 있나

회사 PostgreSQL 의 DBA 권한을 확보하지 못했다. 그래서 격리 DB 검증을 **release
gate 로 미뤄 두고** 개발을 계속한다(오너 결정 2026-09-25 §9).

그동안 콘솔은 `active` 전환과 위험한 기능 스위치를 **막아 둔다**(§10). 막아 두는
것이 요점이다 — 검증이 안 끝났다는 사실이 어딘가에 남아 있지 않으면, 사람은
언젠가 「했다」 고 기억하고 콘솔은 버튼을 그냥 눌러 준다.

여기 적은 절차로 **자기 PC 에서** 검증을 끝내면 게이트가 열린다.

---

## 먼저 — 운영 DSN 을 읽지 않는다

이 스크립트는 `DROP SCHEMA public CASCADE` 를 돌린다. 그래서 **어떤 경로로도
운영 설정을 자동으로 읽지 않는다.**

| 자물쇠 | 무엇을 막나 |
|---|---|
| `DATABASE_URL` 이 설정돼 있으면 시작 안 함 | 운영 셸에서 무심코 돌리는 것 |
| **설정 파일**(`.env`·`/etc/tybot/tybot.env`)이 가리키는 DB 면 거부 | export 안 된 운영 DSN |
| 이름에 `bench`·`lab`·`index`·`prod`·`tyslackai` 가 있으면 거부 | 실측 자료가 든 DB |
| 우리 것이 아닌 표가 있으면 거부 | 이름이 맞아도 남이 쓰는 DB |

두 번째가 중요하다. 개발 PC 의 `.env` 에 운영 DB 가 적혀 있는데 export 는 안 돼
있던 적이 있다(2026-09-25). 그 상태에서 첫 번째 자물쇠만 있으면 **헛돈다.**

**`--dsn` 은 손으로 준다.** 설정에서 읽어 오는 기본값이 없다. 매번 타이핑하게
만드는 것이 번거로움이 아니라 장치다.

---

## 방법 A — Docker (권장)

```bash
docker run --rm -d --name tybot-schema-test \
    -e POSTGRES_PASSWORD=devonly \
    -e POSTGRES_DB=tybot_schema_test \
    -p 15432:5432 postgres:16
```

포트를 **15432** 로 둔다. 운영은 55432, 기본은 5432 다. 셋이 겹치지 않아야 DSN 을
잘못 붙였을 때 「붙었는데 다른 DB」 가 아니라 **아예 안 붙는다.**

역할을 만든다. `LOGIN` 도 비밀번호도 주지 않는다 — 검증이 보는 것은 「권한이
붙었나」 뿐이라 접속이 필요 없다.

```bash
docker exec tybot-schema-test psql -U postgres -d tybot_schema_test -c \
    "CREATE ROLE tyslackai NOLOGIN; CREATE ROLE tybot_archiver NOLOGIN;"
```

준비 상태만 확인한다. **여기까지는 읽기만 한다.**

```bash
python scripts/verify_schema_isolated.py \
    --dsn "postgresql://postgres:devonly@localhost:15432/tybot_schema_test" \
    --preflight-only
```

돌린다.

```bash
python scripts/verify_schema_isolated.py \
    --dsn "postgresql://postgres:devonly@localhost:15432/tybot_schema_test"
```

끝나면 지운다. 컨테이너를 남겨 두면 다음에 「그때 뭘로 검증했더라」 가 된다.

```bash
docker rm -f tybot-schema-test
```

---

## 방법 B — 로컬 PostgreSQL

이미 깔려 있다면 DB 와 역할만 따로 만든다. **기존 DB 를 재사용하지 않는다.**

```bash
createdb -h localhost -p 15432 -U postgres tybot_schema_test
psql -h localhost -p 15432 -U postgres -d tybot_schema_test -c \
    "CREATE ROLE tyslackai NOLOGIN; CREATE ROLE tybot_archiver NOLOGIN;"
```

나머지는 방법 A 와 같다.

> 이 DB 는 **버려도 되는 것**이어야 한다. 스크립트가 `public` 스키마를 통째로
> 지우고 다시 만든다. 「시험용처럼 보이는 이름」 과 「버려도 되는 DB」 는 다르다 —
> `tybot_archive_bench` 와 `archive_lab` 에는 저장 구조 실측 자료가 들어 있고,
> 그래서 이름에 `bench`·`lab` 이 있으면 거부한다.

---

## pytest 로 돌리기

같은 검증을 시험으로도 돌릴 수 있다. DSN 을 주면 일곱 건이 켜지고, 없으면
**사유를 남기고** 건너뛴다.

```bash
TYBOT_SCHEMA_TEST_DSN="postgresql://postgres:devonly@localhost:15432/tybot_schema_test" \
    python -m pytest tests/test_schema_isolated_db.py -v
```

조용히 통과시키지 않는 이유: 건너뛴 것을 「했다」 로 읽으면 게이트가 없는 것과
같다.

| 시험 | 무엇 |
|---|---|
| clean install | 빈 DB 최초 적용 |
| reapply | 같은 스키마 두 번 |
| draft migration | `4ecf634` 초안이 적용된 DB 에 재적용 |
| revoke | 위험 권한이 **실제로** 회수되는가 |
| revision trigger | 번호 건너뛰기 거부 |
| legacy secret | `workspace_secret` → master 서비스 이관 |
| service identity | 신원 없이 `enabled` 불가 · 봇 사용자 중복 불가 |

---

## 통과하면 무엇이 달라지나

스크립트가 `<STATE_DIR>/state/schema-verified.json` 에 **지문과 함께** 통과를
남긴다. 콘솔이 그 파일을 보고 `active` 전환 잠금을 푼다.

```json
{
  "fingerprint": "…",
  "at": "2026-09-25T…",
  "by": "dan",
  "dsn_label": "tybot_schema_test"
}
```

`dsn_label` 은 DB **이름**이다. DSN 전체를 적으면 비밀번호가 상태 파일에 남고,
상태 파일은 로그처럼 복사된다.

### 스키마를 고치면 게이트가 다시 닫힌다

지문은 `index_schema.sql` · `console_schema.sql` · `archiving_schema.sql` ·
`workspace_service_schema.sql` 의 내용에서 나온다. 한 글자라도 고치면 달라지고,
그러면 콘솔이 다시 막는다.

**이게 요점이다.** 「한 번 검증했으니 됐다」 로 두면 검증 뒤에 고친 부분은 아무도
확인하지 않은 채로 운영에 간다. 그리고 고치는 것은 늘 검증 뒤다.

---

## 지금 막혀 있는 것

콘솔 Workspace 상세 화면에서 아래가 회색이고, 눌러도 사유가 나온다.

| 무엇 | 왜 |
|---|---|
| 채널 `active` 전환 | 운영 원문의 주인이 바뀐다 |
| `archiver_writes_live` | 아카이빙 봇이 운영에 쓰기 시작한다 |
| `separate_attachments` | 원문에서 첨부 본문이 빠진다 |
| `preserve_edit_delete` | revision 표에 쌓기 시작한다 |

**`shadow` 는 막지 않는다.** 운영 원문을 안 건드리기 때문이고, 막으면 개발이
멈춘다. 끄는 쪽도 막지 않는다 — 사고 때 내리는 손잡이를 검증 상태로 막으면
막아야 할 순간에 못 막는다.

---

## 하지 않는 것

- **운영 DB 에 스키마를 적용하지 않는다.** `apply-schema.sh` 는 서버 절차이고,
  이 문서의 대상이 아니다
- **안전장치를 완화하지 않는다.** 위 자물쇠 넷은 전부 좁히는 방향이다
- **`--dsn` 기본값을 만들지 않는다.** 기본값이 생기는 순간 「어디에 돌았는지」 를
  명령만 보고는 알 수 없게 된다
