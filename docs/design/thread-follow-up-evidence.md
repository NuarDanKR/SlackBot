# Slack 스레드 후속 질문과 근거 연속성

> 작성: 2026-09-11  
> 구현 담당: Claude  
> 검증 담당: Codex  
> 상태: 구현 완료 (2026-09-11) · Codex 검증 대기

## 1. 문제와 목표

같은 Slack 스레드에서 사용자가 직전 답변을 이어서 물어도 현재 TYBot은 대화의
지칭 범위를 안정적으로 유지하지 못한다.

실제 재현 흐름은 다음과 같다.

1. 사용자가 광주도시철도 미수금 현황을 묻는다.
2. TYBot은 미수금 수치와 함께 특정 PDF가 변환되지 않았다고 답한다.
3. 사용자가 `처리 안 된 문서도 다시 확인해줘`라고 한다.
4. TYBot은 직전 답변의 해당 PDF가 아니라 채널 전체의 변환 실패 문서를 나열한다.
5. 사용자가 `방금 너와 나눈 대화 중 미수금 내용을 요약하고 변환 실패도 알려줘`라고 한다.
6. TYBot은 방금 대화에 미수금 내용이 없다고 답하거나 관계없는 파일까지 붙인다.

이 변경의 목표는 다음과 같다.

- `방금`, `그 문서`, `관련 파일`, `처리 안 된 문서`가 같은 스레드의 어떤 결과를
  가리키는지 구조적으로 해석한다.
- 이전 **봇 답변 문장**을 사실 근거로 재사용하지 않는다.
- 이전 답변이 사용한 **원문 식별자**를 현재 사용자의 권한으로 다시 검증하고 읽는다.
- 채널 질문은 항상 현재 채널로 제한한다. DM만 사용자가 볼 수 있는 여러 채널을 합칠 수 있다.
- 변환 상태는 아카이브 문장 검색이 아니라 현재 첨부 메타데이터에서 조회한다.
- 검증된 원문만 Hermes에 전달한다.

## 2. 하지 않을 것

- 장기 대화 기억이나 사용자 프로필 메모리를 만들지 않는다.
- 이전 TYBot 답변, 요약, Canvas를 아카이브 근거나 Hermes 입력으로 쓰지 않는다.
- 후속 질문이라는 이유로 다른 채널이나 워크스페이스까지 범위를 넓히지 않는다.
- LLM이 반환한 파일명, 경로, source ID를 검증 없이 신뢰하지 않는다.
- Hermes가 ACL, 출처, 첨부 상태 또는 검색 범위를 결정하게 하지 않는다.
- 첨부 변환 재실행 자체를 이 작업에 포함하지 않는다. 이 작업은 정확한 상태 조회와
  관련 파일 식별까지 담당한다.

## 3. 현재 원인

현재 구현에는 다음 구조적 한계가 있다.

| 위치 | 현재 동작 | 문제 |
|---|---|---|
| `slack/pilot.py` | 최근 문답 문자열을 최대 6,000자로 planner에 전달 | 긴 답변 때문에 앞선 관련 문답이 밀려난다 |
| `audit.py` | 질문과 답변을 각각 4,000자로 잘라 JSONL에 기록 | 후속 질문의 안정적인 참조가 아니다 |
| `intent.py` | `방금`, `이전 대화`를 `memory`로 분류 가능 | 실행 요청이 기억 확인으로 잘못 분류된다 |
| `intent.py` | 답변 문구를 정규식으로 읽어 실패 첨부 하나를 추측 | 문구가 달라지거나 파일이 여러 개면 깨진다 |
| `answer.py` | `Answer`에 표시용 `citations`만 존재 | 원문을 정확히 다시 열 수 있는 식별자가 없다 |
| `answer.py` | 요약은 기간과 채널을 기준으로 전체를 읽음 | `미수금 관련` 같은 주제 한정이 사라진다 |
| `attachment_review.py` | 첨부 상태는 구조화되어 별도 보관 | 후속 질문이 이 구조를 사용하지 않고 과거 문장을 검색한다 |

스레드 문맥은 현재 `AnswerEngine.plan()`에만 들어가며 `respond()`와 Hermes 호출에는
전달되지 않는다. 따라서 planner가 지칭어를 어느 정도 풀어도 실제 검색·요약 단계는
직전 결과의 범위를 모른다.

## 4. 핵심 원칙: 답변이 아니라 근거를 이어 간다

후속 질문은 아래 흐름으로 처리한다.

```text
이전 QA 레코드
  -> 구조화된 원문 참조만 추출
  -> 현재 요청자의 ACL로 다시 검증
  -> 현재 채널 범위와 교집합
  -> 현재 질문의 주제와 교집합
  -> 원문 및 현재 첨부 상태 재조회
  -> Hermes 또는 마스터 답변 생성
  -> 현재 조회 결과로 출처 재생성
```

허용하지 않는 흐름은 다음과 같다.

```text
이전 봇 답변 문자열 -> 새 답변의 사실 근거
```

이 구분은 원문 보존과 요약 재귀 금지 원칙을 지키기 위한 필수 조건이다.

## 5. 데이터 모델

### 5.1 `EvidenceRef`

`src/tybot/evidence_refs.py`를 추가하고 불변 dataclass로 정의한다.

```python
@dataclass(frozen=True)
class EvidenceRef:
    kind: Literal["archive_line", "live_message"]
    workspace: str
    channel_id: str
    document_path: str = ""       # ARCHIVE_DIR 기준 상대 경로
    line_no: int = 0               # 빠른 탐색용 힌트
    source_ts: str = ""
    content_hash: str = ""         # ts, speaker, text의 SHA-256
    message_ts: str = ""           # live_message일 때 사용
```

- 절대 경로와 원문 텍스트는 QA 기록에 넣지 않는다.
- `line_no`만 믿지 않는다. 문서를 다시 열었을 때 `source_ts`와 `content_hash`도 일치해야 한다.
- `content_hash`는 원문을 복원할 수 없는 비교용 값이다.
- 실시간 Slack 근거는 `workspace + channel_id + message_ts`로 식별하고 현재 권한으로
  다시 가져온다. 재조회할 수 없으면 근거에서 제외한다.

### 5.2 `AttachmentRef`

```python
@dataclass(frozen=True)
class AttachmentRef:
    workspace: str
    channel_id: str
    file_id: str
```

- 파일명, 변환 오류 문자열, 추출 본문을 참조값에 복제하지 않는다.
- 화면 표시 시 `attachment_review.scan()`의 현재 metadata에서 이름, permalink,
  status, error의 공개 가능한 설명을 읽는다.
- 같은 이름의 파일이 여러 개일 수 있으므로 파일명을 키로 쓰지 않는다.

### 5.3 `Answer`와 `QARecord`

`Answer`에 아래 선택 필드를 추가한다.

```python
evidence_refs: list[EvidenceRef] = field(default_factory=list)
attachment_refs: list[AttachmentRef] = field(default_factory=list)
subject_terms: list[str] = field(default_factory=list)
context_parent_ids: list[str] = field(default_factory=list)
context_resolution: str = "none"
```

`QARecord`에도 JSON 직렬화 가능한 같은 메타데이터를 선택 필드로 추가한다. 기존 JSONL은
필드가 없어도 읽혀야 하며 별도 마이그레이션을 요구하지 않는다.

QA 기록에 새로 저장하면 안 되는 값:

- 원문 또는 OCR 본문
- 전문 봇 입력 전문
- 비밀번호, 토큰, API 키
- PII 거절 문서의 추출 내용
- 이전 답변의 추가 복제본

`answer` 필드는 콘솔 감사와 피드백 표시를 위해 기존대로 유지하되 근거 해석에는 사용하지 않는다.

## 6. 근거 참조 생성과 복원

### 6.1 생성

`ArchiveStore.search()`가 반환한 `SearchHit`에서 `EvidenceRef`를 만든다.

- `document_path`: `ArchiveStore.root` 기준 상대 경로
- `line_no`: `RawLine.lineno`
- `source_ts`: `RawLine.ts`
- `content_hash`: `sha256(ts + NUL + speaker + NUL + text)`
- `workspace`, `channel_id`: `ArchiveDoc` 값

최종 답변에 기록하는 참조는 모델에 보낸 전체 후보가 아니라 **최종 답변 출처로 채택된
근거**와 일치해야 한다. 전문 봇 계약이 사용한 source ID를 반환하면 그 ID를
`SearchHit`에 매핑한다. 반환하지 못하는 기존 경로에서는 모델에 실제 전달한 제한된 hit
목록을 기록하되 `context_resolution=transmitted_evidence`로 구분한다.

첨부 참조는 검색 근거에서 발견한 첨부 표시와 staging metadata를 `workspace + channel_id +
file_id`로 연결해서 만든다. 답변 문자열의 `자동 변환 실패...` 문구를 다시 파싱하지 않는다.

### 6.2 복원

`ArchiveStore.resolve_refs(refs, ctx)`를 추가한다.

1. `visible_docs(ctx)`를 먼저 계산한다.
2. 현재 요청이 채널이면 `ctx.channel_id`와 동일한 문서만 허용한다.
3. 상대 경로로 문서 후보를 찾되 path traversal을 거절한다.
4. `line_no` 위치의 hash를 확인한다.
5. 불일치하면 같은 문서 안에서 `source_ts + content_hash`로 한 번 찾는다.
6. 없거나 ACL이 바뀌었으면 해당 참조를 조용히 제외하고 비민감 사유 코드만 남긴다.

복원 실패 때문에 일반 검색으로 채널 전체를 자동 확장해서는 안 된다.

## 7. 스레드 문맥 모델

`QALog.context_for_thread()`는 단순 `question/answer` 문자열 목록 대신 다음 필드를 가진
구조화된 turn을 반환한다.

```text
record_id, ts, question, intent_kind, subject_terms,
evidence_refs, attachment_refs, context_parent_ids
```

- 최신 3건 고정 대신 최대 10건의 메타데이터를 읽고 관련도를 계산한다.
- 문자 예산은 답변 전문이 아니라 질문과 작은 메타데이터에 적용한다.
- 새 레코드는 이전 봇 답변 전문을 planner에 보내지 않는다.
- 참조 필드가 없는 구형 레코드에 한해 잘린 답변을 **지칭어 해석 전용** legacy context로
  사용할 수 있다. 이 문자열은 검색 근거나 Hermes 입력으로 절대 전달하지 않는다.

`slack/pilot.py`의 `_thread_conversation_context()`는 제거하거나 planner 전용 구조화 JSON
생성기로 바꾼다. 문자열에 한국어 설명을 이어 붙이고 6,000자에서 중간 절단하는 방식은
사용하지 않는다.

## 8. 의도와 참조 범위

`Intent`에 다음 필드를 추가한다.

```python
reference_mode: Literal[
    "none", "prior_turn", "prior_topic", "prior_attachments"
] = "none"
topic_terms: list[str] = field(default_factory=list)
include_attachment_status: bool = False
referenced_record_ids: list[str] = field(default_factory=list)
```

LLM planner는 `reference_mode`와 주제만 제안할 수 있다. 실제 QA record ID와 근거 참조는
TYBot 코드가 같은 workspace, channel, thread 안에서 결정한다.

분류 우선순위:

1. 같은 스레드에서 `방금/아까/위에서/그 문서/관련 파일`과 요약·검색·확인 동사가 함께
   있으면 실행 가능한 후속 질문이다. `memory`로 보내지 않는다.
2. `기억해?`, `전에 물어본 적 있어?`처럼 기억 여부 자체를 묻는 경우만 `memory`다.
3. `미수금 관련 내용을 다시 요약`은 `prior_topic`이며 `미수금`을 topic으로 보존한다.
4. `변환 실패한 게 있는지도`는 별도 채널 전체 검색이 아니라 같은 참조 범위에
   `include_attachment_status=True`를 결합한다.
5. 하나의 문장에 요약과 변환 상태 확인이 함께 있으면 서로 독립적인 광범위 task 둘로
   쪼개지 않는다. 하나의 참조 범위를 공유하는 복합 요청으로 처리한다.

기존 `FAILED_ATTACHMENT_NOTE_RE` 기반 추론은 신규 레코드 경로에서 제거한다.

## 9. `ThreadFollowupResolver`

`src/tybot/thread_followup.py`에 결정론적 resolver를 둔다.

입력:

- 현재 `Intent`
- 현재 `RequestContext`
- workspace, channel_id, thread_ts
- 같은 스레드의 구조화된 QA turn

출력:

```python
@dataclass
class ResolvedFollowup:
    evidence_hits: list[SearchHit]
    attachments: list[Attachment]
    parent_record_ids: list[str]
    topic_terms: list[str]
    resolution: str
    dropped_codes: list[str]
    needs_clarification: bool = False
```

처리 순서:

1. 같은 workspace, channel_id, thread_ts의 turn만 받는다.
2. 명시된 topic과 최근 질문의 `subject_terms`로 관련 turn을 고른다.
3. `prior_turn`은 가장 최근의 관련 결과, `prior_topic`은 topic이 일치하는 최근 결과,
   `prior_attachments`는 선택된 결과의 첨부 참조를 사용한다.
4. `ArchiveStore.resolve_refs()`로 현재 ACL을 다시 적용한다.
5. 채널 요청이면 현재 채널과 다시 교집합한다.
6. topic이 있으면 복원된 hit 안에서 topic 검색을 수행한다.
7. 첨부 상태 요청은 선택된 `AttachmentRef`만 현재 metadata와 결합한다.
8. 파일이 여러 개라 지칭 대상을 확정할 수 없으면 최대 몇 개의 안전한 파일명만 제시해
   사용자에게 선택을 요청한다. 채널 전체 실패 목록으로 넓히지 않는다.

DM에서는 사용자가 현재 접근 가능한 채널의 참조만 복원한다. 이전 답변 이후 멤버십이
바뀌었다면 과거에 보였던 자료라도 제외한다.

## 10. 주제 한정 요약

`AnswerEngine.summarize()`는 기간만 받는 현재 구조를 확장한다.

```python
summarize(
    ctx,
    days,
    *,
    question,
    terms=None,
    evidence_hits=None,
    attachments=None,
)
```

- `evidence_hits`가 있으면 그 범위를 벗어나 새 검색을 하지 않는다.
- `terms`가 있으면 먼저 해당 주제의 원문 줄을 고른 후 요약한다.
- 후속 질문의 유효 범위는 아래 교집합이다.

```text
현재 ACL ∩ 현재 채널 ∩ 이전 답변의 원문 참조 ∩ 현재 topic
```

- `미수금 관련`인데 해당 교집합에 근거가 없으면 다른 주제 문서를 끌어오지 않고
  근거를 다시 찾지 못했다고 답한다.
- 사람의 평가나 의견은 `RawLine.speaker`와 시각을 함께 전달해 `조민희 팀장이 평가했다`처럼
  귀속을 보존한다. 모델 자신의 평가처럼 표현하지 않는다.

## 11. 첨부 변환 상태

변환 상태는 `attachment_review.scan()`의 최신 metadata가 유일한 기준이다.

표시 상태는 최소한 다음을 구분한다.

- 변환 완료
- 변환 실패
- 지원하지 않는 형식
- 다운로드 또는 추출 실패
- PII 차단
- 원본 없음
- 현재 metadata 없음

과거 채팅이나 아카이브에 `처리실패`라고 적혀 있어도 현재 metadata가 `converted`이면
현재 상태를 우선한다. 필요하면 `이전에는 실패했으나 현재 변환 완료`처럼 상태 변경을
표시하되 과거 봇 문장을 사실 근거로 쓰지 않는다.

PII 차단 첨부는 `public_failure_reason()` 수준의 안전한 설명만 사용한다. OCR 추출 내용은
planner, Hermes, QA JSONL에 전달하지 않는다.

## 12. Hermes 경계

Hermes는 대화 기억을 직접 관리하지 않는다.

TYBot이 다음을 끝낸 뒤 Hermes를 호출한다.

1. 후속 질문의 참조 turn 결정
2. 현재 ACL 및 채널 범위 검증
3. 원문 참조 복원
4. topic 필터
5. PII 필터
6. 현재 첨부 상태 결합

Hermes 입력의 `<원문>`에는 위 절차를 통과한 원문만 넣는다. 필요한 경우 별도의
`<요청_맥락>`에 `직전 미수금 답변을 다시 요약` 같은 작업 지시를 넣을 수 있지만 이전
봇 답변 본문은 넣지 않는다. 출처 링크는 TYBot이 복원한 참조로 최종 부착한다.

Hermes가 반환한 출처 ID가 허용 목록에 없으면 계약 위반으로 처리하고 마스터 봇으로
폴백한다.

## 13. 사용자 응답 규칙

- 지칭이 명확하면 별도 확인 질문 없이 처리한다.
- 지칭이 모호하면 범위를 넓히지 말고 짧게 대상을 확인한다.
- 과거 참조가 현재 권한으로 보이지 않으면 `현재 권한으로 다시 확인할 수 없습니다`라고
  알리고 내용을 재현하지 않는다.
- 원문은 찾았지만 관련 첨부 metadata가 없으면 `관련 파일의 현재 변환 상태를 확인하지
  못했습니다`라고 구분한다.
- 출처는 이번 요청에서 다시 검증된 원문과 원본 링크로 새로 만든다.
- 채널 MD는 사람 대화 자체이므로 사용자 화면의 출처 목록에서는 숨길 수 있지만,
  내부 `EvidenceRef`와 Slack 메시지 링크는 유지한다.

## 14. 관측성과 감사

업무 본문을 로그에 남기지 않고 다음 코드와 개수만 기록한다.

```text
followup_resolution=prior_topic
parent_records=1
refs_requested=4
refs_resolved=3
refs_dropped=1
attachments_resolved=1
dropped_codes=permission_changed|source_missing|hash_mismatch
```

QA 레코드에는 `context_resolution`, `context_parent_ids`, 참조 ID를 남겨 콘솔에서 왜 해당
범위로 답했는지 추적할 수 있게 한다. 원문, OCR 본문, 질문에 포함된 PII는 새 메타데이터에
복제하지 않는다.

## 15. 구현 순서

각 단계는 독립 커밋으로 나누고 단계마다 테스트를 함께 넣는다.

1. `EvidenceRef`, `AttachmentRef`, JSON 직렬화와 구형 QA 레코드 호환 구현
2. `SearchHit -> EvidenceRef` 생성 및 `ArchiveStore.resolve_refs()` 구현
3. 답변·요약·전문 봇 경로에서 최종 근거 참조를 `Answer`와 `QARecord`에 기록
4. 구조화된 `context_for_thread()`와 `ThreadFollowupResolver` 구현
5. `Intent`의 reference/topic/attachment 상태 필드와 분류 우선순위 수정
6. 주제 및 이전 근거 범위가 제한된 `AnswerEngine.summarize/respond` 구현
7. 현재 attachment metadata를 참조 범위에 결합
8. Hermes 호출 전 ACL·PII·허용 source ID 계약 검사 추가
9. 회귀·권한·보안 테스트와 운영 로그 추가
10. 구형 QA 레코드 fallback을 제한적으로 유지하고 제거 조건 문서화

이 작업은 `subbots/hermes`의 문서 변환 코드와 독립적이어야 한다. Hermes 소스 변경이
필요한 경우에도 먼저 TYBot의 specialist contract 변경으로 분리한다.

## 16. 필수 테스트

### 실제 대화 회귀

아래 3단계 대화를 고정 fixture로 만든다.

1. `광주도시철도 미수금 현황을 정리해줘`
2. `처리 안 된 문서도 다시 확인해줘`
3. `방금 너와 나눈 대화 중 미수금 관련 내용을 요약하고 파일 변환 실패도 알려줘`

기대 결과:

- 2번은 직전 답변과 연결된 PDF만 확인하고 채널 전체 실패 목록을 내놓지 않는다.
- 3번은 재검증된 원문에서 원기성금액, 회수금액, 미수금액을 요약한다.
- 관련 실패 파일만 현재 metadata 상태로 알려준다.
- `표 이해도 20% 미만` 같은 사람 평가를 포함하면 발언자를 명시한다.
- 이전 봇 답변 문자열만으로 숫자를 재현하지 않는다.

### 분류와 문맥

- 실행 동사가 있는 `방금 대화` 요청은 `memory`가 아니다.
- `이전 답변을 기억하니?`는 기존 `memory` 동작을 유지한다.
- 이전 답변이 4,000자를 넘고 스레드 전체가 6,000자를 넘어도 refs로 연결된다.
- 복합 질문의 요약과 첨부 상태가 동일한 prior scope를 공유한다.
- 구형 QA 레코드에 refs가 없으면 좁은 fallback 또는 명확화 질문을 하고 전체 검색하지 않는다.

### 원문 및 첨부

- line number가 바뀌어도 hash가 일치하면 복원한다.
- hash가 다르면 이전 텍스트를 사용하지 않고 제외한다.
- 같은 파일명의 서로 다른 file ID가 섞이지 않는다.
- 과거 실패 문장과 현재 `converted` metadata가 충돌하면 현재 metadata가 이긴다.
- PII 차단 파일의 추출 본문이 planner, Hermes, QA 기록에 들어가지 않는다.

### 권한

- 채널 A의 후속 질문은 이전 레코드에 B, C 참조가 잘못 섞여 있어도 A만 사용한다.
- 이전 답변 후 채널 멤버십이 제거되면 해당 근거를 재사용하지 않는다.
- DM은 현재 사용자가 볼 수 있는 채널만 통합한다.
- root workspace라도 채널 안에서 물으면 현재 채널만 사용한다.
- Hermes mock이 받은 입력에 ACL 필터 전 원문과 이전 봇 답변이 없는지 검사한다.

### 회귀

- 일반 단일 검색, 기간 요약, Canvas 응답, 출처 링크, 피드백 연결을 유지한다.
- `QARecord` 구형 JSONL 로딩과 콘솔 질문 기록 API가 깨지지 않는다.
- `pytest`, `ruff check src tests scripts`를 모두 통과한다.

## 17. 완료 조건

다음 조건을 모두 만족해야 완료다.

- 실제 재현 3단계 대화 테스트가 통과한다.
- 이전 봇 답변을 변조해도 최종 답변 사실이 바뀌지 않는다.
- 채널 간 정보 누출 테스트가 통과한다.
- 관련 파일만 현재 변환 상태로 표시한다.
- 모든 최종 출처가 이번 요청 시점에 ACL과 원문 hash 검증을 통과한다.
- Hermes 입력에 이전 봇 답변, 권한 밖 원문, PII 차단 본문이 없다.
- 구형 QA 데이터에서도 넓은 채널 전체 검색으로 조용히 폴백하지 않는다.
- 전체 테스트와 lint가 통과한다.

## 18. Codex 검증 순서

Claude 구현 완료 후 Codex는 다음 순서로 검증한다.

1. 변경 커밋과 파일 범위를 확인하고 Hermes 작업과 불필요하게 충돌하지 않았는지 본다.
2. `Answer`와 QA에 원문이 복제되지 않았는지 정적 검사한다.
3. `resolve_refs()`에서 ACL 검사가 경로 조회보다 먼저 적용되는지 코드 리뷰한다.
4. 실제 대화 회귀 테스트, 채널 A/B 누출 테스트, 권한 변경 테스트를 우선 실행한다.
5. specialist mock 입력을 캡처해 이전 답변과 차단 원문이 없는지 확인한다.
6. 전체 `pytest`와 `ruff`를 실행한다.
7. 실패가 없더라도 legacy QA fallback과 실시간 Slack 재조회 경로의 잔여 위험을 별도로 보고한다.

## 19. 구현 메모 (2026-09-11 구현 완료)

구현: `evidence_refs.py`, `thread_followup.py`, `ArchiveStore.resolve_refs()`,
`Intent` 참조 필드, `AnswerEngine._respond_scoped()`, `QARecord` 좌표 필드.
회귀 테스트: `tests/test_thread_followup.py` (28건).

### 19.0 설계와 달라진 점 하나

§6.2-2 는 "현재 요청이 채널이면 `ctx.channel_id` 와 동일한 문서만 허용한다" 고
적었다. `resolve_refs()` 에 그 검사를 따로 두지 **않았다** — `can_access()` 의 0번
판정이 이미 같은 일을 하기 때문이다. 같은 판정을 두 곳에 쓰면 한쪽만 고쳐도
오류가 안 나고, 그게 원칙 3 이 막으려는 모양 그대로다. 되돌림 실험으로 확인했다:
`can_access()` 의 채널 판정을 끄면 `test_root_in_a_channel_still_sees_only_that_channel`
과 `test_a_reference_to_another_channel_is_dropped` 가 함께 깨진다.

### 19.1 구형 QA 레코드 폴백 — 제거 조건

좌표가 없는 레코드에서만 두 가지가 살아 있다.

1. `QALog.context_for_thread()` 가 `legacy_answer` 를 싣는다(600자). **지칭어
   해석 전용**이고 planner 에만 간다 — 검색 근거·전문 봇 입력으로는 넘기지 않는다.
2. `intent._referenced_failed_attachment()` 가 이전 답변 문구에서 실패 첨부
   한 건을 추측한다. `thread_has_refs=True` 면 이 길로 오지 않는다.

**제거 조건**: 운영 QA JSONL 에서 `evidence_refs` 가 없는 레코드가 보존 기간
(현재 월별 파일 2개 = 약 60일)에서 사라지면 둘 다 뗀다. 확인 방법은 아래 한 줄이고,
0 이 나오면 제거해도 스레드 연결이 끊기지 않는다.

```bash
# 최근 두 달치 중 좌표 없는 레코드 수
cat /opt/tybot/qa/qa-*.jsonl | python3 -c \
  "import json,sys; print(sum(1 for l in sys.stdin if l.strip() and not json.loads(l).get('evidence_refs')))"
```

뗄 때 함께 지울 것: `LEGACY_ANSWER_CHARS`, `FAILED_ATTACHMENT_NOTE_RE`,
`_referenced_failed_attachment()`, `_context_fallback()` 의 `thread_has_refs` 분기,
그리고 `tests/test_thread_followup.py::test_a_legacy_record_without_refs_does_not_widen_to_the_whole_channel`.

### 19.2 남아 있는 한계

- **첨부 좌표는 근거 줄에 첨부 표시가 있을 때만 생긴다**(설계 §6.1 그대로).
  검색에 걸린 줄에 그 파일이 안 보이면 다음 턴이 그 파일을 가리킬 수 없다.
  넓히려면 문서 단위로 첨부를 붙여야 하는데, 그러면 첨부 50건짜리 채널에서
  "그 문서" 가 50건을 가리키게 된다 — 좁게 두는 쪽을 골랐다.
- **전문 봇이 도구로 연 문서는 줄 단위 좌표가 없다.** 계약이 source ID 를
  돌려주지 않아, 마스터가 전달한 근거만 좌표로 남는다(`transmitted_evidence`).
  Hermes 계약에 source ID 반환이 들어오면 §6.1 의 매핑 경로로 바꾼다.
- **실시간(`live_message`) 좌표는 아직 되살리지 않는다.** `resolve_refs()` 가
  `live_not_archived` 로 빼고, Slack 재조회는 붙이지 않았다. 재조회를 붙이기
  전까지 실시간 근거는 다음 턴에서 이어지지 않는다.
- **도구 호출 횟수가 `specialist_call` 에 남지 않는다**(전문 봇 검증 문서와 같은 빈틈).
