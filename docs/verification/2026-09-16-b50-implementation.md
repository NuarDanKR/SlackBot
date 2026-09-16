# B-50 구현 기록 — 요약 검토 Canvas + DM 승인

> 작성: 2026-09-16 (Claude)
> 설계: [`docs/design/summary-review-canvas.md`](../design/summary-review-canvas.md)
> 인계: [`2026-09-16-summary-review-canvas-handoff.md`](2026-09-16-summary-review-canvas-handoff.md)
> 상태: 구현 및 Codex 코드 QA 완료 · **운영 Slack 검증 대기**

## 1. 변경 파일

### 새로 만든 것

| 파일 | 하는 일 |
|---|---|
| `deploy/sql/summary_review_canvas_schema.sql` | 회차·후보 매핑·발송 표 3개 + GRANT |
| `src/tybot/console/summary_review_store.py` | 콘솔이 읽는 회차 상태(**본문 없이**) |
| `console-web/src/pages/SummaryReviews.tsx` | `/collect/reviews` 현황 화면 |
| `tests/test_summary_review_canvas.py` | 회차 계약 테스트 34건 |

### 고친 것

| 파일 | 무엇을 |
|---|---|
| `deploy/apply-schema.sh` | `summary_review_schema.sql` **다음에** 새 스키마 등록 |
| `src/tybot/summary_review.py` | 렌더러·Artifact store·DM 블록·모달·전체 맞다·DM 갱신 |
| `src/tybot/canvas_answer.py` | `grant_users()` — 여러 수신자에게 한 번의 호출 |
| `src/tybot/daily_review.py` | 실행 로그에 `canvas/fallback/ambiguous` 수치 |
| `src/tybot/channel_health.py` | `check_review_canvas` 진단 항목 |
| `src/tybot/console/app.py` | `summaryReview` capability + `/api/summary-review/rounds` |
| `src/tybot/slack/pilot.py` | 진단 사실에 마지막 회차 상태 |
| `console-web/src/App.tsx` | `/collect/reviews` 라우트 연결 |
| `tests/test_summary_review.py` | 반려 모달이 「틀린 부분」 을 요구한다 |
| `tests/test_channel_health.py` | 새 진단 항목 fixture·테스트 |

`subbots/hermes` 는 이 작업에서 **수정하지 않았다.**

## 2. DB 마이그레이션

파일 하나. `apply-schema.sh` 에 등록했으므로 별도 수동 SQL 절차는 없다.

```bash
sudo /opt/tybot/deploy/apply-schema.sh --dry-run    # 목록에 새 파일이 보이는지
sudo /opt/tybot/deploy/apply-schema.sh
```

세 표 모두 `CREATE TABLE IF NOT EXISTS` 이고 GRANT 가 같은 파일에 있다 —
`check_schema_drift.py` 가 자동으로 잡는다(테스트로 고정).

**Canvas 본문을 DB 에 복제하지 않는다.** 저장하는 것은 좌표뿐이다.

## 3. 새 상태 전이

### 회차(`summary_review_artifact.state`)

```text
creating ─ Canvas 생성 성공 ─→ ready ─ 일부 결정 ─→ partial ─ 전부 결정 ─→ completed
   │
   ├─ 명백한 API 거절 ────→ failed      (다시 시도한다. 후보 DM 으로 폴백)
   └─ 만들어졌는지 불명 ──→ ambiguous   (자동 재생성 금지. 사람이 본다)
```

- `deferred` 후보가 하나라도 있으면 **`completed` 가 아니다.**
- `failed`·`ambiguous` 는 후보 집계로 덮어쓰지 않는다.

### 후보

기존 값 그대로다(`pending`/`approved`/`rejected`/`deferred`).

### 실패 코드

| 코드 | 뜻 | 다음 상태 |
|---|---|---|
| `missing_scope`·`invalid_auth`·`canvas_disabled` 등 | Slack 이 **분명히 거절** | `failed` |
| `canvas-unknown:<예외이름>` | 만들어졌는지 모른다 | `ambiguous` |
| `canvas-access-failed` | Canvas 는 있는데 권한 부여 실패 | `failed` |
| `canvas-state-unknown` | `creating` 도 아닌데 Canvas ID 가 없다 | `ambiguous` |

## 4. 지킨 순서 — 왜 이 순서인가

Slack API 와 DB 는 하나의 트랜잭션이 아니다.

1. 후보 생성·검증
2. **검토자 조회** — 없으면 Canvas 를 만들지 않는다
3. `content_hash` 계산(승인 항목 + 후보 + 순서 + 렌더러 버전)
4. **Artifact 행과 후보 매핑을 `creating` 으로 먼저 넣는다**
5. `canvas_answer.create()` 한 번
6. `grant_users()` 한 번 — 수신자 전원
7. Canvas ID·permalink 기록, `ready`
8. 수신자별 DM → `summary_review_delivery` 기록

**4를 5보다 먼저 하지 않으면** Slack 이 만들고 응답만 유실됐을 때 DB 에 아무 흔적이
없고, 다음 실행이 같은 회차의 Canvas 를 또 만든다.

**8의 delivery 기록은 DM 을 보낸 뒤다.** 먼저 남기면 못 받은 사람이 결정할 수 있다.

## 5. 권한

- 수신자는 `channel_reviewer.enabled=true` **만**. `run(owners=...)` 는 인자만 남기고
  수신자를 만들지 않는다.
- 결정 권한은 `summary_review_delivery(artifact_id, recipient, state='sent')` 로 본다.
  날짜 단위 `review_digest_sent` 는 **다른 회차의 권한**이라 쓰지 않는다.
- 폴백 DM(회차 없음)은 예전 날짜 단위 검사를 그대로 쓴다. 같은 UPDATE 문 안에서
  둘을 갈라 둬서, 회차가 있으면 회차 쪽만 본다.
- Canvas 읽기 권한은 그 회차 수신자 목록에만 준다. `grant_channel` 을 쓰지 않는다.
- 동시 결정은 **조건부 UPDATE 한 문장**이 막는다. `state IN ('pending','deferred')` 가
  WHERE 에 있으므로 행 잠금을 먼저 잡은 쪽만 바꾸고 나머지는 0행을 받는다.
  읽고 나서 쓰면 그 사이가 벌어진다.

## 6. 테스트

```text
pytest                          2309 passed
ruff check src tests scripts    All checks passed
npx tsc --noEmit                exit 0
npm run build                   built in 2.79s
```

새 테스트 34건(`tests/test_summary_review_canvas.py`)이 덮는 것:

- 예상본 교체·추가·**모호 거부**·삭제 금지
- 숫자 후보 우선(Canvas 첫 표 + DM 첫 항목)
- `content_hash` 가 조회 순서에 흔들리지 않고 내용에는 반응
- 회차 Canvas 가 **수집 제외 표식 세 겹**을 통과
- 버튼·모달이 회차+후보 좌표를 함께 싣고 옛 형식도 읽음
- 결정된 후보는 버튼이 사라지고 **정정 본문은 다른 DM 에 안 보임**
- 재실행이 기존 회차를 재사용(후보 매핑을 다시 쓰지 않음)
- 후보 번호가 **표시 순서**로 고정
- 비수신자·잘못된 회차 ID 는 DB 를 건드리지 않고 거절
- `전체 맞다` 가 이미 결정된 후보를 건너뜀
- 회차 상태 집계(보류 있으면 완료 아님)
- 불확실 실패가 `ambiguous`
- 중단된 기존 `creating` 회차의 Canvas 중복 생성 방지
- 권한 부여 실패 시 기존 Canvas 재사용·권한 재부여

### 되돌리기 실험

고친 곳을 일부러 되돌려 **테스트가 실제로 잡는지** 확인했다.

| 되돌린 것 | 결과 |
|---|---|
| 예상본 모호를 무시하고 그린다 | 1 failed ✅ |
| 불확실 실패를 `failed` 로(재생성 허용) | 1 failed ✅ |
| `전체 맞다` 가 이미 결정된 후보도 덮어씀 | 1 failed ✅ |
| 숫자 우선 정렬 제거 | 3 failed ✅ |
| 후보 번호를 조회 순서로 | 1 failed ✅ |
| 권한 판정을 열리는 쪽으로 | 1 failed ✅ |
| Canvas 진단 항목 제거 | 4 failed ✅ |

## 7. 실제 Slack 에서 확인할 것

DB·API 가 걸린 부분은 **테스트로 증명되지 않는다.** 아래는 사람이 본다.

- [ ] 검토자 2명에게 **같은 Canvas ID** 가 가고, 둘 다 열린다
- [ ] 검토자가 아닌 채널 담당자에게 Canvas 권한도 DM 도 가지 않는다
- [ ] 같은 날 같은 채널을 두 번 돌려도 Canvas 가 하나다
- [ ] 후보 5건 중 3건 승인·2건 반려 후 `approved_summary_item` 이 **정확히 3건**
- [ ] 반려 모달에서 「틀린 부분」 을 안 고르면 제출이 막힌다
- [ ] 다른 검토자 DM 에 정정 본문이 안 보인다
- [ ] 두 사람이 같은 후보를 동시에 눌렀을 때 한 번만 반영된다
- [ ] 회차 Canvas 가 수집·백필·채널 파일 동기화에서 제외된다
- [ ] `chat_update` 를 막아도(예: DM 삭제) 이미 끝난 결정이 되돌아가지 않는다

## 8. `ambiguous` 회차 복구 절차

**자동으로 다시 만들지 않는다.** Slack 이 이미 Canvas 를 만들었을 수 있다.

1. 콘솔 `수집 > 요약 검토 현황` 에서 해당 회차의 채널·검토일을 확인한다.
2. Slack 에서 그 채널의 Canvas 목록을 열고 제목 `[<회차 ID 앞 8자>] · TYBot` 을 찾는다.
3. **있으면** — 그 Canvas 가 정상이다. DB 에 ID 를 채우고 `ready` 로 올린다.

   ```sql
   UPDATE summary_review_artifact
      SET state='ready', canvas_id=:canvas_id, canvas_permalink=:permalink,
          error_code=NULL, ready_at=now()
    WHERE id=:artifact_id;
   ```

   그 뒤 다음 회차 실행이 DM 을 보낸다(`summary_review_delivery` 가 비어 있으므로).

4. **없으면** — 만들어지지 않았다. `failed` 로 내려 다음 실행이 다시 만들게 한다.

   ```sql
   UPDATE summary_review_artifact
      SET state='failed', error_code='recovered-not-created'
    WHERE id=:artifact_id;
   ```

5. 어느 쪽인지 확신이 안 서면 **`ambiguous` 로 둔다.** 중복 Canvas 가 생기면
   검토자가 어느 것에 답해야 하는지 모르게 된다 — 지연보다 나쁘다.

## 9. 설계에서 바꾼 것

### `content_hash` 는 조회 순서에 흔들리지 않는다

설계 §2.1 은 "후보 순서" 를 지문에 넣으라고 했다. 넣되 **표시 순서**
(`ordered_rows`)를 넣었다. 조회 순서를 넣으면 재시도마다 지문이 달라져 같은 회차가
새 회차가 되고, 멱등성이 바로 그 자리에서 깨진다. 테스트로 고정했다.

### `FOR UPDATE` 대신 조건부 UPDATE

설계 §6 은 후보 행을 `FOR UPDATE` 로 잠그라고 했다. 기존 `decide()` 의
`UPDATE ... WHERE state IN ('pending','deferred')` 가 같은 보장을 한 문장으로 준다 —
행 잠금을 먼저 잡은 쪽만 바꾸고 나머지는 0행을 받는다. 읽고-잠그고-쓰는 세 단계로
늘리면 그 사이가 벌어질 자리가 생긴다.

### 깨진 버튼 값은 후보 ID 로 넘기지 않는다

`parse_button_value("{broken")` 은 `("", "")` 다. 그 문자열을 후보 ID 자리에 넘기면
무엇을 결정하려던 것인지 모르는 채로 진행된다.

## 10. 하지 않은 것

- Canvas 본문을 사람이 고친 내용을 승인 요약으로 읽어오지 않는다
- 정정사항을 답변 근거나 아카이브 원문으로 쓰지 않는다
- 파일 변환 상세 DM 을 되살리지 않았다(`daily_review.main()` 그대로)
- Canvas 를 채널 전체에 공유하지 않는다
- `전체 틀리다` 를 만들지 않았다
- 과거 Canvas 접근권한 회수 — **완료 조건에 있으나 별도 정리 작업**이다.
  검토자 변경은 다음 회차부터 적용된다.
- `.claude/settings.local.json` 은 건드리지 않았다

## 11. 동시 작업 중인 것 — 내 변경 밖

다른 에이전트가 `console-web/src/pages/Channels.tsx`, `channel_management.py`,
`console/channel_admin.py`, `specialist_*` 를 같이 고치고 있다. 그쪽 파일은
읽기만 했고 수정하지 않았다.

## 12. Codex QA (2026-09-16)

구현 계약을 검토하고 다음 운영 결함을 수정했다.

1. 같은 날 새 회차도 구형 `review_digest_sent` 때문에 발송이 생략될 수 있었다.
   Canvas 회차는 `summary_review_delivery`를 발송 멱등성과 결정 권한의 기준으로 쓴다.
2. Canvas 생성 후 권한 부여가 실패한 회차를 재실행하면 권한을 다시 주지 않고 기존
   링크를 정상으로 취급했다. 기존 Canvas에 수신자 권한을 재부여한 뒤 `ready`로 복구한다.
3. 프로세스가 Canvas 생성 API 전후에 종료되어 기존 `creating` 행이 남으면 같은
   Canvas를 다시 만들 수 있었다. 새 행은 호출 결과 `new`로 구별하고, 기존
   `creating`은 `ambiguous`로 닫아 사람이 확인하게 한다.
4. 회차 DM 발송 후 구형 날짜 이력 저장이 실패해도 이미 저장된 회차 delivery를
   실패로 덮지 않도록 순서를 고쳤다.
5. `전체 맞다` 처리 중 다른 검토자가 먼저 결정한 후보는 감사 기록에 승인으로
   남지 않게 했다. 조건부 UPDATE에 실제 성공한 후보 ID만 반환해 회차 ID와 함께
   피드백·감사 기록을 남긴다.

검증 결과:

- B-50·Canvas·전문봇 관련 테스트: `238 passed`
- 전체 Ruff: 통과
- 콘솔 TypeScript: 통과
- 전체 pytest: `2283 passed`, `30 failed`
  - 실패 30건은 Windows `bash.exe`가 WSL 미설치 안내를 반환한 Linux 전용 전문봇
    런타임 셸 테스트다. B-50 관련 실패는 없다. Linux 배포 환경에서 전체 재검증한다.
