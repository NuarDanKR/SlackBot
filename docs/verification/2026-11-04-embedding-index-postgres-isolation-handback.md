# B-12 벡터 색인 — 실제 PostgreSQL 격리 시험 인계

작성 2026-11-04 · 범위: `tests/test_embedding_index_store_db.py` 확장과 이 문서.
Codex 의 `hybrid_search_eval.py` · `hybrid_search_rank.py` · 기준선 CLI 는 **안
건드렸다.** 커밋하지 않았다.

## 서버 실측

> 2026-11-04 서버 배포본 `d0c761f`에서 당시 존재하던 **31건이 전부 통과**했다.
> PostgreSQL 18.6 · pgvector 0.8.6, `31 passed in 0.87s`.
>
> 그 뒤 CAS(`expected_current`) 경쟁 조건 시험 두 건을 더해 현재 작업 트리는
> **33건**이다. 추가 두 건은 CAS 커밋 배포 후 실제 DB에서 다시 확인해야 한다.

확인한 것(2026-11-04, Windows 11 / Python 3.13.14):

| 찾은 것 | 결과 |
|---|---|
| `TEST_DATABASE_URL` | 비어 있음 |
| `psql` · `pg_ctl` · `initdb` | 없음 |
| `docker` · `podman` | 없음 |
| `127.0.0.1:5432` | 아무것도 안 듣는다 |
| `C:\Program Files\PostgreSQL*` · 관련 서비스 | 없음 |
| 자체 포함 PG 휠(`pgserver`) | 이 플랫폼용 배포본 없음 |
| `psycopg` | **3.3.5 설치돼 있음**(그래서 시험은 import 된다) |

`pytest` 가 남기는 skip 사유도 같은 말을 한다.

```
TEST_DATABASE_URL is absent; the pgvector dimension trigger, the
incomplete-revision guard, idempotent republication, the one-current index
under concurrency, mid-publication rollback, uninterrupted current coverage,
and cross-workspace candidate isolation are all UNVERIFIED in this session
```

시험 중 `test_the_database_reports_its_version`가 두 값을 `record_property`와
표준출력으로 남겼다.

| | 값 |
|---|---|
| PostgreSQL | 18.6 (`x86_64-pc-linux-gnu`) |
| pgvector | 0.8.6 |
| 돌린 날짜·환경 | 2026-11-04 · 운영 서버의 폐기 가능한 `tybot_schema_test` |
| 배포본 결과 | `31 passed in 0.87s` (`d0c761f`) |
| 현재 CAS 추가분 | 33건 중 추가 2건 실제 DB 검증 대기 |
| 실패한 항목 | 없음 |

## 어떻게 돌리나

폐기 가능한 전용 DB 가 필요하다. 이름에 `schema_test` · `schema_check` ·
`scratch` 중 하나가 **들어가야** 하고, `bench` · `lab` · `index` · `prod` ·
`tyslackai` 가 들어가면 거부된다(`scripts/verify_schema_isolated.py`
`operational_db_refusal`). 시험 fixture 가 그 판정을 그대로 부르므로, 운영 DB 를
가리키면 **시작도 안 한다.**

```sql
CREATE DATABASE tybot_schema_test;           -- DBA 가 미리 만든다
CREATE EXTENSION IF NOT EXISTS vector;       -- 그 DB 안에서
```

```bash
# 서버 (CLAUDE.md: 운영 명령은 venv 절대 경로)
cd /opt/tybot && sudo -u tybot env -u DATABASE_URL \
  TEST_DATABASE_URL='postgresql://…/tybot_schema_test' \
  /opt/tybot/.venv/bin/python -m pytest \
  tests/test_embedding_index_store_db.py -q -s -p no:cacheprovider
```

`cd /opt/tybot` 이 **필요하다.** `sudo -u` 는 작업 디렉터리를 안 바꾸므로, `root`
셸(`/root`)에서 그냥 치면 pytest 가 끝에 `os.chdir(session.startpath)` 로 `/root`
에 돌아가려다 `PermissionError` 로 죽고 **결과 요약이 통째로 안 나온다**(2026-11-04
실제로 그랬다). `-p no:cacheprovider` 는 `tybot` 이 `/opt/tybot/.pytest_cache` 를
못 써서 나는 경고를 없앤다.

`DATABASE_URL` 을 **비워야 한다.** 설정돼 있으면 격리 판정이 거부한다 — 운영
셸에서 무심코 돌리는 모양을 막는 자물쇠다.

### 스키마가 적용될 자리가 있어야 한다

```
psycopg.errors.InvalidSchemaName: no schema has been selected to create in
```

2026-11-04 첫 시도에서 네 건이 전부 이것으로 멈췄다. 연결은 됐고, **스키마 파일이
내려앉을 스키마가 없었다.** 둘 중 하나다.

- `public` 스키마가 없다 — `scripts/verify_schema_isolated.py` 가
  `DROP SCHEMA public CASCADE` 를 돌리므로, 그 스크립트가 중간에 끊기면 안 돌아온다
- `public` 은 있는데 접속 역할에 `USAGE`/`CREATE` 가 없다

**두 경우가 같은 오류로 나온다.** PostgreSQL 은 `USAGE` 가 없는 스키마를
`search_path` 에서 조용히 빼기 때문이다. 그래서 fixture 가 스키마를 적용하기
**전에** 먼저 보고 역할·DB·search_path·보이는 스키마를 같이 찍는다
(`_unreachable_schema_report`).

```bash
psql "$TEST_DATABASE_URL" -c '\dn+' \
  -c "select current_user, current_database(), current_schemas(true), current_setting('search_path')"
```

고치는 것은 **격리 DB 에서만**.

```sql
CREATE SCHEMA IF NOT EXISTS public;
GRANT USAGE, CREATE ON SCHEMA public TO <접속역할>;
-- 또는: ALTER SCHEMA public OWNER TO <접속역할>;
```

스키마는 시험이 직접 적용한다(`deploy/sql/search_embedding_schema.sql`).
같은 DB 를 여러 번 돌려도 된다 — 표는 `IF NOT EXISTS` 이고 행은 uuid 좌표라
서로 안 겹친다. 다만 행이 쌓이므로 가끔 DB 를 버리고 다시 만드는 편이 좋다.

## 무엇을 보게 했나 (33건)

요구 번호대로.

### 1. 차원 불일치 거절 (4건)

- `test_a_wrong_dimension_chunk_is_refused_by_the_trigger` — **writer 를 건너뛰고**
  표에 직접 3차원 벡터를 넣는다. 2차원 모델이므로 트리거가
  `embedding_dimension_mismatch` 로 막아야 한다
- `test_a_right_dimension_chunk_passes_the_same_trigger` — 막는 쪽만 보면
  「전부 막는 트리거」 도 통과한다
- `test_changing_a_chunk_to_a_wrong_dimension_is_refused` — `UPDATE` 도 같은 문을
  지난다. 넣을 때만 보면 고쳐 넣으면 된다
- `test_the_writer_refuses_a_wrong_dimension_before_the_database_does` — 문이 둘

### 2. 미완료 revision 은 current 가 못 된다 (3건)

chunk 가 없는데 `chunk_count=1` / `state='building'` 인데 `is_current` /
`indexed_at` 이 빈 채로 current — 셋 다 거절되고 current 수는 0 이어야 한다.

### 3. 멱등 발행과 좌표 불일치 (3건)

같은 판 재발행은 **행도 chunk 도 안 늘고** `already_current=True`.
같은 본문을 다르게 자르면(개수가 달라도, 개수가 같고 자리만 밀려도)
`embedding_revision_conflict` 이고 **기존 행은 그대로**여야 한다.

### 4. 동시 발행에서 current 단일성 (3건)

- 서로 다른 판을 **두 연결**이 동시에 → 끝나고 current 는 정확히 하나
- 같은 판을 두 연결이 동시에 → 둘 다 성공, 행은 하나, 한쪽만 `already_current`
- 손으로 두 번째 current 를 세우려 하면 부분 유니크 색인이 거절

세 번째가 필요한 이유: 동시성 시험은 **운 좋게 통과할 수 있다.** 색인 자체를
따로 본다.

### 5. 중간 실패 전체 rollback (3건)

- chunk 중복으로 실패 → 매니페스트·chunk 둘 다 안 남고 current 는 0
- **은퇴 뒤에 실패** → 옛 판이 그대로 현역이고 새 판은 흔적도 없다

둘째 자리는 바깥에서 만들 수 없어 **DB 안에 고장을 심는다** — 시험 동안만 다는
`zz_pytest_fail_ready` 트리거가 마지막 `UPDATE` 에서 예외를 던진다. 이름이
`zz_` 로 시작하는 이유는 PostgreSQL 이 같은 시점 트리거를 이름 순서로 돌리기
때문이다(스키마 검사 트리거가 먼저 돌아야 한다). 끝나면 트리거와 함수를 지운다.

셋째(`test_the_injected_trigger_recovers_from_an_aborted_run`)는 **그 주입
자체가 재실행 가능한가**를 본다. 앞 실행이 `finally` 를 못 돌린 채 끊기면
(Ctrl-C · 연결 끊김) 같은 이름의 트리거가 남고, 다음 실행은 `CREATE TRIGGER`
에서 `DuplicateObject` 로 죽는다. 화면에는 「이미 있다」 만 뜨므로, 남은 트리거가
**그 표의 모든 UPDATE 에 고장을 섞고 있다**는 사실은 안 보인다 — 그 상태에서는
다른 시험들도 같이 틀어지고, 사람이 손으로 지우기 전까지 안 풀린다. 그래서
`_injected_trigger` 가 들어갈 때 **먼저 지우고** 만들도록 바꿨고, 시험은 남은
트리거를 손으로 걸어 두고 그 위에서 선다는 것을 확인한다.

### 6. current 가 끊기는 구간이 없다 (1건)

`test_the_old_revision_stays_current_until_the_new_one_commits`.

writer 는 **은퇴를 먼저** 돌리고 그 다음에 새 판을 세운다. 그래서 「새것이 선
뒤에만 옛것이 은퇴한다」 는 코드 순서로는 거짓이다. 참인 것은 **바깥에서 보면
그 순서가 안 보인다**는 것이고(한 트랜잭션), 시험은 그걸 본다.

**들여다본 자리가 트리거 안이라는 증거를 같이 둔다.** 처음에는 은퇴 `UPDATE`
에서 `pg_sleep(1.0)` 으로 쉬게 하고 시계를 보며 0.9초 동안 표본을 떴다. 그
모양은 **트리거에 들어가기도 전을 들여다봐도 통과한다** — 서버가 느리거나 연결이
늦으면 그 0.9초가 전부 「아직 시작도 안 한」 구간일 수 있고, 그러면 옛 판이
현역인 것은 당연해서 아무것도 증명하지 않는다.

그래서 시계를 버리고 **락으로 세운다.**

1. 관찰 연결이 `pg_advisory_lock(a,b)` 를 **먼저** 잡는다
2. 주입 트리거가 은퇴 `UPDATE` 안에서 같은 키를 잡으려다 **막힌다**
3. 관찰 연결이 `pg_locks` 에서 `NOT granted` 대기를 본다 — 이게 「발행 스레드가
   은퇴 UPDATE 한가운데에 있다」 는 증거다. 커밋 전이라 다른 방법으로는 안 보인다
4. 그 상태에서만 표본을 뜨고, 표본을 다 뜬 뒤 **대기가 아직 있는지 다시 확인**한다
5. 풀어 줘야 비로소 커밋된다. `pg_sleep` 도 시계 단언도 없다

### 7. 조회 범위 (6건)

- 채널 목록이 비면 **DB 에 가지도 않는다**(연결 함수가 한 번도 안 불린다)
- 자기 범위에서는 나온다
- 워크스페이스가 다르면 채널 ID 가 같아도 안 나온다(원칙 4)
- 모르는 채널 → 0건
- 은퇴한 판은 후보가 아니다
- 모델 판을 내리면(`state<>'active'`) 즉시 0건
- 질의 차원이 다르면 **엉뚱한 순위가 아니라 0건**

## 돌리기 전에 알아 둘 것

이 시험들은 **한 번도 안 돌았다.** 처음 돌리면 다음이 걸릴 수 있다.

1. **타이밍은 안 쓴다** — 6번은 `pg_sleep` 대신 advisory lock 으로 세운다. 대신
   「트리거 안으로 못 들어왔다」 를 기다리는 상한이 `ENTRY_TIMEOUT = 30초` 다.
   그 시간 안에 `pg_locks` 에 대기가 안 보이면 실패하고, 메시지가 그 이유를 적는다
2. **연결 수** — 동시성 시험이 연결을 둘 더 연다. `max_connections` 가 빡빡한
   DB 면 그 자리에서 실패한다
3. **`DROP`/`CREATE TRIGGER` 권한** — 고장 주입에 필요하다. 읽기 전용 역할로는
   5·6번이 못 돈다
4. **행이 쌓인다** — 같은 DB 를 계속 쓰면 `candidates()` 상한에 걸릴 수 있어
   조회 시험은 `limit=500` 으로 돌린다. 더 쌓이면 DB 를 버리고 다시 만든다

## 남아 있는 경쟁 조건 — 시험으로 **안 덮인다**

### ① 늦게 끝난 옛 색인이 새 색인을 덮는다 (가장 큰 것)

**고쳤다 — 더는 경쟁 조건이 아니다.** 발행자가 **자기가 대체하려는 행을 직접
말하고**(`expected_current`, 기본값 없음), writer 가 advisory lock 을 받은 뒤
실제 current 와 대조해 다르면 `embedding_source_superseded` 로 거절한다.
**아무것도 쓴 적 없는 자리**에서 던지므로 기존 current 는 그대로고 새 행·chunk 도
안 남는다. **스키마는 안 바뀐다.**

설계와 제품 변경 목록은 따로 적었다:
[`2026-11-04-stale-index-overwrites-current.md`](2026-11-04-stale-index-overwrites-current.md).
시험은 8번 절(4건)과 4번 절(동시 발행 2건)에 있다.

남은 것은 경쟁이 아니라 **비용**이다.

- 거절된 잡의 임베딩 비용은 버려진다. 복구는 **다음 실행**이고, 그때 정본을 다시
  열어 다시 자르고 다시 임베딩한다 — 손에 든 벡터는 이미 정본이 아닌 글을
  가리키므로 재사용하면 **없는 글이 색인된다**
- 얼마인지는 `publish_superseded` 수치로 보인다. 이게 크면 **스냅숏이 실행보다
  오래됐다**는 뜻이지 writer 가 고장 난 것이 아니다

### ② advisory lock 은 해시다

`pg_advisory_xact_lock(hashtextextended(ws|ch|doc|model, 0))` — 64비트 해시라
서로 다른 문서가 **같은 락에 줄 설 수 있다.** 느려질 뿐 틀리지 않는다(정합성은
부분 유니크 색인이 지킨다). 색인 잡이 병렬로 커지면 그때 재 볼 것.

### ③ 후보를 고른 뒤 is_current 가 바뀌는 창

`candidates()` 는 아무 락도 안 잡는다. 후보를 받아 든 사이 그 판이 은퇴할 수
있다. **설계대로다** — 근거는 창구(`VisibleSources`)가 현재 권한으로 다시 열고
`source_revision` 을 대조한 것만 쓴다. 시험 범위 밖이지만 적어 둔다.

### ④ `embedding_model_revision_missing` 가지는 못 닿는다

차원 트리거의 그 분기는 매니페스트 행이 없을 때만 돈다. `document_revision_id`
가 외래키라 **닿을 수 없다.** 시험하지 않았고, 할 방법도 없다(외래키를 끄지
않는 한). 남겨 둔 안전망으로 본다.

## 다음 사람이 할 일

1. 격리 DB 를 만들고 위 명령으로 돌린다 (`cd /opt/tybot` 잊지 말 것)
2. 버전 표를 채우고, 실패한 항목이 있으면 **이 문서에 그대로 적는다**
3. ①번(늦은 색인 덮어쓰기)을 Codex 와 정한다 — 대안 넷은 별도 문서에 있다

### 8. 늦은 옛 색인은 새 current 를 못 밀어낸다 (4건)

처음 이 절은 **결함을 고정했다** — 옛 판이 그냥 현역이 되는 것을 단언했다.
CAS 가 들어오면서 **안전한 쪽으로 뒤집었다.**

- 처음 보는 옛 판이 뒤늦게 끝나면 `embedding_source_superseded` ·
  **행도 chunk 도 안 남고** 새 판이 그대로 현역
- 반대 방향(대체하려던 것이 이미 사라진 경우)도 거절 — 한 방향만 보면 「내가 본
  것」 이 뜻 없는 값이 된다
- 제대로 본 잡은 정상 발행(막는 쪽만 보면 「전부 막는 검사」 도 통과한다)
- 전에 본 판이 돌아오면 CAS 는 통과하고 **멱등 판정**이 막는다
  (`embedding_revision_conflict`) — 두 문이 막는 것이 다르다

4번 절도 바뀌었다. 서로 다른 판을 두 연결이 동시에 올리면 이제 **한쪽만 끝까지
가고** 나머지는 superseded 다. 같은 판을 둘이 올리는 경우는 그대로 **둘 다
성공**한다 — 쓸 것이 없으므로 거절할 것도 없다.
