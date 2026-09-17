# 생성 요약(abstract)과 기간 파생 요약

> 2026-09-17 오너 결정. 이 문서는 **인계 문서**다. 작업이 중단돼도 다른
> 세션·에이전트가 이어받을 수 있게 「무엇을 왜 어떤 순서로」 를 고정한다.
> 관련 원칙: [`CLAUDE.md`](../../CLAUDE.md) 절대 원칙 1 (2026-09-17 개정)

## 진행 상황 (2026-09-17 기준)

| 단계 | 상태 | 남은 일 |
|---|---|---|
| 선행 1 · 채널 간 근거 오염 | **코드 수정 완료** | 운영 조치 — 아래 §선행 1 |
| 선행 2 · 파일 줄 시각 | **완료** | 없음 |
| 1단계 · 생성 요약 후보 | **코드 구현 완료** | 스키마 적용 · 실측 |
| 2단계 · 기간 파생 요약 | 미착수 | 1단계 실측 뒤 판단 |
| 3단계 · 중복 정리 | 미착수 | 1단계 뒤 |

**다음 사람이 할 일은 두 가지다.**

1. 배포 후 스키마 적용(`summary_review_candidate.form` 이 생긴다).
   ```bash
   sudo /opt/tybot/deploy/update.sh
   sudo -u postgres psql -p 55432 -d tyslackai -c '\d summary_review_candidate' | grep form
   ```
2. **1단계 실측.** 실제 채널에서 30일 소급을 돌린 뒤 진단 스크립트로 잰다.
   이 수치가 2단계 착수 판단의 근거다 — 없으면 2단계를 시작하지 않는다.
   ```bash
   sudo -u tybot /opt/tybot/.venv/bin/python        /opt/tybot/scripts/diagnose_summary_review.py --days 30
   ```
   읽는 법: 후보 수가 줄고 `abstract` 승인률이 `quote` 와 비슷하면 묶기가 통한
   것이다. `abstract` 승인률만 낮으면 생성 문장이 원문을 잘못 묶고 있다는 뜻이고,
   그때는 2단계를 시작하지 않고 계약을 먼저 고친다.

   승인률의 분모는 **사람이 결정한 것뿐**이다(`approved` + `rejected`). 보류는
   결정이 아니고 자동 폐기는 사람이 한 일이 아니다 — 둘을 넣으면 그 수치는 사람의
   판단과 무관해진다. 결정이 하나도 없으면 `0%` 가 아니라 `-` 로 보인다.

## 왜 필요한가

담당자·검토자가 지정되지 않은 채로 몇 달치가 수집된 채널이 많다. 소급 검토(B-58)로
읽을 수는 있게 됐지만, 30일치를 돌리면 후보가 수십 건 나오고 한 회차 DM 은 10건만
싣는다. 검토자 앞에 다 도착하는 데만 며칠이 걸린다.

**양이 문제지 깊이가 아니다.** 지금 요약은 원문을 그대로 오려 내는 추출(extractive)
방식이라, 같은 사실이 여러 날에 걸쳐 나오면 그 수만큼 후보가 된다.

```python
# summary_review.parse_proposals — 제안 문장이 원문 인용과 글자 단위로 같아야 통과
if _normalized(proposed) != quote_norm:
    continue
```

생성(abstractive)을 허용하면 그 수십 건이 몇 건으로 묶인다. 대신 모델이 문장을 지어낼
수 있으므로 **검토자가 본다** — 그게 이 설계의 축이다.

## 무엇을 바꾸지 않는가

- 숫자·날짜·금액은 **원문 인용에 있는 것만** 쓸 수 있다. 기존 검사를 그대로 쓴다.
  ```python
  if not _numbers(proposed) <= (_numbers(quote) | _numbers(current)):
      continue
  ```
- 출처는 끝까지 **원문 링크**다(원칙 2). 파생 문장도 좌표를 상속한다.
- 승인 전 후보는 어떤 답변의 근거도 될 수 없다. B-56 결론 그대로다.
- 권한은 요청 시점에 **원문 좌표로** 다시 판정한다.

---

## 착수 전에 반드시 끝낼 것

아래 둘을 먼저 처리한다. 순서를 바꾸지 않는다 — 근거 귀속이 틀린 상태에서 생성
요약을 얹으면, 틀린 귀속이 사람 승인을 거쳐 **사실로 굳는다.**

### 선행 1 — 채널 간 근거 오염 (B-61, 원인 확정·수정 완료)

2026-09-17 QC 에서 발견. `#팀-전산_abb155-전산팀장보고` 의 요약 검토 Canvas
(`[3b216564]`)에 `#팀-전산_abb155-공지` 의 내용이 들어갔다.

**원인.** 프론트매터에 채널명을 따옴표 없이 적으면 파서가 `#` 를 주석으로 읽는다.

```yaml
channel: #팀-전산_abb155-공지     # → 표시명이 빈 문자열이 된다 (YAML 도 같게 읽는다)
```

`ArchiveStore.docs()` 는 표시명을 마이그레이션 별칭으로 쓴다 — `channel_id` 가 없는
문서를 같은 이름의 진짜 채널에 붙이는 장치다. 그런데 표시명이 **빈 문자열**이면
그런 문서 전부가 `(워크스페이스, "")` 라는 **하나의 키**를 공유한다. 그 키에 걸린
진짜 ID 가 하나뿐이면 서로 관계없는 문서가 전부 그 채널로 편입되고, `_merge()` 가
한 문서로 합치면서 `channel_id` 를 그 진짜 ID 로 단다. 그 뒤로 `channel_source()` 는
**다른 채널의 줄을 그 채널의 근거로** 돌려준다.

지금 writer 는 `channel: "{name}"` 로 따옴표를 붙이지만, v1 아카이브
(`/var/lib/tybot/archive/channels`, 운영 로그 기준 5개 파일)에는 옛 모양이 남아 있다.

**수정.** `ArchiveStore.docs()`:

- 표시명이 비어 있으면 **별칭을 만들지 않는다**
- 진짜 ID 도 표시명도 없는 문서는 자기 파일 경로를 신원으로 삼아 **혼자 남는다**.
  가까운 채널로 밀어 넣으면 그 채널 검토자가 본 적 없는 자료를 승인하게 된다
- 그 문서는 경고 로그로 드러난다(조용히 사라지지 않게)

경계는 [`tests/test_archive_channel_isolation.py`](../../tests/test_archive_channel_isolation.py)
가 고정한다. 진짜 채널 둘, 표시명이 겹치는 둘, 이름이 읽히는 v1, 이름이 안 읽히는 v1,
주인 없는 v1 — 다섯 경우다.

**남은 운영 조치.** 수정으로 오염은 멈췄지만, 표시명을 못 읽는 v1 문서는 이제 **어느
채널의 근거도 아니다**. 그 내용이 필요하면 옮겨야 한다.

```bash
# 어느 문서가 어느 채널에도 안 붙는지 — 파일 경로와 조치까지 찍는다
sudo -u tybot /opt/tybot/.venv/bin/python     /opt/tybot/scripts/diagnose_collection.py --archive /var/lib/tybot/archive
```

두 종류로 나뉜다. 조치가 다르므로 문장도 다르다.

- 🔴 **표시명과 `channel_id` 가 둘 다 없다** — `channel:` 값에 따옴표가 없어 `#` 뒤가
  잘린 경우다. `channel: "#팀-전산_abb155-공지"` 로 고치거나 v2 로 이전한다.
- 🟡 **그 이름의 v2 문서가 없다** — 채널명이 바뀐 뒤 옛 이름만 남은 경우다. 지금
  채널명을 확인해 맞춘다.

해당 파일의 `channel:` 값에 따옴표를 붙이거나 v2 로 이전하면 원래 채널로 돌아온다.
**고치기 전에 그 채널의 요약에 빠진 근거가 있다는 것을 검토자에게 알린다.**

### 선행 2 — 파일 줄의 시각이 수집 시각이다 (완료)

[`scripts/backfill_channel_history.py`](../../scripts/backfill_channel_history.py) 는
채널 파일에서 뽑은 줄을 이렇게 쓴다.

```python
writer.IncomingMessage(ts=datetime.now(UTC), speaker="채널 파일", text=line)
```

`writer.ingest()` 는 이 `ts` 로 저장 경로(`raw/<날짜>.md`)를 정하고,
`SourceLine.at` 도 같은 값이 된다. 결과로 **몇 년 전 문서가 「오늘 수집 내용」 으로
요약에 들어간다.** 위 Canvas 가 오늘 자 일일 요약인데 2027-02-15 기한을 담고 있는
것이 그 증상이다.

**수정 완료.** `SlackFile.created` 를 staging 결과까지 실어 나르고
`files.staged_line_time()` 이 한 자리에서 판단한다. Slack 이 시각을 주면 그 값을 쓰고,
못 주면 수집 시각을 쓰되 화자를 `채널 파일(올린 시각 미상)` 로 적는다 — 모르는 것을
아는 척하지 않는다. 두 수집 경로(`sync_channel_files.py`,
`backfill_channel_history.py`)가 같은 함수를 지난다.

---

## 1단계 — 생성 요약(abstract) 후보 (코드 구현 완료)

2차 요약은 만들지 않는다. **1차 요약에서 생성 문장을 허용**하는 것까지다.

### 스키마

```sql
-- 후보의 형식. `quote` 는 원문 그대로(지금까지의 전부), `abstract` 는 생성 문장.
ALTER TABLE summary_review_candidate
    ADD COLUMN IF NOT EXISTS form text NOT NULL DEFAULT 'quote';
ALTER TABLE summary_review_candidate
    DROP CONSTRAINT IF EXISTS summary_review_candidate_form;
ALTER TABLE summary_review_candidate
    ADD CONSTRAINT summary_review_candidate_form
    CHECK (form IN ('quote', 'abstract'));
```

기존 행은 전부 `quote` 로 남는다 — 과거 후보가 소급으로 생성문으로 바뀌면 안 된다.

### 계약 (`subbots/hermes/contract/summary-review.md`)

- 후보마다 `form` 을 명시하게 한다.
- `abstract` 는 `evidence_quote` 를 **반드시** 하나 이상 싣는다.
- 여러 줄을 묶은 생성 문장이면 인용도 여러 개다(2단계의 좌표 상속과 같은 모양).

### 검증 (`parse_proposals`)

| 검사 | `quote` | `abstract` |
|---|---|---|
| 인용이 원문에 있는가 | 필수 | **필수** |
| 제안 == 인용 | 필수 | 면제 |
| 숫자가 인용·기존 승인에 있는가 | 필수 | **필수** |
| `current_text` 가 승인 목록에 있는가 | 필수 | 필수 |

`abstract` 에서 푸는 것은 **제안 == 인용 한 줄뿐**이다. 나머지는 그대로 둔다.
이 표를 그대로 테스트로 옮긴다.

### 검토 화면

`abstract` 후보는 **생성 문장과 근거 인용을 나란히** 보여야 한다. 문장만 보여 주면
검토자가 확인할 방법이 없고, 승인은 형식이 된다. Canvas 의 「항목별 근거 원문」 절에
이미 인용이 들어가므로, 표에서도 `form` 을 구분해 보이고 인용을 붙인다.

### 완료 조건

- [x] `abstract` 후보가 원문에 없는 숫자를 담으면 버려진다
- [x] `abstract` 후보가 인용 없이 오면 버려진다
- [x] 인용이 원문에서 안 나오면 버려진다
- [x] **모르는 `form` 은 `quote` 로 읽는다** — 생성문으로 통과시키면 검증이 한 겹
      조용히 사라진다
- [x] `quote` 후보의 판정이 바뀌지 않는다(기존 테스트 전부 통과)
- [x] 검토 Canvas·폴백 DM 에서 생성 문장이 「정리 문장」 으로 표시되고 인용이 바로
      아래 붙는다
- [x] 실측 수단 — `scripts/diagnose_summary_review.py` (읽기 전용, 본문 미출력)
- [ ] **실제 채널에서 30일 소급 시 후보 수가 줄었는지, 승인률이 얼마인지 기록**
      (남은 일 — 위 §진행 상황 2번)

구현 위치: `summary_review.FORM_QUOTE|FORM_ABSTRACT`, `parse_proposals`,
`form_of()` · `form_note()`, `Store.save_run`.
테스트: `tests/test_summary_review.py` (생성 요약 후보 절),
`tests/test_summary_review_canvas.py` (생성 문장 표시 절).

**1단계 결과를 재면 2단계 설계의 근거가 된다.** 승인률이 낮으면 2차 요약은 더
낮을 것이므로 착수 판단이 달라진다.

---

## 2단계 — 기간 파생 요약 (1단계 결과를 보고 착수)

승인된 1차만 입력으로 받는다. 원칙 1의 다섯 조건을 전부 스키마와 코드로 강제한다.

```sql
-- 세대. 1 = 원문에서 뽑은 1차, 2 = 승인된 1차를 묶은 파생. 3 이상은 없다.
ALTER TABLE approved_summary_item
    ADD COLUMN IF NOT EXISTS generation smallint NOT NULL DEFAULT 1;
ALTER TABLE approved_summary_item
    ADD CONSTRAINT approved_summary_item_generation CHECK (generation IN (1, 2));

-- 파생이 묶은 1차. 좌표 상속의 실체이자 연쇄 stale 의 경로다.
CREATE TABLE IF NOT EXISTS derived_summary_source (
    derived_id  uuid NOT NULL REFERENCES summary_review_candidate(id),
    source_id   uuid NOT NULL REFERENCES summary_review_candidate(id),
    PRIMARY KEY (derived_id, source_id)
);
```

지켜야 할 것:

1. **입력은 `generation = 1` 이며 `superseded_at IS NULL AND stale_at IS NULL`** 인
   승인 항목뿐이다. 2차를 입력으로 받는 경로를 만들지 않는다.
2. **좌표 상속** — 파생의 출처는 `derived_summary_source` 를 따라 1차의
   `evidence_locator` · `evidence_message_ts` · `evidence_hash` 를 모은 집합이다.
3. **묶음 상한** — 한 파생이 묶는 1차 수에 상한을 둔다(초안 5). 넘으면 나눈다.
4. **연쇄 stale** — 1차가 `stale_at` 이 되면 그것을 물고 있는 2차도 `stale_at`.
   B-56 의 stale 판정 자리에서 같이 처리한다.
5. **단일 채널** — 파생은 한 `channel_id` 안에서만 묶는다.
6. **답변 시 재확인** — B-56 과 같다. 좌표를 요청자 권한으로 다시 열고,
   **하나라도 못 열면 그 파생 항목을 버린다.**

### 완료 조건

- [ ] 2차를 입력으로 주면 거부된다(스키마 제약으로)
- [ ] 상한을 넘는 묶음이 거부된다
- [ ] 1차가 stale 되면 그것을 문 2차가 같은 실행에서 stale 된다
- [ ] 원문 하나에 권한이 없으면 그 파생이 답변에서 빠진다
- [ ] 파생 문장의 출처가 원문 링크 여러 개로 열린다

---

## 3단계 — 중복 정리

지금 유니크 키에 `source_digest` 가 들어 있어 **구간이 다르면 같은 문장도 중복
저장된다.**

```sql
CONSTRAINT summary_review_candidate_unique
    UNIQUE (workspace, channel_id, source_digest, kind, proposed_text)
```

소급은 구간을 여러 개로 나눠 돌므로 반복되는 사실이 회차마다 쌓인다. `source_digest`
를 빼면 채널당 한 번만 남는다.

1단계 뒤로 미루는 이유: `abstract` 가 들어오면 중복 양상이 달라진다. 실제 데이터를
보고 정리하는 편이 낫다.

---

## 하지 않기로 한 것

- **3세대 이상 요약** — 분기·연간 요약. 세대마다 정보가 마모되고 어느 세대에서
  틀어졌는지 추적할 수 없다.
- **좌표 없는 파생** — 출처가 원문으로 열리지 않는 문장은 만들지 않는다.
- **여러 채널을 묶은 파생** — 권한이 파생 문장으로 새는 경로다.
- **승인 없는 파생** — 1단계·2단계 모두 사람 승인이 게이트다.
