# B-81 DB 스키마·저장소 검증 (2026-10-27)

Codex 구현에 대한 읽기 전용 리뷰. **기존 파일을 고치지 않았다.** 추가한 것은
`tests/test_image_description_store_db.py` 와 이 문서뿐이다.

## 시험·Ruff

개발 PC(Windows) 기준.

| 명령 | 결과 |
|---|---|
| `pytest` (게이트·스키마·저장소·드리프트 4모듈) | **74 passed** |
| `ruff check src tests scripts` | **All checks passed** |
| `pytest tests/test_image_description_store_db.py` | **29 skipped** — `TEST_DATABASE_URL` 없음 |

실제 DB 는 **이 세션에서 돌리지 못했다.** DSN 이 없다. 건너뛴 사유를 시험이
직접 말하게 해 뒀다 — 「초록인데 아무것도 안 쟀다」 가 제일 나쁜 상태다.

## 9개 항목

| # | 항목 | 판정 |
|---|---|---|
| 1 | withhold 가 빈 설명 + 태그·코드로 저장되는가 | **조건부** — 아래 발견 1 |
| 2 | block 이 revision 에 INSERT 되지 않는가 | **안전** (이중 잠금) |
| 3 | 비용 > 0 이면 발행 안 되는가 | **코드만** — 아래 발견 2 |
| 4 | 승인·SHA·prompt·model·provider 가 identity 에 묶이는가 | **안전** |
| 5 | 정책 저장만으로 호출되지 않는가 | **안전** |
| 6 | quota·일일·동시성의 0/모름 | **대체로 안전** — 아래 관찰 1 |
| 7 | dispatching/unknown 재전송 | **안전** |
| 8 | Master·수집기·PUBLIC 권한 | **안전** |
| 9 | 원문·bytes·raw 응답이 job/로그에 남는가 | **안전** |

---

## 발견 1 (높음) — 게이트가 내는 조합을 스키마가 거절한다

`archiver_image_description_revision` 에 이런 제약이 있다.

```sql
CHECK (guard_verdict <> 'withhold_description' OR status = 'uncertain')
```

그런데 `guard_index_write` 는 **`withhold_description` + `described` 를 실제로
낸다.** `index_status` 가 「불확실성 코드가 있으면 uncertain」 으로만 결정되는데,
설명을 빼는 경로 둘은 **코드를 남기지 않는다.**

| 입력 | 게이트 verdict | `index_status` | DB |
|---|---|---|---|
| 설명이 공백 + uncertainty 없음 | `withhold_description` | `described` | **거절** |
| 모르는 uncertainty 코드만 | `withhold_description` | `described` | **거절** |
| 민감 코드 | `withhold_description` | `uncertain` | 통과 |
| 화질 코드 | `allow` | `uncertain` | 통과 |

재현(모델·DB 없이 돈다):

```
tybot.archive.image_description_output_guard.guard_index_write(
    ImageDescription(status="uncertain", description="", uncertainty=("새코드",), …)
)
→ verdict="withhold_description", index_status="described"
```

### 무엇이 터지나

`finish_and_publish` 는 `publish` 일 때 그대로 INSERT 한다. `CheckViolation` 이
`ImageDescriptionDenied` 로 감싸이지 않고 올라가고, **트랜잭션이 통째로
롤백되므로 작업 상태 UPDATE 도 사라진다.** 작업은 `dispatching` 에 남고,
`mark_unknown` 을 사람이 부르기 전까지 동시성 슬롯을 계속 잡는다.

모델이 모르는 코드를 하나 내보내면 바로 닿는 경로다.

### 왜 시험이 못 잡았나

`tests/test_image_description_store.py` 의 `ScriptedConnection` 은 **SQL 을
실행하지 않는다.** 미리 적어 둔 행을 돌려줄 뿐이라 `CHECK`·외래키·`UNIQUE`·
권한이 한 번도 돌지 않는다. 저장소 로직을 재는 데는 맞는 대역이지만
「DB 가 거절하는가」 는 그 대역으로 답할 수 없다.

추가한 `tests/test_image_description_store_db.py` 의
`test_a_withhold_row_must_be_uncertain` 이 이 경계를 실제 DB 에서 건다.

### 고치는 쪽은 둘 중 하나

둘 다 **내 소유 밖**이라 고치지 않았다.

1. 게이트에서 `withhold_description` 이면 `index_status` 를 **항상 `uncertain`**
   으로 둔다. 「설명을 뺐다」 는 그 자체로 확정하지 않았다는 뜻이라 의미도 맞다
2. 스키마의 `CHECK` 를 푼다 — 권하지 않는다. 설명 없는 행이 `described` 로
   들어가면 검색 쪽에서 「설명이 있는 줄」 로 세어진다

1번이 맞다고 본다.

---

## 발견 2 (보통) — 비용이 붙어도 **다음 호출을 막지 않는다**

`finish_and_publish` 는 비용이 0보다 크면 발행을 막는다. 거기까지는 맞다.

```python
unexpected_charge = actual_usd is not None and actual_usd > 0
publish = decision.storable and not unexpected_charge
```

작업은 `blocked` + `unexpected_charge` 로 남고 `actual_usd` 가 기록된다. 좋다.

그런데 `archiver_image_model_catalog.zero_cost_verified` 는 **그대로 true 다.**
저장소 어디에서도 과금을 근거로 카탈로그를 내리지 않는다(`zero_cost_verified`
는 `save_catalog` 에서만 바뀐다). 그래서 **다음 사진이 같은 모델로 또 나가고 또
과금된다.** 사람이 콘솔에서 내릴 때까지 멈추지 않는다.

「무료라서 켠 기능」 이 조용히 과금되는 것을 막는 것이 이 설계의 전제인데, 한 건을
막을 뿐 **흐름을 막지는 않는다.**

제안: 과금이 확인되면 같은 트랜잭션에서 그 (provider, model) 의
`zero_cost_verified` 를 false 로 내리거나 `quota_state='exhausted'` 로 둔다.
사람이 다시 검증해 올리게 하는 쪽이 설계 의도와 같다.

### 덧붙여 — 비용 `None` 은 통과한다

`cost_usd` 가 `None`(모름)이면 `unexpected_charge` 가 False 라 발행된다.
카탈로그가 `zero_cost_verified` 를 요구하므로 큰 구멍은 아니지만, **「모름」 을
「0」 으로 취급하는 유일한 자리**다. 6번 항목이 막으려는 모양과 같다.

---

## 항목별 근거

### 2. block 은 두 겹으로 막힌다

- 코드: `publish = decision.storable and …` — `storable` 은 `allow`·`withhold` 뿐
- 스키마: `guard_verdict IN ('allow','withhold_description')` ·
  `status IN ('described','uncertain')`

한쪽이 뚫려도 다른 쪽이 잡는다. **이 항목은 안전하다.**

### 4. identity 에 무엇이 묶이나

`dispatch_identity` 는 승인 scope 에서 `policy_revision` 만 빼고 해시한다.

```
workspace · channel · file_id · original_sha256 · prompt_revision ·
model · provider · image_sha256
```

원본 SHA·prompt·model·provider 전부 들어간다 ✓. `image_sha256`(실제 보낸
bytes)도 따로 들어가 **원본과 렌더링본이 갈린다.**

`approval_id` 는 **일부러 빠져 있다.** 빼야 승인을 새로 받는 것만으로 같은
bytes 를 다시 보낼 수 없다(7번과 같은 축). 작업 행 자체는 `approval_id` 외래키로
한 승인에 묶여 있으므로 「어느 승인으로 나갔나」 는 남는다.

`policy_revision` 도 빠져 있다 — 정책을 저장했다고 재전송되지 않는다(5번).

**뒤집으면 이런 뜻이다:** 실패한 사진은 같은 prompt·model 로 **영원히 재시도할
수 없다.** 다시 하려면 prompt revision 이나 모델이 바뀌어야 한다. 의도된
설계로 읽히지만, 운영에서 「일시 장애로 실패한 건」 을 되돌릴 길이 없다는 뜻이기도
하다. 콘솔에 그 사실이 보여야 한다.

### 5. 정책 저장은 전송을 일으키지 않는다

`save_policy` 는 정책 표와 감사 표만 쓴다. 전송은 `reserve` → `begin_dispatch`
두 단계를 지나야 하고, 둘 다 `preflight_image_description` 을 다시 돈다.
`revision` 은 낙관적 잠금(`expected_revision` 불일치 → `policy_conflict`)이다.

### 6. 0 과 모름

| 축 | 처리 |
|---|---|
| 동시성 | `type(cap) is not int or cap <= 0` → `concurrency_unconfigured` ✓ |
| 일일 호출 | 같은 검사 → `daily_calls_unconfigured` ✓ |
| quota 0 | 스키마가 `quota_remaining = 0` 이면 `quota_state='exhausted'` 강제 ✓ |
| quota 모름 | 카탈로그가 `quota_state='unknown'` 을 차단 ✓ |

동시성 카운트가 `reserved`·`dispatching`·**`unknown` 을 함께 센다.** 전송
여부가 모호한 작업이 슬롯을 계속 잡는다 — 맞는 방향이다.

일일 호출은 상태와 무관하게 그날 만든 작업을 전부 센다. `cancelled`·`blocked` 도
센다 — 보수적이다.

#### 관찰 1 — `quota_state='available'` + `quota_remaining IS NULL`

이 조합은 스키마도 카탈로그도 통과하고, 저장소의 quota 카운팅은
`if catalog.quota_remaining is not None:` 으로 **통째로 건너뛴다.**
「쓸 수 있는데 얼마인지는 모른다」 가 **무제한**이 된다.

일일 호출·동시성 상한이 따로 있어 폭주하지는 않는다. 다만 6번이 막으려는 모양과
같으므로 적어 둔다 — 숫자를 모르면 `unknown` 으로 두는 운영 규칙이 필요하다.

### 7. 재전송 경로가 없다

- `revoke()` 는 `state='reserved'` 만 취소한다. 주석이 이유를 적어 뒀고
  `dispatching`·`unknown` 은 손대지 않는다 ✓
- `cancel()` 도 `reserved` 만 ✓
- `mark_unknown()` 은 `dispatching` 에서만 ✓
- 취소된 뒤 같은 사진을 새 승인으로 넣어도 `identity_sha256` **UNIQUE** 에
  걸려 `duplicate_job` 이다 ✓

**취소가 재전송 통로가 되지 않는다.**

### 8. 권한

- `REVOKE ALL … FROM PUBLIC` 여섯 표 전부 ✓
- `GRANT` 는 `tybot_vision` 역할이 **이미 있을 때만**, `DO $$` 안에서 ✓
- 역할 생성 없음 ✓. Master·수집기에 대한 `GRANT` 없음 ✓
- 파생 색인 표는 `SELECT, INSERT` 만 — **`UPDATE`·`DELETE` 없음** ✓ (불변)
- `apply-schema.sh` 는 스키마를 **`postgres` 역할로** 적용한다 → 여섯 표의
  소유자가 `postgres` 다. 봇·수집기가 그 역할로 붙지 않는 한 접근 경로가 없다

`tybot_vision` 역할이 없으면 `GRANT` 가 조용히 건너뛰어지고, 저장소는 런타임에
권한 오류로 닫힌다. **막히는 쪽으로 실패한다** — 맞는 방향이지만 배포 시
역할 존재를 확인하는 절차가 필요하다.

### 9. 본문이 남는 곳이 없다

| 자리 | 내용 |
|---|---|
| `job.scope` | 승인 scope + `image_sha256`. 설명·bytes 없음 |
| `job.policy_snapshot` | 설정값 |
| `job.catalog_snapshot` | 모델 검증 사실 |
| `job.error_code` | 게이트 코드 또는 코어 오류 코드(둘 다 고정 목록) |
| `revision.description` | **게이트를 통과한 설명만.** 이 표의 존재 이유다 |

`image_description_store.py` 에는 **로거가 없다.** 본문이 샐 자리가 없다는 뜻이고,
동시에 **감사 로그도 없다**는 뜻이다. 운영에서 「언제 무엇이 거절됐나」 는 작업 표를
읽어야만 알 수 있다.

---

## 미검증

- **실제 DB 를 한 번도 안 돌렸다.** `TEST_DATABASE_URL` 이 없다. 위의
  스키마 판정은 전부 **SQL 을 읽어서** 낸 것이고, 추가한 29건은 skip 상태다
- 발견 1 의 터지는 모습(`CheckViolation` 이 어떻게 올라오는지, 작업이
  `dispatching` 에 남는지)은 **DB 없이 재현하지 못했다.** 게이트가 그 조합을
  낸다는 것까지만 실제로 확인했다
- `tybot_vision` 역할이 운영 DB 에 실제로 있는지 확인하지 않았다
- 동시성·일일 상한의 경쟁 조건은 재지 않았다. 전역 정책 행을 `FOR UPDATE` 로
  잠그므로 직렬화되는 것으로 읽히지만, **두 프로세스를 띄워 보지는 않았다**
- `_money` 의 `Decimal` 과 스키마 `numeric` 의 반올림 경계는 안 봤다

### 실제 DB 검증 절차

전용 격리 DB 에서만. **운영 DSN 을 주지 마십시오.**

```bash
TEST_DATABASE_URL=postgresql://…/tybot_image_test \
    sudo -u tybot /opt/tybot/.venv/bin/python -m pytest \
    tests/test_image_description_store_db.py -q
```

DSN 은 출력·문서에 남기지 않는다.

---

## Codex 후속 반영 (2026-10-27)

- 발견 1: `withhold_description`의 `index_status`를 항상 `uncertain`으로 바꿨다.
  빈 설명·모르는 uncertainty도 스키마와 같은 뜻으로 닫힌다.
- 발견 2: 비용이 양수이거나 `None`이면 revision을 발행하지 않고, 같은 트랜잭션에서
  해당 모델의 `zero_cost_verified=false`, `quota_state=unknown`으로 내려 다음 호출을 막는다.
- 관찰 1: `available`은 양수 `quota_remaining`을 반드시 요구한다. 수량 제한이 없는
  무료 계약은 검증된 `unlimited` 상태로만 표현하고, `unknown`과 구분한다.
- 실제 DB 시험에 `available + NULL` 거절과 명시적 `unlimited` 허용을 추가했다.

실제 PostgreSQL 31건은 여전히 전용 격리 DB에서 실행해야 한다. 운영 DB로 시험하지 않는다.
