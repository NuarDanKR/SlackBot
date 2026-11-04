# 늦게 끝난 옛 색인이 새 색인을 밀어낸다 — 재현과 스키마 대안

작성 2026-11-04 · 별도 조사. **제품 코드는 안 고쳤다**(설계 검토 전 보류).
재현 시험만 `tests/test_embedding_index_store_db.py` 끝에 붙였다.

## 한 줄

벡터 색인 표에 **「어느 쪽이 더 최신 원문인가」 를 보는 칸이 없다.**
그래서 발행이 늦은 쪽이 이긴다 — 그 쪽이 읽은 원문이 더 오래됐어도.

## 어디가 위험한 구간인가 — 처음 생각과 다르다

전에는 「재색인 잡 둘이 **겹쳐 돌면**」 이라고 적었다. 트랜잭션이 겹치는
그림이었다. 실제로는 안 겹친다.

`EmbeddingIndexWriter._publish` 는 맨 먼저 이걸 잡는다.

```python
cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock_key,))
#                     lock_key = workspace | channel_id | doc_path | model_revision
```

같은 문서를 두 잡이 동시에 발행하면 **DB 가 둘을 줄 세운다.** 한쪽 트랜잭션이
끝나야 다른 쪽이 들어간다. 그러니 위험한 곳은 트랜잭션 안이 아니다.

```
잡 A:  원문 읽기(v1) ─────────────────────── 임베딩 ────────── 발행
잡 B:        원문 읽기(v2) ── 임베딩 ── 발행
                                          ↑ 여기서 B 가 현역
                                                              ↑ 여기서 A 가 현역
       └──────────── 위험 구간: 읽은 때 ~ 발행 때 ────────────┘
```

**읽은 시점의 순서를 아무도 안 들고 간다.** `source_revision` 은 SHA-256 이라
순서가 없고, `indexed_at` 은 **읽은 때가 아니라 쓴 때**다. 늦게 쓴 쪽이 이긴다.

그래서 재현에 스레드가 필요 없다. 위험 구간을 접으면 **발행 순서만** 남는다.

## 재현 (결정론적)

`test_a_never_indexed_stale_revision_takes_over_current_today`

```python
fresh = writer.publish(key("v2"), "v2", ...)   # 새 원문을 읽은 잡이 먼저 끝난다
assert current == [fresh]

stale = writer.publish(key("v1"), "v1", ...)   # 옛 원문을 읽은 잡이 늦게 끝난다
assert current == [stale]                      # ← 오늘은 이게 통과한다
assert row(fresh) == ("retired", False)
```

## 비대칭 — 막아 주는 것은 신선도가 아니다

`test_a_returning_retired_revision_is_refused`

| 순서 | 결과 |
|---|---|
| v2 발행 → v1 발행 | **v1 이 현역이 된다** (v1 은 처음 보는 판) |
| v0 → v1 → v0 발행 | `embedding_revision_conflict` 으로 **거절** |

둘째가 막히는 이유는 「v0 이 옛 원문이라서」 가 아니다. v0 **행이 이미 있어서**다 —
writer 가 멱등 가지로 들어가고 `state<>'ready'` 라 거절한다
(`_existing`). 즉 지금 있는 방어는 **기억**이지 **신선도**가 아니다.
한 번도 색인 안 된 옛 판은 그 기억에 안 걸린다.

## 지금 실제 피해

오류는 안 난다. 막아 주는 것은 하나뿐이다 — resolver 가 후보의
`source_revision` 을 현재 정본과 대조해 틀리면 버린다.

그래서 **답이 틀리지는 않는다.** 대신 그 문서가 **벡터 검색에서 조용히
빠진다** — 다음 재색인까지. 원칙 1·2 는 안 깨지고, 재현율만 떨어진다.
조용하다는 게 문제다. 아무 데도 안 남는다.

## 고친 방법 — C(기대 current 대조, CAS)

대안 넷 중 **C** 를 골랐다. 표를 안 바꾸고(스키마 변경 0), 시계도 외부 순서값도
안 쓴다. 발행자가 **자기가 대체하려는 행을 직접 말한다.**

```python
writer.publish(key, text, chunks, vectors, expected_current=<내가 본 현역 id | None>)
```

- `expected_current` 에 **기본값이 없다.** 무엇을 대체하는지 말 못 하는 호출자는
  발행할 수 없다. 기본값을 두면 그 기본값이 곧 예전 동작이 된다
- 비교는 **advisory lock 을 받은 뒤**다. 락 전에 읽으면 그 값은 내가 쓰기 전에
  남이 바꿀 수 있고, 검사는 장식이 된다
- 비교 범위는 은퇴 UPDATE 와 **똑같이** `(workspace, channel_id, doc_path,
  model_revision)` 다. `chunker_revision` 으로 좁히면 **청커를 바꾸는 날 모든
  문서가 영원히 거절된다** — 새 청커 기준으로는 현역이 없고, 실제로는 옛 청커
  행이 현역이라 기대가 절대 안 맞는다
- 안 맞으면 `embedding_source_superseded`. **아무것도 안 쓴 자리**에서 던지므로
  기존 current 는 그대로고 새 행·chunk 도 안 남는다

### 한 가지 예외 — 이미 그 판이 서 있으면

기대가 틀렸어도 **지금 현역인 것이 바로 올리려던 판**이면(같은
`source_revision` + 같은 `chunker_revision`) 거절하지 않는다. 쓸 것이 없고,
멱등 가지가 `already_current` 로 끝난다.

없으면 같은 원문을 집어 든 두 잡이 평범하게 겹친 날 오류가 난다 —
`test_two_connections_publishing_the_same_revision_make_one_row` 가 그 자리다.

### 거절된 뒤 — 벡터를 다시 쓰지 않는다

`run_index_batch` 는 **그 자리에서 재시도하지 않는다.** 손에 든 벡터는 이미
정본이 아닌 글을 가리키므로, 다시 올리면 **없는 글을 색인한다.**

복구는 **다음 실행**이다 — 정본을 다시 열고, 다시 자르고, 다시 임베딩한다.
결과는 `publish_superseded` 로 따로 센다. `publish_failed` 에 섞으면 writer 가
고장 난 것처럼 읽히고, 진짜 원인(**스냅숏이 실행보다 오래됨**)이 안 보인다.

### 어디서 값이 오나

`run_reconciled_index` 가 **임베딩 전에** 한 번 읽는다.

```python
observed = current_document_ids(connection, model_revision=...)   # 좁히지 않은 범위
batch = run_index_batch(..., expected_current=observed)
```

지도에 없는 문서는 「현역 없음」 이고, writer 는 그것을 **최초 색인으로만**
받아들인다. 그래서 빈 지도를 넘기면 문서를 만들 수는 있어도 **대체는 못 한다** —
닫힌 쪽이 기본값이다(원칙 3).

## 제품 변경 목록 (DB 시험 파일 밖) — 커밋 안 함

| 파일 | 무엇 |
|---|---|
| `src/tybot/embedding_index_store.py` | `publish(..., *, expected_current)` 필수 인자 · 락 직후 CAS 비교와 `embedding_source_superseded` · 멱등 예외 · `current_document_ids()` 신설 |
| `src/tybot/embedding_index_plan.py` | `_Publisher` 규약과 `IndexPlan.publish(..., *, expected_current)` 통과 |
| `src/tybot/embedding_index_job.py` | `Publisher` 규약 · `run_index_batch(..., expected_current=지도)` 필수 · `publish_superseded` 결과 코드 · 재시도 안 함 |
| `src/tybot/embedding_index_runner.py` | 임베딩 전에 `current_document_ids` 를 읽어 그대로 넘김 |

시험(제품 아님): `tests/test_embedding_index_store.py` ·
`tests/test_embedding_index_plan.py` · `tests/test_embedding_index_job.py` ·
`tests/test_embedding_index_runner.py` · `tests/test_embedding_index_store_db.py`.

**스키마 파일은 안 건드렸다.** `deploy/sql/search_embedding_schema.sql` 그대로.

## 고르지 않은 셋 (기록)

### A. 읽은 시각 칸 (`source_seen_at timestamptz`)

쉽지만 **시계에 기댄다.** 색인 잡이 여러 호스트로 늘면 skew 가 그대로 결함이
된다. 한 호스트라는 전제를 스키마에 박는 셈이라 마지막에 뒀다.

### B. 세대 번호 (`index_generation bigint`, 시퀀스)

시계는 없지만 **「읽기 전에 번호를 받는다」 는 규칙이 코드 바깥에** 있다. 안
지키면 조용히 무력해진다. 스키마도 바뀐다.

### D. 그대로 두고 드러내기

결함은 남기고 resolver 가 버린 후보만 세어 콘솔에 올린다. 오늘 피해가
「조용함」 이라 그 부분은 없어지지만, 재현율은 다음 재색인까지 떨어진 채다.

C 를 고른 이유는 **표가 안 바뀌고, 지켜야 할 규칙이 인자 하나로 코드 안에**
들어오기 때문이다. 약점은 경합이 잦을 때 임베딩 비용이 버려진다는 것 —
`publish_superseded` 수치가 그 비용을 보여 준다.
