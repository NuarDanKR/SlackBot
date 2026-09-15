# PII 가드레일 오탐 개선과 TYBot Canvas 산출물

> 작성: 2026-09-15
> 상태: 설계 완료 · Claude 구현 대기
> 관련: B-41, B-44, B-46, B-47, B-49

## 1. 배경과 결정

운영에서 서로 연결된 네 문제가 확인됐다.

1. OCR 결과에 `등기부등본`이라는 단어가 한 번 있다는 이유만으로 비민감 일정표가
   파일 전체 차단됐다.
2. 생성 Canvas의 제목과 본문 제목이 모두 `TYBot 정식 답변`으로 고정돼 문서를
   구별할 수 없다.
3. 사용자가 “공지 내용을 Canvas에 캘린더로 작성”해 달라고 하면 Hermes가 Canvas
   편집은 자기 역할 밖이라고 답한다. 근거 추출은 성공했지만 산출물 요청은 실패한다.
4. Canvas는 Markdown 표를 지원하는데도 긴 서술형 답변이 그대로 들어간다.

다음 두 원칙을 함께 지킨다.

- **PII 보안 자체를 최저로 낮추지 않는다.** 직접 식별자와 실제 고위험 문서는 계속
  차단한다. 낮출 것은 “위험 단어 하나만으로 파일 전체를 차단하는 오탐”이다.
- **Hermes는 근거 기반 업무 답변의 유일한 작성자다.** TYBot 마스터는 질문을
  `근거 조사`와 `표시 산출물`로 나누고, Canvas 생성·제목·공유·렌더링만 수행한다.
  마스터가 아카이브 원문을 보고 별도 업무 답변을 만들지 않는다.

Hermes 저장소와 프롬프트 소스는 이 작업에서 수정하지 않는다. TYBot의 planner,
Hermes 호출용 요청 구성, Canvas renderer만 수정한다.

## 2. 가드레일: 키워드 차단에서 증거 기반 판정으로

### 2.1 현재 원인

`src/tybot/archive/writer.py`의 `PII_PATTERNS`에는 다음이 같은 수준으로 들어 있다.

- 실제 주민등록번호 형식
- `등기부등본`이라는 일반 명사
- `계약자 명단`이라는 일반 명사
- `주민번호`라는 언급

`src/tybot/archive/files.py`는 추출문 전체에서 한 줄이라도 `screen()`에 걸리면
첨부 전체를 `pii_refused`로 바꾼다. 따라서 “등기부등본 제출일” 같은 일정 문장과
실제 등기부등본 원문을 구분하지 못한다.

### 2.2 판정 등급

새 모듈을 권장한다: `src/tybot/pii_screen.py`.

```python
@dataclass(frozen=True)
class Finding:
    code: str
    label: str
    severity: Literal["notice", "block"]
    count: int = 1

@dataclass(frozen=True)
class ScreenResult:
    blocked: bool
    block_code: str = ""
    findings: tuple[Finding, ...] = ()
```

판정은 다음처럼 분리한다.

| 신호 | 예 | 처리 |
|---|---|---|
| 직접 식별자 | 유효한 주민등록번호 형태 | 즉시 파일 전체 차단 |
| 고위험 문서 복합 신호 | 제목/파일명과 `갑구`, `을구`, `소유자`, `등기번호`, `소재지번` 등이 함께 반복 | 파일 전체 차단 |
| 위험 용어만 언급 | `등기부등본 제출`, `계약자 명단 취합 예정`, `주민번호는 수집하지 않음` | 통과 + notice |
| 일반 업무 내용 | 일정, 공정, 금액 등 | 통과 |

`등기부등본`이나 `계약자 명단` 한 단어만으로는 차단하지 않는다. 실제 고위험 문서
분류는 파일명·첫 머리글·필드 표식·반복 횟수의 결정적 점수로 한다. LLM에게 PII 여부를
판정시키지 않는다. 동일 입력은 항상 동일 결과여야 한다.

초기 권장 점수는 코드 상수로 두되 fixture로 고정한다.

```text
파일명/문서 제목이 고위험 문서명과 일치     +3
고유 필드 표식 하나                         +1 (최대 4)
같은 표식이 여러 행에서 반복                 +2
직접 식별자                                 즉시 block
합계 5 이상                                 document-class block
```

점수와 표식은 예시다. Claude는 실제 변환 결과 fixture를 확인해 최소 신호 조합을
고정해야 한다. 점수만 낮춰 실제 등기부등본 fixture가 통과하게 해서는 안 된다.

### 2.3 부분 OCR과 실패 정책

- 직접 식별자를 찾으면 coverage와 무관하게 차단한다.
- 고위험 복합 신호가 임계값 이상이면 차단한다.
- 단일 위험 용어만 있고 OCR이 완료됐다면 통과한다.
- OCR이 부분 성공이고 고위험 복합 신호가 있으면 차단한다.
- OCR이 부분 성공이지만 단일 용어만 있는 경우에는 본문을 통과시키되
  `sensitive-term-partial-coverage` 경고를 남기고 답변의 확인 범위에 표시한다.
- OCR을 전혀 하지 못한 원본은 지금처럼 답변 근거로 사용하지 않는다.

### 2.4 저장과 표시

기존 `pii_refused` 상태는 호환을 위해 유지한다. metadata에는 본문을 넣지 않고 다음만
추가한다.

```json
{
  "screen_version": 2,
  "screen_result": "passed_with_notice",
  "screen_codes": ["sensitive-term-mentioned"]
}
```

콘솔과 서비스 로그에는 코드·파일 좌표만 표시한다. OCR 본문, 주민번호 일부, 사람 이름은
감사 metadata에 복제하지 않는다. 차단 결과의 자동 재처리는 지금처럼 금지한다.

`writer.screen()`은 호환 wrapper로 남기고 직접 식별자만 검사하게 한다. 첨부 전체의
문서 유형 판정은 filename과 full extracted text를 모두 받는 새 `screen_document()`에서
수행한다. 일반 Slack 메시지가 “등기부등본 제출 예정”이라고 말한 것까지 버리지 않는다.

### 2.5 설정 정책

운영 환경변수로 `PII_LEVEL=off` 같은 우회 기능을 만들지 않는다. 콘솔에서도 보안 수준을
낮추는 토글을 제공하지 않는다. 오탐은 판정 로직과 fixture를 고쳐 해결한다. 긴급 진단이
필요하면 결과만 보는 dry-run 도구를 제공하되 아카이브 반영은 하지 않는다.

## 3. Canvas 산출물 계약

### 3.1 마스터 판정 데이터

`Intent`와 `MasterTask`에 표시 요청을 업무 질문과 분리해 담는다.

```python
@dataclass(frozen=True)
class ArtifactRequest:
    delivery: Literal["message", "canvas"] = "message"
    operation: Literal[
        "answer_document", "edit_existing_canvas", "create_calendar_events"
    ] = "answer_document"
    layout: Literal[
        "auto", "report", "table", "timeline", "calendar_grid"
    ] = "auto"
    title: str = ""

@dataclass(frozen=True)
class MasterTask:
    # 기존 필드
    research_question: str = ""
    artifact: ArtifactRequest = field(default_factory=ArtifactRequest)
```

- `original_fragment`: 사용자가 한 말 그대로
- `research_question`: Canvas 생성 동작을 제거하고 Hermes가 근거에서 답할 사실 질문
- `artifact`: TYBot이 결과를 어떻게 표시할지

`캘린더`라는 단어를 코드가 무조건 `calendar_grid`로 치환하지 않는다. planner LLM은
현재 문장, 같은 스레드의 사용자 질문, 요청 위치와 지원 기능 목록을 보고 다음을 구분한다.

| 사용자 의도 | 판정 예 |
|---|---|
| “공지 일정을 Canvas에 달력 형태로 정리” | `canvas / answer_document / calendar_grid` |
| “이 일정을 실제 캘린더에 등록” | `create_calendar_events` |
| “캘린더에 있는 일정이 무엇인지 알려줘” | 일반 근거 질의, Canvas 산출물 없음 |
| “기존 채널 Canvas의 캘린더를 고쳐줘” | `edit_existing_canvas` |
| “공사기간을 한눈에 비교해줘” | 문맥에 따라 `table` 또는 `timeline` |

첫 번째 예시:

```text
원문: 우리 팀 공지 채널 내용을 Canvas에 캘린더로 작성해줘
research_question: 현재 채널 공지에서 일정 항목의 날짜, 내용, 관련 공지를 근거와 함께 정리해줘
artifact.delivery: canvas
artifact.operation: answer_document
artifact.layout: calendar_grid
artifact.title: 전산팀 공지 일정 정리
```

planner는 표시 의도를 판단하지만 실제 실행 권한을 갖지 않는다. 코드는 `operation`을
현재 배포가 지원하는 capability와 대조한다. 현재 허용되는 것은 새 답변 Canvas 생성뿐이다.
기존 Canvas 편집이나 실제 캘린더 등록으로 판단되면 실행했다고 가장하지 않고 지원 여부를
알리거나 필요한 확인을 요청한다.

`artifact.title`은 기존 planner LLM이 함께 제안한다. 별도 제목 LLM 호출을 기본으로
추가하지 않는다. 코드는 제목을 다음 조건으로 검증한다.

- 한 줄, 제어문자·링크·Markdown 문법 없음
- 빈 값이나 지나치게 긴 값은 결정적 fallback 제목 사용
- `TYBot 정식 답변` 금지
- 질문에 없던 사람 이름·금액·날짜를 제목에 새로 넣지 않음

마지막 Canvas 메타데이터 제목은 `{검증된 AI 제목} · TYBot`으로 만든다. 핵심 제목은
AI가 작성하지만 생성 주체 표식은 남긴다.

### 3.2 쓰기 동작과 답변 산출물 구분

`Canvas에 캘린더로 작성`을 마스터가 **새 답변 Canvas의 시각적 일정 정리**로 판단한
경우에는 다음 동작을 허용한다.

- TYBot이 **새 답변 Canvas**를 생성한다.
- 현재 질문 채널에 읽기 권한을 부여한다.
- 일정은 Canvas Markdown 표로 표현한다.

다음은 같은 기능으로 처리하지 않는다.

- 기존 채널 Canvas 수정
- Slack 캘린더 또는 외부 캘린더에 실제 일정 등록
- 기존 문서 삭제·교체

사용자가 실제 편집이나 일정 등록을 명시하거나 문맥상 두 뜻이 모두 가능하면 지원하지
않는 write action 또는 clarification으로 구분한다. 새 답변 Canvas 생성은
`WRITE_KINDS`가 아니라 delivery mode다. LLM 판정이 모호하거나 허용 enum 밖이면
안전한 기본값은 메시지 답변이며, 임의로 캘린더 표를 만들지 않는다.

### 3.3 Hermes 호출

Hermes에는 Canvas를 만들라는 동작 요청을 보내지 않는다. `research_question`과 필요한
표시 힌트만 전달한다.

```text
업무 질문: 현재 채널 공지에서 일정 항목을 근거와 함께 정리해라.
표시 힌트: 반복되는 일정은 날짜/내용/관련 공지/비고 열의 Markdown 표로 답하라.
Canvas 생성과 공유는 호출자가 담당한다.
```

이는 Hermes 소스 변경이 아니다. TYBot의 specialist request adapter가 요청을 구성하는
방식만 바꾼다. 권한 필터와 EvidenceRef 전달은 기존과 동일하다.

Hermes가 여전히 “Canvas를 만들 수 없다”는 실행 거절만 반환하면 같은 전문 봇에 한 번만
다음 보정 요청을 할 수 있다.

```text
Canvas를 생성하지 마라. 제공된 근거로 일정 사실을 답변하는 것만 네 역할이다.
```

무한 재시도하지 않는다. 보정 후에도 사실 답변이 없으면 마스터가 대신 내용을 만들지
않고 `specialist-output-unusable`로 종료한다.

## 4. 제목, Disclaimer와 재귀 방지

현재 `canvas_answer.markdown()`은 본문 첫 줄에도 `# TYBot 정식 답변`을 넣어 Slack의
Canvas 제목과 중복된다. 새 형식은 metadata 제목만 제목으로 사용하고 본문 맨 앞에는
다음 고정 Disclaimer를 둔다.

```markdown
> 이 문서는 TYBot이 요청 시점에 확인 가능한 사내 자료를 바탕으로 생성한 AI 문서입니다.
> 중요한 일정·금액·의사결정은 아래 출처 원문을 확인하세요.
```

Disclaimer 뒤에 생성 시각과 조회 범위를 비민감 값으로 표시할 수 있다. 질문 본문이나
근거 본문을 metadata에 복제하지 않는다.

동적 제목을 도입하면 `archive/canvas.py`의 현재
`title.startswith("TYBot 정식 답변")` 검사가 작동하지 않는다. 원칙 1을 지키기 위해
다음을 함께 구현한다.

1. 새 제목은 항상 ` · TYBot` suffix를 갖는다.
2. 예전 `TYBot 정식 답변` prefix도 계속 제외한다.
3. 본문 첫 Disclaimer도 생성 문서 표식으로 검사한다.
4. 생성된 Canvas ID를 `STATE_DIR/generated-canvases.jsonl`에 비민감 좌표로 기록한다.
5. 수집 경로는 ID, 제목 표식, Disclaimer 중 하나라도 일치하면 답변 Canvas를 제외한다.

상태 기록에는 `canvas_id`, workspace, channel_id, qa_record_id, created_at만 넣고 제목과
본문은 넣지 않는다. ID 기록 실패 시에도 suffix와 Disclaimer가 방어선이다.

## 5. 답변 하네싱: 표와 값의 표시 형식

`canvas_answer.py`는 이미 일반 Markdown 표를 유지하고 Slack의 300-cell 제한을 넘는
표를 행 단위로 나눈다. 새 구현은 이 기능을 재사용하되, 프롬프트만으로 형식을 맞췄다고
간주하지 않는다. `src/tybot/canvas_harness.py`의 결정적 formatter와 validator가 최종
Canvas를 검사한다.

### 5.1 책임 분리

| 단계 | 책임 |
|---|---|
| 마스터 LLM | 문맥에 맞는 표 종류, 비교 축, 사용자가 명시한 목표 단위 판단 |
| Hermes | 근거에 있는 사실과 source ID 작성. 원문값을 임의 환산·보정하지 않음 |
| Canvas harness | 날짜·기간·금액·비율의 표시 형식과 열 스키마 통일 |
| Validator | 새 숫자·날짜·이름 생성, 반올림, 행 누락, 출처 이탈 차단 |

마스터의 `layout`과 목표 단위는 제안이다. 하네스가 정확히 변환할 수 없으면 원문값을
유지하고 `확인 필요`로 표시한다. 보기 좋은 표를 만들기 위해 사실을 추론하지 않는다.

### 5.2 중간 데이터

Hermes 결과를 바로 문자열 표로 쓰지 않고, 최소한 다음 좌표를 가진 typed cell로 옮긴다.

```python
@dataclass(frozen=True)
class HarnessCell:
    field: str
    raw: str
    display: str
    value_kind: Literal["text", "date", "period", "money", "percent"]
    source_id: str
    transform: str = "identity"
    status: Literal["exact", "converted", "unresolved"] = "exact"
```

`raw`와 `source_id`는 반드시 남긴다. `display`만 Canvas에 쓰더라도 validator는
`display`가 `raw`에서 손실 없이 만들어졌는지 검사한다. AI가 새 값을 직접 쓰는 경로를
만들지 않는다.

### 5.3 날짜와 기간

보고 기준일은 모두 같은 정밀도의 날짜가 아니다. 예를 들어 `2026년 9월 2주차`와
`2026년 8월 말`을 임의의 특정 일자로 바꾸면 오답이다. 정밀도를 유지하면서 문법만
통일한다.

| 원문 의미 | 표준 표시 |
|---|---|
| 정확한 일자 | `YYYY-MM-DD` |
| 월말 기준 | `YYYY-MM 말` |
| 주차 기준 | `YYYY-MM N주차` |
| 기간 | `시작일 ~ 종료일` |
| 추가 조건 | 기간 셀과 분리한 `기간 비고` 또는 같은 셀의 괄호 |

- `2026.08월말`은 `2026-08 말`로 바꿀 수 있지만 임의로 `2026-08-31`로 만들지 않는다.
- 두 자리 연도는 주변 근거에 4자리 연도가 명시돼 동일 세기를 확정할 수 있을 때만
  확장한다. 그렇지 않으면 원문을 유지하고 `연도 확인 필요`로 표시한다.
- 공사기간의 구분자는 모든 행에서 ` ~ `로 통일한다.
- 시작·종료일 중 하나가 없으면 채우지 않는다.
- 날짜순 정렬은 파싱이 확정된 행에만 적용한다. 미확정 행은 원래 순서를 유지한다.

따라서 사용자 이미지의 `보고 기준일`과 `공사기간`은 외형은 통일되지만 서로 다른
시간 정밀도는 보존된다.

### 5.4 금액과 단위

한 표의 동일 금액 열은 하나의 단위를 쓴다. 선택 우선순위는 다음과 같다.

1. 사용자가 명시한 단위
2. 워크스페이스 답변 규칙에 명시된 단위
3. 모든 값이 반올림 없이 표현되는 가장 읽기 좋은 공통 단위

지원 단위는 우선 `원`, `천원`, `백만원`, `억원`으로 제한하고 `Decimal`로만 환산한다.
부동소수점 연산을 사용하지 않는다. 모든 값을 같은 단위로 정확하게 표현할 수 없으면
더 작은 단위로 내려간다. 그래도 불가능하거나 원문 단위가 불명확하면 전체 열을 억지로
환산하지 않고 원문값과 `단위 확인 필요`를 표시한다.

- 표 머리글에 단위를 한 번 표시한다: `도급액 (백만원)`.
- 셀마다 `억`, `백만원`을 섞지 않는다.
- `전체/당사`, `계약/실행`처럼 금액의 의미 축은 유지한다.
- 반올림·절삭·임의 천 단위 보정은 금지한다.
- 환산한 표에는 `금액은 표시 단위로 환산했으며 반올림하지 않았습니다`를 남긴다.
- 원문 정밀도와 환산 결과가 맞는지는 역변환으로 검증한다.

### 5.5 비율과 일반 열

- 비율 열은 `%` 위치와 소수 자릿수를 표 단위로 통일한다. 가장 정밀한 원문 자릿수까지
  0을 덧붙일 수는 있지만 숫자를 반올림하지 않는다.
- 기관명·현장명·사람 이름은 표기 통일을 이유로 고쳐 쓰지 않는다.
- 자료에 없는 값은 `-`로 표시하고 추정하지 않는다.
- 같은 표의 모든 행은 같은 열 개수와 열 순서를 사용한다.
- 서로 다른 의미의 값은 한 열에 합치지 않는다. 필요하면 표를 나눈다.

### 5.6 표 선택

표를 우선 사용하는 경우:

- 일정, 비교, 현황, 담당자, 금액처럼 같은 필드가 두 건 이상 반복
- 열이 2~8개이며 각 셀이 짧은 경우
- 날짜순·우선순위순처럼 행 순서가 의미 있는 경우

표를 강제하지 않는 경우:

- 결론과 근거 설명처럼 긴 문장 중심
- 셀 하나가 여러 문단인 경우
- 열이 너무 많아 읽기 어려운 경우

`calendar_grid`로 판정된 답변 산출물은 네이티브 Slack Calendar가 아니라 날짜 기반
Canvas 표현이다. 월간 격자, 날짜순 일정표, timeline 중 어느 표현이 적절한지는 데이터
범위와 사용자 문맥을 마스터가 판단한다. 날짜가 희소하거나 기간이 여러 달이면 일정표나
timeline이 더 적절할 수 있다. `캘린더`라는 낱말 하나만으로 월간 격자를 강제하지 않는다.

초기 구현에서 Mermaid/SVG를 생성하지 않는다. Canvas가 안정적으로 표시하는 제목,
구분선, 표, 목록과 필요한 경우 고정폭 텍스트 흐름도만 사용한다.

### 5.7 하네스의 저장 위치와 버전

현재 `HARNESS_DIR`의 규칙 MD는 콘솔 목록 조회에는 쓰이지만 답변 런타임에 자동 적용되는
경로가 없다. 규칙 파일 하나만 추가하고 “하네싱 완료”로 처리하면 안 된다.

이번 형식 불변조건은 우선 `canvas_harness.py`의 버전된 코드 정책으로 구현한다.
워크스페이스별 선호 단위처럼 운영자가 바꿀 수 있는 값만 별도 규칙으로 허용하되,
다음 불변조건은 하네스 파일로 덮어쓸 수 없다.

- 원문 정밀도보다 구체적인 날짜 생성 금지
- 반올림·절삭 금지
- source ID 없는 값 추가 금지
- 열 의미 변경과 전체/당사 순서 변경 금지
- 기관명·사람 이름 임의 교정 금지

`CANVAS_HARNESS_VERSION` 같은 코드 상수를 결과 추적에 남긴다. 규칙 MD를 런타임에
연결할 경우에는 digest, 적용 순서, 허용 키와 실패 시 기본 정책을 테스트하고 문서화한다.

## 6. 구현 순서

### A. PII 판정 분리

- `src/tybot/pii_screen.py` 추가
- `archive/writer.py`: 직접 식별자 검사 호환 wrapper
- `archive/files.py`: full document + filename 복합 판정
- `attachment_review.py`, `attachment_trace.py`, 콘솔: 새 notice/block code 표시
- 기존 `pii_refused` 큐 비재시도 정책 유지

### B. 산출물 판정

- `intent.py` planner JSON에 `research_question`, `artifact_delivery`,
  `artifact_operation`, `artifact_layout`, `artifact_title`, `target_unit` 추가
- planner 입력에 현재 지원하는 artifact operation 목록을 넣는다
- `master_planner.py`에서 enum·제목·지원 capability 검증 및 fallback
- 규칙 planner는 LLM 장애 시 명시적인 Canvas 요청만 보존한다. `캘린더` 표면형으로
  layout이나 write action을 확정하거나 LLM 판정을 덮어쓰지 않는다

### C. 전문 봇 요청과 결과

- `answer.py`/`specialist_contract.py`: 표시 힌트를 전문 봇 요청에 전달
- `specialist_adapters.py`: Canvas 실행 요청이 아닌 근거 질문 + Markdown 표시 힌트 구성
- Hermes 소스와 `subbots/hermes`는 수정하지 않음
- 실행 거절 감지 시 보정 요청 최대 1회, 이후 명시 실패

### D. Canvas 생성

- `canvas_answer.create(client, body, *, title, provenance)` 형태로 변경
- `canvas_harness.py`: typed cell, 날짜·기간·금액·비율 formatter와 역검증 추가
- formatter 규칙에 버전을 부여하고 QA 기록에는 버전만 남김
- 고정 본문 H1 제거, Disclaimer를 첫 블록으로 추가
- 채널 요청은 현재처럼 채널 read access, DM은 요청자 read access
- 생성 ID 기록과 수집 제외 규칙 추가
- 링크 메시지에도 실제 제목 표시: `전산팀 공지 일정 정리를 Canvas로 작성했습니다`

### E. 추적

QA/호출 기록에 본문 없이 다음 값을 추가한다.

- `delivery_mode=canvas|message`
- `artifact_layout=calendar_grid|timeline|table|report|auto`
- `artifact_operation=answer_document|edit_existing_canvas|create_calendar_events`
- `title_source=planner|fallback`
- `harness_version`
- `harness_result=passed|fallback|failed`
- `target_unit`과 `converted_cell_count` (원문값은 기록하지 않음)
- `format_retry_count`
- `guardrail_result=passed|passed_with_notice|blocked`
- 비민감 오류 코드

## 7. 필수 테스트

### 7.1 가드레일

1. “등기부등본 제출 일정” 한 문장만 있는 표 이미지는 통과한다.
2. `등기부등본` 용어가 있는 일반 공지·보고서가 통과한다.
3. 실제 주민등록번호 형식은 계속 파일 전체 차단한다.
4. 파일명과 여러 고유 필드가 일치하는 실제 등기부등본 fixture는 차단한다.
5. 계약자 명단이라는 용어만 있는 문장과 실제 개인정보 명단 fixture를 구분한다.
6. 부분 OCR + 고위험 복합 신호는 차단한다.
7. notice 파일은 답변 근거로 쓸 수 있지만 차단 파일 원문은 전달되지 않는다.
8. 로그·감사·콘솔 응답에 실제 PII와 OCR 본문이 없다.

### 7.2 Canvas

1. 제목은 질문에 맞는 값이며 `TYBot 정식 답변`이 아니다.
2. Canvas metadata 제목은 `{AI 제목} · TYBot`이고 본문에 제목이 중복되지 않는다.
3. Disclaimer가 본문 첫 블록이다.
4. 채널에서 요청하면 채널 구성원이 읽을 수 있고, DM은 요청자만 읽는다.
5. 동적 제목 Canvas가 파일 목록·Canvas·백필 어느 경로에서도 재수집되지 않는다.
6. 예전 `TYBot 정식 답변` Canvas도 계속 제외한다.
7. 같은 요청도 문맥에 따라 calendar grid, 일정표, timeline 또는 일반 답변으로 갈린다.
8. 300-cell 초과 표는 기존 방식으로 분할된다.
9. 표가 부적절한 긴 설명은 억지로 표 한 칸에 넣지 않는다.
10. `캘린더에 등록`은 답변 Canvas 생성으로 오인하지 않는다.
11. `캘린더 내용을 알려줘`는 Canvas 생성 요청으로 오인하지 않는다.

### 7.3 답변 하네싱

1. `2026.08월말`, `2026년 9월 2주차`는 문법이 통일되지만 일자 정밀도를 만들지 않는다.
2. 명시적인 4자리 일자는 모두 `YYYY-MM-DD`로 표시된다.
3. 세기를 확정할 수 없는 두 자리 연도는 임의로 `20YY`로 바뀌지 않는다.
4. 공사기간 구분자가 모든 행에서 ` ~ `로 통일된다.
5. `억원`과 `백만원`이 섞인 표는 반올림 없는 공통 단위로 표시된다.
6. 정확한 공통 단위가 없으면 원문값을 유지하고 확인 필요를 표시한다.
7. 전체/당사 금액의 위치가 행마다 바뀌지 않는다.
8. 비율 소수 자릿수는 0 추가만 허용하고 반올림하지 않는다.
9. formatter가 새 숫자·날짜·기관명을 만들면 validator가 거절한다.
10. 하네스 실패 시 Hermes 원문 답변과 출처를 보존한 읽을 수 있는 fallback을 보낸다.

### 7.4 오케스트레이션

합성 질문:

```text
우리 팀 공지 채널에 올라온 내용을 Canvas에 캘린더로 작성해줘
```

검증:

- planner 결과는 업무 요약 + 문맥에 맞는 artifact operation/layout이다.
- Hermes에는 Canvas 편집 명령이 제거된 `research_question`이 전달된다.
- 현재 채널 ACL을 통과한 근거만 Hermes에 전달된다.
- Hermes가 일정 사실과 source ID를 작성한다.
- TYBot은 그 결과를 Canvas로 만들고 채널 읽기 권한을 부여한다.
- 최종 표의 날짜·금액·이름은 Hermes 출력에 있던 값과 동일하다.
- 출처 링크가 유지된다.
- 마스터 업무 답변 생성 호출 수는 0이다.
- 기존 Canvas 수정이나 실제 캘린더 이벤트 생성 API는 호출되지 않는다.
- 같은 `캘린더` 단어라도 등록·조회·표현 문맥별 판정이 달라진다.

## 8. 완료 조건과 인계

- 관련 단위·통합 테스트와 실제 이미지의 비식별 fixture가 통과한다.
- `pytest`, `ruff check src tests scripts`, 프론트 타입 검사·빌드가 통과한다.
- 실제 Slack에서 일반 용어 포함 이미지 통과, 실제 PII fixture 차단을 각각 확인한다.
- 채널 Canvas 공유 범위와 동적 제목 Canvas 재수집 금지를 운영에서 확인한다.
- Hermes 소스 변경이 없음을 diff로 확인한다.
- Claude는 구현 커밋, DB/환경 변경 여부, 실행 결과와 미검증 항목을
  `docs/verification/`에 남긴다.
