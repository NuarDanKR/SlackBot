# 요약 검토 주기 콘솔 인계

작성일: 2026-10-06  
수신: 독립 웹 콘솔 담당자

## 소유권 경계

웹 콘솔은 별도 프로젝트와 담당자가 관리한다. 이 저장소에서는 이후
`console-web/` 및 `src/tybot/console/`의 화면·콘솔 API 구현을 직접 수정하지 않는다.
콘솔 변경이 필요하면 이 문서와 같은 Markdown 인계서를 갱신해 전달한다.

커밋 `666d966`은 이 경계가 확정되기 전에 아래 콘솔 파일을 수정했다.

- `console-web/src/pages/Channels.tsx`
- `console-web/src/pages/SummaryReviews.tsx`
- `src/tybot/console/channel_admin.py`
- `src/tybot/console/summary_review_store.py`

이미 배포 이력에 들어간 코드는 즉시 되돌리거나 삭제하지 않는다. 외부 콘솔이 계약을
인수할 때 참고 구현으로만 사용하고, 이후 변경은 외부 프로젝트에서 진행한다.

## 데이터 계약

`channel_reviewer`의 검토 예약은 다음 세 값으로 정한다.

| 필드 | 값 | 규칙 |
|---|---|---|
| `schedule_kind` | `daily` 또는 `weekly` | 기존 행의 기본값은 `daily` |
| `weekday` | `0..6` 또는 `NULL` | Python 기준 월요일 `0`, 일요일 `6` |
| `send_at` | PostgreSQL `time` | KST 기준 검토 DM 발송 시각 |

유효한 조합은 두 가지뿐이다.

```text
daily  + weekday NULL
weekly + weekday 0..6
```

`daily + 요일`, `weekly + NULL`, 범위 밖 요일은 저장 단계에서 거부해야 한다.

## 읽기 API

### `GET /api/channels`

각 `rows[]` 항목에 다음 값이 제공된다.

```json
{
  "reviewers": ["U123"],
  "sendAt": "09:00:00",
  "scheduleKind": "weekly",
  "weekday": 2,
  "scheduleLabel": "매주 수요일 09:00"
}
```

화면에서는 주기 문구를 다시 조립하지 말고 `scheduleLabel`을 우선 표시한다. 값이 없는
구버전 응답에 한해서만 `sendAt`을 대체 표시한다.

### `GET /api/summary-review/rounds`

응답의 `schedule`은 다음 모양이다.

```json
{
  "channels": 3,
  "due": 1,
  "waiting": 2,
  "nextSendAt": "수요일 09:00",
  "timezone": "Asia/Seoul"
}
```

`nextSendAt`은 일간이면 `HH:MM`, 주간이면 `요일 HH:MM`이다. 화면 문구는 “설정 시각”이
아니라 일간·주간을 모두 포괄하는 “예약 시점”을 사용한다.

## 화면 용어와 순서

사용자에게 보이는 명칭은 다음으로 통일한다.

| 이전 | 변경 |
|---|---|
| 채널 수정 담당자 | 채널 관리자 |
| 요약 검토자 | 검토 승인자 |
| 검토 DM 보낼 시각 | 검토 시각 |

채널 수정 화면의 순서는 다음과 같다.

1. 업무명
2. 채널 관리자
3. 요약 검토 주기
4. 주 1회 검토 요일 (`weekly`일 때만 표시하고 필수)
5. 검토 시각
6. 검토 승인자

채널 생성 화면은 관리자를 별도로 고르지 않으므로 다음 순서다.

1. 채널 기본 정보와 추가 참여자
2. 요약 검토 주기
3. 주 1회 검토 요일 (`weekly`일 때만 표시하고 필수)
4. 검토 시각
5. 검토 승인자

`daily`를 고르면 요일 입력을 숨기고 저장값도 `NULL`로 만든다. `weekly`를 고르면 요일을
보인 뒤 선택하기 전에는 저장할 수 없어야 한다. 이전에 선택한 요일이 `daily` 전환 뒤
몰래 제출되면 안 된다.

## 수용 조건

- 일간 화면에는 요일 입력이 없다.
- 주간 화면에는 필수 요일 입력이 있다.
- 주간에서 일간으로 바꾸면 요일 값이 제출되지 않는다.
- 채널 목록은 `매일 09:00` 또는 `매주 수요일 09:00`처럼 주기를 함께 표시한다.
- 검토 현황의 다음 예약 문구가 일간·주간 모두 자연스럽다.
- API가 알 수 없는 `scheduleKind`를 주면 임의로 일간 처리하지 않고 오류 상태로 보인다.
- 시간대는 화면과 요청 모두 `Asia/Seoul`로 명시한다.

## 이 저장소 담당 범위

TYBot/Hermes 담당자는 DB·Slack 모달·발송 스케줄러 계약을 유지한다. 외부 콘솔에서 추가
필드나 쓰기 API가 필요하면 먼저 이 문서에 요청 계약을 적고, 콘솔 구현은 콘솔 담당자가
자기 프로젝트에서 수행한다.
