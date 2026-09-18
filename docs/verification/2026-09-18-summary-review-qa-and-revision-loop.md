# 요약 검토 QA와 자유 텍스트 재작성 흐름

> 점검일: 2026-09-18  
> 기준 커밋: `32e6bc1`  
> 범위: B-50, B-58~B-62 요약 후보 생성·Canvas·검토 DM

## 결론

현재 구현은 후보 생성과 항목별 승인에는 동작하지만, 운영에서 요구한 두 가지를
완료하지 못한다.

1. B-61 수정 전에 생성된 오염 후보가 PostgreSQL에서 `pending`으로 남아 오늘 날짜
   Canvas와 DM으로 다시 발송될 수 있다.
2. `틀리다` 입력은 후보 반려와 피드백 로그 기록으로 끝난다. LLM 재작성과 새 검토
   회차가 없으므로, 항목별 버튼을 자유 텍스트 하나로 바꾸기만 해서는 안 된다.

## QA 발견 사항

### 긴급: 수정 전 대기 후보가 계속 재발송된다

`Store.pending()`은 날짜와 무관하게 해당 채널의 모든 `pending` 후보를 가져온다.
DM 제목의 날짜는 후보 생성일이 아니라 **발송일**이다. 따라서 B-61 배포 전에 다른
채널 원문으로 만들어진 후보도 2026-09-18 Canvas로 다시 보일 수 있다.

기존 후보의 `evidence_locator`는 `2026-09-17.md:42`처럼 파일명과 줄 번호뿐이라
실제 채널을 사후 검증할 수도 없다.

이번 QA 수정:

- 요약 원문은 이름으로 병합된 `ArchiveStore.docs()`가 아니라 실제 파일별
  `source_docs()`에서 읽는다.
- 실제 `channel_id`가 요청 채널과 정확히 같은 문서만 허용한다. channel ID 없는 v1
  문서는 요약 승인 후보에 사용하지 않는다.
- 후보에 `evidence_channel_id`를 저장한다. 모델 출력은 신뢰하지 않고 원문 문서에서
  복사한다.
- 발송 전 `evidence_channel_id != channel_id`인 과거 `pending/deferred` 후보를
  `system:channel-scope-upgrade` 사유로 폐기한다.
- 원문 digest에 파이프라인 버전을 넣어 소급 재생성 시 B-61 이전 실행과 구별한다.

### 높음: 현재 정정은 재작성으로 이어지지 않는다

현재 `REJECT_CALLBACK`은 다음만 수행한다.

1. 틀린 부분 분류와 정정사항 입력
2. 후보 상태를 `rejected`로 변경
3. 피드백 로그 기록
4. 기존 DM 상태 갱신

LLM 호출, 새 후보 생성, 새 Canvas 생성, 재검토 DM 발송은 없다. 사용자는 정정을
입력했지만 수정 결과를 확인할 수 없다.

### 긴급: `abstract`의 비숫자 사실은 원문 대조가 없다

`parse_proposals()`는 `abstract`에 대해 제안문과 인용문이 같은지 검사를 면제한다.
그 뒤 결정적으로 확인하는 것은 제안문의 **숫자가 인용 또는 기존 승인문에 있는가**뿐이다.
기관명, 제품명, 공종, 판단 문구는 검사하지 않는다. 따라서 실제 채널 인용 하나를 붙인
뒤 인용에 없는 비숫자 사실을 제안문에 쓰더라도 통과할 수 있다.

현재 계약은 여러 줄 요약을 말하지만 저장 형식은 `evidence_quote` 한 줄뿐이다. 여러 줄을
근거로 한 생성문을 한 줄 인용으로 검증할 수 있다는 전제가 맞지 않는다. 운영에서 보인
`SG Secukit NX for Client` 항목은 다음 두 경우를 모두 확인해야 한다.

- B-61 이전 다른 채널 후보가 재발송된 경우
- 동일 채널 인용을 붙였지만 `abstract`가 비숫자 사실을 만든 경우

자유 텍스트 revision을 구현하기 전에 `abstract`를 다음 중 하나로 바꿔야 한다.

1. 권장: `evidence_quotes[]`와 각 인용의 `channel_id/message_ts/hash`를 저장하고, 제안문의
   기관명·제품명·날짜·금액을 모든 인용의 합집합과 대조한다.
2. 임시 fail-closed: 다중 근거 계약이 준비될 때까지 `abstract`를 발송하지 않고
   `quote` 후보만 사용한다.

숫자 검사만 유지한 채 revision LLM을 붙이면 사용자가 정정할 때마다 검증되지 않은
비숫자 사실을 새로 만들 수 있으므로 허용하지 않는다.

### 높음: Slack 인터랙션 안에서 LLM을 동기 호출하면 안 된다

Slack action/view 요청은 즉시 `ack()`해야 한다. Hermes가 느리거나 timeout이면 모달
제출이 실패한 것처럼 보인다. 정정 요청은 DB에 먼저 저장하고 review timer가 비동기로
처리해야 한다.

### 높음: 사용자 정정문은 원문이 아니다

사용자가 적은 정정은 LLM의 **수정 지시**일 뿐 답변 근거가 아니다. 정정문을 후보나
아카이브에 그대로 복사하면 사람의 피드백이 사내 원문으로 승격된다. 새 후보는 기존
회차가 가리킨 동일 채널 원문에서 다시 인용하고 현재의 숫자·날짜 대조를 모두 통과해야
한다.

## 목표 UX

검토 DM에는 항목별 `맞다/틀리다/나중에`를 반복하지 않는다.

```text
#채널 · 2026-09-18 요약 검토
[Canvas에서 요약과 출처 읽기]
[전체 내용 맞음] [수정사항 입력]

수정할 내용이 있으면 한 번에 자연어로 적어 주세요.
예: 2번 금액은 도급액이 아니라 기성액이고, 4번 일정은 공지가 아니라 검토안입니다.
```

`수정사항 입력`은 전체 회차 ID를 가진 multiline 모달을 연다. 일반 TYBot DM 질문과
섞이지 않도록 버튼에서 연 모달을 기본 경로로 한다. 추가로 검토 DM의 **스레드 답글**은
`summary_review_delivery.message_ts`가 정확히 일치할 때만 같은 정정 경로로 받을 수 있다.
일반 DM 본문은 요약 정정으로 가로채지 않는다.

## 처리 흐름

1. 사용자가 자유 텍스트 정정사항을 제출한다.
2. 서버는 권한을 `summary_review_delivery(artifact_id, recipient, state='sent')`로 확인한다.
3. `summary_review_revision_request`에 `pending`으로 저장하고 즉시 접수 메시지를 보낸다.
4. `tybot-review-dm.timer`가 요청을 claim한다. Slack 요청 스레드에서 LLM을 기다리지 않는다.
5. LLM 입력은 다음으로 한정한다.
   - 기존 후보 문장
   - 후보별 동일 채널 근거 인용과 좌표
   - 사용자의 수정 지시
   - 현재 승인 요약
6. LLM 출력은 기존 `parse_proposals()`와 같은 검사를 거친다.
   - 인용이 동일 채널 원문에 실제 존재
   - 숫자·금액·날짜는 인용 또는 교체 대상 승인문에 존재
   - 근거 채널 ID가 회차 채널 ID와 일치
7. 검증 후보가 0건이면 기존 회차를 변경하지 않고 사용자에게 재작성 실패 사유를 DM한다.
8. 성공하면 기존 열린 후보를 `superseded`로 닫고 revision 1의 새 후보와 새 Canvas를 만든다.
9. 같은 검토자에게 새 Canvas 링크와 `[전체 내용 맞음] [수정사항 입력]`을 보낸다.
10. 승인되기 전에는 `approved_summary_item`에 아무것도 넣지 않는다.

## 데이터 모델

새 테이블은 원문이나 Canvas 본문을 복제하지 않는다.

```sql
CREATE TABLE summary_review_revision_request (
    id uuid PRIMARY KEY,
    artifact_id uuid NOT NULL REFERENCES summary_review_artifact(id),
    requested_by text NOT NULL,
    instruction text NOT NULL,
    state text NOT NULL DEFAULT 'pending',
    attempt_count integer NOT NULL DEFAULT 0,
    error_code text NOT NULL DEFAULT '',
    result_artifact_id uuid REFERENCES summary_review_artifact(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    CHECK (state IN ('pending','processing','completed','failed'))
);
```

`summary_review_artifact`에는 다음을 추가한다.

- `parent_artifact_id`: 어느 검토본을 수정했는가
- `revision`: 0, 1, 2 ...
- 유일성: `(parent_artifact_id, revision)`

정정문은 이 작업 테이블과 피드백 로그에만 둔다. 아카이브, 승인 요약, 검색 색인,
감사 metadata에는 넣지 않는다.

## 상태와 실패 정책

- 한 회차당 열린 정정 요청은 하나만 허용한다.
- 동일 사용자의 중복 제출은 같은 요청을 반환한다.
- LLM timeout은 최대 3회 재시도한다. Slack action은 이미 접수됐으므로 영향을 받지 않는다.
- revision은 최대 3회로 제한한다. 계속 맞지 않으면 자동 승인하지 않고 운영 확인으로 보낸다.
- 다른 검토자가 먼저 전체 승인한 뒤 도착한 정정 요청은 처리하지 않고 `already-decided`로 닫는다.
- 새 revision 생성에 성공한 뒤에만 이전 열린 후보를 `superseded`로 닫는다.

## 필수 테스트

1. 다른 채널의 원문 파일과 후보는 Canvas 생성 전에 차단된다.
2. `evidence_channel_id`가 비어 있는 과거 대기 후보는 발송되지 않는다.
3. 자유 텍스트가 후보에 그대로 복사되지 않는다.
4. 정정문에만 있는 숫자를 LLM이 쓰면 후보가 폐기된다.
5. LLM 실패 시 기존 회차와 후보 상태가 유지된다.
6. 성공 시 새 Canvas와 DM이 생성되고 이전 회차는 더 이상 승인할 수 없다.
7. 중복 제출·동시 검토자·timer 재시작에도 revision이 하나만 생긴다.
8. 일반 TYBot DM은 활성 검토 회차가 있어도 정정 요청으로 오인하지 않는다.
9. 검토 DM 스레드 답글은 정확한 `message_ts`와 수신자일 때만 정정으로 처리된다.
10. 승인 전 revision 후보는 답변 검색 길잡이에 포함되지 않는다.

## 운영 조치

코드 배포만으로 이미 발송된 잘못된 Canvas가 사라지지는 않는다. 스키마 적용 뒤 해당
채널을 다시 실행하면 좌표 없는 대기 후보는 자동 폐기된다. 그 다음 오염 구간을 소급
재생성해야 한다.

```bash
sudo bash /opt/tybot/deploy/apply-schema.sh
sudo systemctl restart tybot tybot-console
sudo -u tybot env TYBOT_ENV_FILE=/etc/tybot/tybot.env \
  /opt/tybot/.venv/bin/python \
  /opt/tybot/scripts/diagnose_collection.py
```

식별 불가 v1 문서를 먼저 v2로 이전하거나 올바른 채널 ID로 복구한 뒤 콘솔의 해당 채널
`소급 검토`를 실행한다. 기존 오염 Canvas는 승인하지 않고 검토자에게 폐기 사실을 알려야
한다.
