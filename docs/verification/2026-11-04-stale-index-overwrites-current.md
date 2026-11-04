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

## 스키마 대안 넷

제품 변경은 보류. 고르는 것은 Codex 몫이다.

### A. 읽은 시각 칸 (`source_seen_at timestamptz`)

색인 잡이 **원문을 읽은 시각**을 같이 넣고, 승격을 그 값으로 건다.

```sql
ALTER TABLE archive_search_document_revision ADD COLUMN source_seen_at timestamptz;
-- 승격 직전: 현재 current 의 source_seen_at 보다 크지 않으면 승격하지 않는다
```

- 쉽다. 잡이 이미 아는 값이다
- **시계에 기댄다.** 색인 잡이 여러 호스트에서 돌면 skew 가 그대로 결함이 된다.
  지금은 한 호스트지만, 그 전제를 스키마에 박는 셈이다

### B. 세대 번호 (`index_generation bigint`, 시퀀스)

잡이 **원문을 읽기 전에** 시퀀스에서 번호를 받고, 그 번호를 들고 간다.
승격은 번호가 더 클 때만.

```sql
CREATE SEQUENCE archive_search_index_generation;
ALTER TABLE archive_search_document_revision ADD COLUMN index_generation bigint;
```

- **시계가 없다.** DB 가 주는 단조 증가 값 하나뿐이라 호스트가 늘어도 같다
- 잡이 「읽기 전에 번호를 받는다」 를 지켜야 한다. 안 지키면 조용히 무력해진다 —
  규칙이 코드 바깥에 있다는 뜻이고, A 의 시계와 같은 종류의 약점이다

### C. 기대 current 대조 (compare-and-set)

잡이 **읽을 때 보던 current 의 id** 를 들고 와서, 그게 아직 current 일 때만
승격한다. 아니면 거절하고 잡이 **다시 읽는다.**

```sql
ALTER TABLE archive_search_document_revision ADD COLUMN ...  -- 표 변경 없음
-- 승격 UPDATE 에 ... AND (SELECT id FROM ... WHERE is_current) IS NOT DISTINCT FROM %(expected)s
```

- 새 칸도 시계도 순서값도 없다. **표가 안 바뀐다**
- 위 그림에서: A 는 B 가 끼어든 것을 보고 거절당하고, 다시 읽어 v2 를 올린다.
  재시도가 「옛 원문을 다시 읽는」 이 아니라 **지금 원문을 읽는** 것이라 수렴한다
- 값을 안 넘기는 호출부는 그냥 예전처럼 덮는다 — **기본값이 막는 쪽이 아니다**
  (원칙 3 과 어긋난다). 인자를 필수로 만들면 호출부를 다 고쳐야 한다
- 경합이 잦으면 재색인 비용(임베딩 호출)이 버려진다

### D. 그대로 두고 드러내기

고치지 않고, **빠진 것을 보이게만** 한다 — resolver 가 `source_revision` 불일치로
후보를 버릴 때 세어서 콘솔에 올린다.

- 스키마 변경 0. 오늘 피해가 「조용함」 이므로 그 부분만 없앤다
- 결함은 남는다. 다음 재색인까지 재현율이 떨어진 채다

## 추천

**C(기대 current 대조)** 를 먼저 본다 — 표를 안 바꾸고, 시계도 외부 규칙도 안 쓴다.
C 가 호출부 때문에 안 되면 **B**. A 는 색인 잡이 한 호스트라는 전제가 깨지는 날
조용히 틀리므로 마지막에 둔다.

어느 쪽이든 고치면 `test_a_never_indexed_stale_revision_takes_over_current_today`
의 단언을 **뒤집어야 한다.** 지금 그 시험은 결함을 고정하고 있다.
