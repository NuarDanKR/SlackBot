# 요약 검토 Canvas + DM 승인 설계

> 상태: 구현 완료 · 2026-09-17 일일 요약·미응답 폐기·출처 링크 보완
> 작성: 2026-09-16
> 담당: Claude 구현, Codex QA
> 백로그: B-50

## 1. 결정

검토자는 긴 요약과 근거를 **Canvas에서 읽고**, 결정은 **DM의 버튼과 모달**에서 한다.

```text
채널 원문·변환 완료 첨부
        ↓
Hermes 요약 후보 + TYBot 근거·숫자 검증
        ↓
채널·검토일별 Canvas 1개 생성
        ↓
검토자 DM: Canvas 링크 + 후보 번호별 버튼
        ├─ 맞다   → 해당 후보 승인
        ├─ 틀리다 → DM에서 정정 모달 → 해당 후보 반려
        └─ 나중에 → 해당 후보만 다음 날 다시 알림
```

Canvas 내부에는 Slack Block Kit 버튼을 넣을 수 없다. 따라서 Canvas만 보내거나 DM만
보내는 방식이 아니라 두 화면을 역할별로 분리한다.

- Canvas: 전체 맥락, 표, 숫자, 근거를 읽는 문서
- DM: 누가 어떤 후보를 승인·반려했는지 남기는 제어 화면

## 2. 사용자가 보는 화면

### 2.1 Canvas

제목 예시:

```text
2026-09-16 전산팀장보고 요약 검토 · TYBot
```

본문 첫 블록은 기존 `canvas_answer.DISCLAIMER`를 사용한다. 별도 문구를 복제하지 않는다.
TYBot 생성 Canvas 표식과 `remember_generated()` 기록을 반드시 남겨 재수집을 막는다.

본문 순서:

1. 검토 대상 채널, 검토 기준일, 후보 수
2. **그날 수집된 채팅·변환 완료 첨부의 핵심 요약**
3. 기존 승인 항목이 있으면 이번 후보를 가상 적용한 누적 예상 요약본
4. 숫자·금액·비율·날짜 확인표
5. 나머지 쟁점 요약
6. 후보별 근거 원문과 Slack 채널 원문 링크
7. 검토 방법 안내

전체 예상 요약본은 LLM을 다시 호출해 만들지 않는다. 현재 `approved_summary_item`의 활성
항목을 기준으로 결정적 코드가 다음처럼 조합한다.

- `current_text`가 있는 후보: 기존의 정확히 같은 승인 문장을 `proposed_text`로 교체
- `current_text`가 없는 후보: 끝에 추가
- `closed_issue`: 기존 문장을 종결 근거 문장으로 교체하며 임의로 삭제하지 않음
- 같은 `current_text`가 두 번 나오거나 찾지 못하면 Canvas 생성을 중단하고 상세 DM 폴백

이 예상본은 **아직 승인된 문서가 아니다.** Canvas에 `검토 전 예상본`이라고 표시하고,
DB의 승인 요약이나 아카이브에는 쓰지 않는다. `content_hash`에는 현재 승인 항목, 후보 ID,
후보 순서, 렌더러 버전을 모두 포함한다.

숫자 확인표 예시:

| 번호 | 구분 | 확인할 값 | 요약 후보 | 근거 |
|---|---|---|---|---|
| 1 | 숫자·일정 | 62.5%, 1,200억원 | 공정률은 62.5%... | 홍길동 · 2026-09-16 |

규칙:

- `summary_review._number_values()`로 원문 순서의 값을 표시한다.
- 금액을 환산하거나 날짜 정밀도를 올리지 않는다.
- 후보 문장과 `evidence_quote`는 현재 검증을 통과한 값을 그대로 쓴다.
- 후보에 없는 설명·평가·결론을 Canvas 렌더러가 만들지 않는다.
- 숫자 후보를 먼저 배치한다.
- Markdown 표는 `canvas_answer.markdown()`의 300셀 분할 규칙을 거친다.
- Canvas에는 첨부 변환 원문 전체, 내부 파일 경로, 질문 본문을 넣지 않는다.
- 변환 완료 첨부에서 추출된 문장이 요약 근거가 된 경우 그 후보 문장과 검증된 인용만
  표시한다. XML·HTML·OCR 덤프를 그대로 싣지 않는다.

### 2.2 검토자 DM

DM 상단:

```text
#전산팀장보고 · 2026-09-16 요약 검토 5건
숫자 확인 3건 · 일반 쟁점 2건
[Canvas에서 전체 요약과 근거 읽기]
```

그 아래에는 후보별로 짧게 표시한다.

```text
1. 숫자·일정 · 62.5% · 1,200억원
[맞다] [틀리다] [나중에]

2. 새 쟁점 · 발주처 협의 일정 변경
[맞다] [틀리다] [나중에]
```

DM에 긴 후보·근거 원문을 다시 복제하지 않는다. Canvas 생성 또는 권한 부여에 실패한
경우에만 현재 `candidate_blocks()`를 사용해 **요약 후보와 근거만** DM에 표시한다.
파일 변환 상세 DM으로 돌아가면 안 된다.

후보가 모두 맞을 때 반복 클릭을 줄이기 위해 상단에 `[전체 맞다]` 버튼을 추가할 수 있다.
이 버튼은 그 Artifact에 속하며 아직 `pending`인 후보만 한 트랜잭션에서 승인한다.
`전체 틀리다`는 만들지 않는다. 여러 후보를 하나의 정정 문장으로 반려하면 무엇이
틀렸는지 다시 구분할 수 없기 때문이다.

## 3. 일부만 맞는 경우

부분 검토는 별도 상태 하나로 뭉치지 않고 **후보별 결정의 조합**으로 표현한다.

예를 들어 5건 중 1·2·4가 맞고 3·5가 틀리면:

- 1·2·4: `맞다`
- 3·5: 각각 `틀리다` → 각각 정정사항 입력

이 방식이면 승인된 후보만 `approved_summary_item`에 반영되고 틀린 후보는 반영되지 않는다.
Canvas는 검토 당시 스냅샷으로 유지하며 사람이 입력한 정정으로 본문을 다시 쓰지 않는다.
한 후보 문장 안에서 일부 값만 맞는 경우에도 그 후보 전체를 `틀리다`로 처리하고,
정정 모달에 올바른 전체 문장을 적는다. 검증되지 않은 절반만 자동 승인하지 않는다.

### 3.1 틀리다 모달

현재 `reject_modal(candidate_id)`를 확장한다.

표시 항목:

- 후보 번호와 짧은 후보 문장: 읽기 전용 section
- 틀린 부분: `static_select`, 필수
  - 숫자·금액
  - 날짜·기간
  - 사실관계
  - 누락
  - 기타
- 올바른 내용/정정사항: multiline, 필수, 5~2000자

`private_metadata`에는 임의 문자열을 이어 붙이지 말고 JSON을 사용한다.

```json
{"artifact_id":"uuid","candidate_id":"uuid"}
```

검증:

- 현재 워크스페이스의 Artifact와 후보인지
- 클릭한 사용자가 이 Artifact의 `summary_review_delivery` 수신자인지
- 후보가 아직 `pending` 또는 `deferred`인지
- 정정사항 최소 길이를 만족하는지

정정사항은 피드백 로그에는 기록하지만 아카이브·승인 요약·감사 metadata에는 넣지 않는다.
감사 이벤트에는 Artifact ID, 후보 ID, 분류 코드, 결정자만 남긴다.

모달 제출 후 DM 응답:

```text
3번 후보를 반려했습니다. 정정사항은 검토 기록에 남겼으며 원문이나 답변 근거를
자동으로 바꾸지 않습니다.
```

## 4. 데이터 모델

새 파일 `deploy/sql/summary_review_canvas_schema.sql`을 만들고
`deploy/apply-schema.sh`의 `summary_review_schema.sql` 다음에 등록한다.

### 4.1 `summary_review_artifact`

```sql
CREATE TABLE summary_review_artifact (
    id              uuid PRIMARY KEY,
    workspace       text NOT NULL,
    channel_id      text NOT NULL,
    channel_name    text NOT NULL DEFAULT '',
    review_date     date NOT NULL,
    source_digest   text NOT NULL,
    state           text NOT NULL DEFAULT 'creating',
    canvas_id       text,
    canvas_permalink text,
    content_hash    text NOT NULL,
    error_code      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    ready_at        timestamptz,
    decided_at      timestamptz,
    CONSTRAINT summary_review_artifact_state
      CHECK (state IN ('creating','ready','partial','completed','failed','ambiguous')),
    UNIQUE (workspace, channel_id, review_date, source_digest)
);
```

`canvas_permalink`는 Slack 내부 링크이며 DB에는 저장해도 되지만 로그·감사 metadata에는
남기지 않는다. Canvas 본문은 DB에 복제하지 않는다.

### 4.2 `summary_review_artifact_candidate`

```sql
CREATE TABLE summary_review_artifact_candidate (
    artifact_id  uuid NOT NULL REFERENCES summary_review_artifact(id),
    candidate_id uuid NOT NULL REFERENCES summary_review_candidate(id),
    position     integer NOT NULL,
    PRIMARY KEY (artifact_id, candidate_id),
    UNIQUE (artifact_id, position)
);
```

Canvas 번호와 후보 ID를 고정한다. `pending()`을 다시 조회한 순서에 의존하면 재시도 때
번호가 바뀔 수 있다.

### 4.3 `summary_review_delivery`

```sql
CREATE TABLE summary_review_delivery (
    artifact_id uuid NOT NULL REFERENCES summary_review_artifact(id),
    recipient   text NOT NULL,
    dm_channel  text,
    message_ts  text,
    state       text NOT NULL DEFAULT 'pending',
    sent_at     timestamptz,
    error_code  text,
    PRIMARY KEY (artifact_id, recipient),
    CONSTRAINT summary_review_delivery_state
      CHECK (state IN ('pending','sent','failed'))
);
```

기존 `review_digest_sent(kind='summary')`는 호환 조회용으로 유지한다. 새 발송 성공 시 같은
트랜잭션에서 기존 이력과 delivery를 함께 기록한다. 후보 결정 권한은 날짜 단위 이력이
아니라 `summary_review_delivery`의 `(artifact_id, recipient, state='sent')`로 검증한다.

모든 새 테이블에 `tyslackai`의 필요한 `SELECT, INSERT, UPDATE` 권한을 스키마 파일에서
직접 부여한다. 수동 SQL 절차를 만들지 않는다.

## 5. Canvas 생성과 멱등성

Slack API와 DB는 하나의 트랜잭션이 아니므로 생성 순서가 중요하다.

1. 후보를 생성하고 검증한다.
2. 활성 검토자를 조회한다. 후보나 검토자가 없으면 Canvas를 만들지 않는다.
3. 고정된 후보 순서와 렌더 결과로 `content_hash`를 계산한다.
4. DB에 Artifact와 후보 매핑을 `creating`으로 먼저 넣는다.
5. `canvas_answer.create()`로 Canvas를 한 번 생성한다.
6. 모든 검토자에게 한 번의 `canvases_access_set` 호출로 읽기 권한을 준다.
7. Canvas ID·permalink를 기록하고 `ready`로 바꾼다.
8. 검토자별 DM을 보내고 delivery와 기존 발송 이력을 기록한다.

제목에는 Artifact ID 앞 8자를 포함한다.

```text
2026-09-16 전산팀장보고 요약 검토 [a1b2c3d4] · TYBot
```

`creating` 상태에서 프로세스가 죽으면 자동으로 새 Canvas를 만들지 않는다. Slack API가
Canvas를 만들고 응답만 유실했을 수 있기 때문이다. 상태를 `ambiguous`로 올리고 콘솔에
표시한다. 같은 Artifact의 중복 Canvas 생성보다 발송 지연을 택한다.

명백한 API 거절처럼 Canvas가 생성되지 않았음이 확실한 경우만 `failed`로 기록하고
재시도한다. Canvas 실패가 요약 검토 전체를 막지는 않는다. 상세 요약 후보 DM으로
폴백한다.

## 6. 권한

- 수신자는 `channel_reviewer.enabled=true`인 사용자만 사용한다.
- 채널 담당자·개설자를 자동 수신자로 추가하지 않는다. 담당자도 보려면 검토자로 등록한다.
- Canvas 읽기 권한은 그 회차의 수신자 목록에만 부여한다. 채널 전체 공유를 하지 않는다.
- 검토자 변경은 다음 Artifact부터 적용한다. 과거 Canvas 접근권한 회수는 별도 정리 작업이
  필요하므로 B-50 완료 조건에 포함한다.
- 결정 API는 delivery 수신자 검증에 실패하면 아무 상태도 바꾸지 않는다.
- 여러 검토자가 동시에 누르면 후보 행을 `FOR UPDATE`로 잠그고 첫 결정만 반영한다.
  나머지는 `이미 다른 검토자가 처리했습니다`로 응답한다.

## 7. 상태 전이

후보 상태는 기존 값을 유지한다.

```text
pending ─ 맞다 ─────→ approved
pending ─ 틀리다 ───→ rejected + correction
pending ─ 나중에 ───→ deferred
deferred ─ 재알림 ───→ pending과 같은 결정 가능
pending ─ 다음 검토일 미응답 ─→ expired
deferred ─ 재알림 날도 미응답 ─→ expired
```

Artifact 상태는 후보 집계로 계산한다.

- `ready`: 아직 결정이 없음
- `partial`: 일부만 결정됨
- `completed`: 모든 후보가 approved 또는 rejected
- 후보가 하나라도 deferred이면 완료가 아니다
- 전부 expired이면 Artifact도 `expired`다. 일부 승인·반려 후 나머지가 만료되면
  처리 회차는 완료되지만 expired 후보는 승인 요약에 들어가지 않는다

`전체 맞다`는 아직 결정되지 않은 후보를 전부 승인한다. 하나라도 다른 검토자가 먼저
결정했다면 그 항목은 건너뛰고 결과 건수를 알려 준다. 이미 반려된 후보를 승인으로
덮어쓰지 않는다.

## 8. DM 갱신

결정 후 `summary_review_delivery.message_ts`가 있는 모든 수신자의 DM을 `chat_update`한다.

- 승인: `1. 맞음 · <@U123>`
- 반려: `3. 수정 필요 · <@U456>`
- 보류: `5. 내일 다시 확인`

정정 본문은 다른 검토자의 DM에 노출하지 않는다. 결정 상태만 보여 준다. `chat_update`
실패는 결정을 롤백하지 않고 비민감 오류 코드만 기록한다.

## 9. 구현 파일

| 파일 | 변경 |
|---|---|
| `deploy/sql/summary_review_canvas_schema.sql` | Artifact·후보 매핑·delivery 테이블 |
| `deploy/apply-schema.sh` | 새 스키마 순서 등록 |
| `src/tybot/summary_review.py` | Artifact store, Canvas 렌더, DM 버튼, batch 승인, 권한 검증 |
| `src/tybot/canvas_answer.py` | 필요하면 여러 사용자 일괄 권한 helper 추가 |
| `src/tybot/daily_review.py` | 요약 실행 결과 로그에 Canvas 생성·폴백 수치 추가 |
| `src/tybot/channel_health.py` | Canvas 생성 실패·모호 상태 진단 |
| `src/tybot/console/app.py` | 요약 검토 현황에 Artifact 상태와 실패 코드 제공 |
| `console-web/...` | Canvas 링크·발송/결정 상태 표시, 본문·정정 내용은 표시 금지 |
| `tests/test_summary_review.py` | 아래 계약 테스트 |

기존 `candidate_blocks()`는 Canvas 실패 시 폴백 화면으로 남긴다. 새 정상 경로는
`canvas_review_blocks(artifact, rows)`처럼 별도 함수로 만든다.

## 10. 필수 테스트

### 생성·발송

- 같은 워크스페이스·채널·날짜·source digest 재실행 시 Canvas가 하나만 생성된다.
- 여러 검토자에게 같은 Canvas ID가 전달된다.
- 검토자가 아닌 담당자에게 Canvas 권한이나 DM이 가지 않는다.
- Canvas 생성 실패 시 요약 후보 DM으로 폴백하며 첨부 변환 상세는 나오지 않는다.
- `ambiguous` 상태에서는 자동 재생성하지 않는다.
- TYBot 생성 Canvas가 수집·백필·채널 파일 동기화에서 제외된다.

### 숫자·내용

- 숫자 후보가 Canvas 첫 표와 DM 첫 항목에 온다.
- Canvas의 숫자가 `proposed_text`와 `evidence_quote`에 없는 값으로 바뀌지 않는다.
- 금액 환산·날짜 정밀도 상승·반올림이 없다.
- 300셀을 넘는 표가 기존 분할 규칙을 통과한다.
- XML/HTML/OCR 원문 덤프가 Canvas에 그대로 노출되지 않는다.

### 결정·부분 정정

- 후보 5건 중 3건 승인·2건 반려 시 승인본은 정확히 3건만 생긴다.
- 반려 후보마다 정정사항이 필수다.
- 정정사항은 피드백 로그에는 남고 아카이브·승인 요약·감사 metadata에는 없다.
- 검토자가 아닌 사용자가 action payload를 재현해도 결정할 수 없다.
- 두 검토자의 동시 승인에서 한 번만 반영된다.
- `전체 맞다`가 이미 반려된 후보를 덮어쓰지 않는다.
- DM 갱신 실패가 이미 끝난 결정을 되돌리지 않는다.

### 배포

- 새 SQL이 `apply-schema.sh --dry-run` 목록에 나온다.
- `check_schema_drift.py`가 새 테이블·권한 누락을 잡는다.
- `pytest`, `ruff`, 콘솔 타입 검사·빌드가 통과한다.

## 11. 구현하지 않을 것

- Canvas 본문을 사용자가 직접 고친 내용을 승인 요약으로 읽어오지 않는다.
- 정정사항을 답변 근거나 아카이브 원문으로 사용하지 않는다.
- 파일 변환 상세 DM을 되살리지 않는다.
- Canvas를 채널 전체에 공유하지 않는다.
- 모델이 후보 밖의 표 설명이나 숫자를 새로 만들게 하지 않는다.

## 12. 완료 조건

1. 검토자는 DM 링크로 한 개의 Canvas를 열어 전체 요약을 읽는다.
2. DM에서 각 후보를 맞다·틀리다·나중에 처리한다.
3. 일부만 맞으면 틀린 후보마다 정정사항을 입력할 수 있다.
4. 숫자 후보가 먼저 보이고 원문 값과 일치한다.
5. 권한 없는 사용자·채널에 Canvas나 결정 권한이 노출되지 않는다.
6. 재실행·장애·동시 클릭에도 중복 Canvas와 중복 승인이 생기지 않는다.
7. 미응답 후보는 다음 검토일에 폐기되고 승인 요약에 반영되지 않는다.
8. Canvas에서 검토 대상 채널의 Slack 원문 링크를 열 수 있다.
