# B-49 구현 기록 — PII 오탐 개선과 Canvas 산출물

> 작성: 2026-09-15 (Claude)
> 설계: [`docs/design/pii-guardrail-and-canvas-artifacts.md`](../design/pii-guardrail-and-canvas-artifacts.md)
> 상태: Codex 코드 QA 및 결함 수정 완료 · **운영 검증 대기**
> QA: [`2026-09-15-b49-codex-qa.md`](2026-09-15-b49-codex-qa.md)

## 1. 무엇이 바뀌었나

### A. PII — 키워드 차단에서 증거 기반 판정으로

새 모듈 `src/tybot/pii_screen.py`.

| 신호 | 처리 | 코드 |
|---|---|---|
| 주민등록번호 형태 | **즉시 파일 전체 차단** | `resident-registration-number` |
| 고위험 문서 복합 신호(점수 5 이상) | 파일 전체 차단 | `document-class-registry` / `document-class-personal-roster` |
| 위험 용어 언급만 | 통과 + 표시 | `sensitive-term-mentioned` |
| 부분 OCR + 용어만 | 통과 + 표시 | `sensitive-term-partial-coverage` |

점수(코드 상수, fixture 로 고정):

```text
파일명/첫 머리글이 고위험 문서명과 일치   +3
고유 필드 표식 하나                       +1 (합계 최대 4)
같은 표식이 여러 행에서 반복               +2
직접 식별자                               즉시 block
합계 5 이상                               문서 분류 차단
```

`writer.screen()` 은 호환 wrapper 로 남기고 **직접 식별자만** 검사한다. 사람이
"등기부등본 제출 예정" 이라고 **말한 것**은 이제 아카이브에 남는다.

`stage_attachments` 는 한 줄씩 보던 검사를 **파일명 + 추출문 전체**를 보는
`screen_document()` 로 바꿨다. metadata 에 `screen_version`·`screen_result`·
`screen_codes` 를 더했고, **본문·번호·이름은 넣지 않는다.**

> **보안 수준은 낮추지 않았다.** `PII_LEVEL=off` 같은 우회 스위치를 만들지 않았고
> 콘솔에도 완화 토글이 없다. 오탐은 판정 로직과 fixture 로 고친다(설계 §2.5).

### B. 산출물 판정 — 표시 요청과 업무 질문 분리

`Intent` 에 planner 제안 6개(`research_question`, `artifact_delivery`,
`artifact_operation`, `artifact_layout`, `artifact_title`, `target_unit`)를 더했고
**검증은 전부** `master_planner.artifact_for()` 한 곳에서 한다.

- 열거형 밖 → 안전한 기본값(메시지 답변)
- 제목: 한 줄·60자 이하·Markdown/링크 없음·`TYBot 정식 답변` 금지 ·
  **질문에 없던 숫자와 직함은 거부**
- 「캔버스로 답변해줘」 **명시 요청은 LLM 판정보다 세다** — 분류기가 죽어도 살아 있다
- `캘린더` 라는 낱말로 layout 이나 쓰기 동작을 정하지 않는다

planner 입력에 `<지원_산출물>` 블록을 넣었다. **분류 LLM 호출은 늘리지 않았다** —
이미 부르는 분해 호출에 필드를 더했을 뿐이다.

### C. 전문 봇 요청 — 동작이 아니라 형식 힌트

- `MasterTask.question` 이 `research_question` 을 우선한다 → Hermes 에 **Canvas 생성
  지시가 제거된 사실 질문**이 간다
- `SpecialistRequest.display_hint` 는 형식 안내뿐이고, 끝에 "Canvas 생성과 공유는
  호출자가 담당한다" 를 붙인다
- 실행 거절(`is_execution_refusal`)을 감지하면 **한 번만** 보정 요청, 그래도 안 되면
  `specialist-output-unusable` 로 끝낸다. **마스터가 대신 답을 만들지 않는다.**
- `subbots/hermes` 와 Hermes 프롬프트 소스는 **변경 없음**

### D. Canvas — 제목·Disclaimer·재수집 금지·하네스

- `canvas_answer.create(client, body, *, title, provenance)`
- 본문 H1 제거, **Disclaimer 가 첫 블록**
- metadata 제목은 `{검증된 AI 제목} · TYBot`
- 링크 메시지에 실제 제목: `전산팀 공지 일정 정리를 Canvas로 작성했습니다`
- 새 모듈 `src/tybot/canvas_harness.py` — 날짜·기간·금액·비율 formatter 와 **역검증**

**재수집 금지는 세 겹이다**(원칙 1). 동적 제목을 쓰면 예전
`title.startswith("TYBot 정식 답변")` 한 겹으로는 우리 문서를 못 알아본다.

1. 제목 접미사 ` · TYBot` — 사람이 제목을 고치면 사라진다
2. `STATE_DIR/generated-canvases.jsonl` 의 canvas_id — 디스크 유실로 사라진다
3. 본문 첫 블록의 Disclaimer — 사람이 지울 수 있다

**하나만 맞아도 제외한다.** 기록에는 `canvas_id`·workspace·channel_id·
qa_record_id·created_at 만 넣고 제목과 본문은 넣지 않는다.

기록을 못 읽으면 `is_generated()` 는 **False** 다 — 사람이 만든 Canvas 가 조용히
수집에서 빠지는 쪽이 중복 수집보다 나쁘다.

### E. 추적

`QARecord` 에 본문 없이: `delivery_mode`, `artifact_layout`, `artifact_operation`,
`title_source`, `harness_version`, `harness_result`, `target_unit`,
`converted_cell_count`, `format_retry_count`, `guardrail_result`.

## 2. 하네스가 하지 않는 것

**사실을 만들지 않는다.** 이것이 §5 의 전부다.

| 원문 | 표시 | 하지 않는 것 |
|---|---|---|
| `2026.08월말` | `2026-08 말` | `2026-08-31` 로 만들지 않는다 |
| `2026년 9월 2주차` | `2026-09 2주차` | 특정 일자로 만들지 않는다 |
| `26.8.1` (세기 불명) | `26.8.1` + 확인 필요 | `2026` 을 지어내지 않는다 |
| `12억원` + `340백만원` | 같은 열 `억원` 으로 | 반올림하지 않는다 |
| 시작만 있는 기간 | 그대로 | 종료일을 채우지 않는다 |

금액은 `Decimal` 로만 환산하고 **역변환이 원문과 같아야** 통과한다. 다르면 그 표를
통째로 원문으로 되돌린다 — 반쯤 고친 표는 안 고친 표보다 나쁘다.

`calendar_grid` 로 판정돼도 **네이티브 Slack Calendar 가 아니다.** 날짜 기반 Canvas
표현이고, Mermaid·SVG 는 만들지 않는다.

## 3. 판정은 되지만 **실행하지 않는** 것

`master_planner.SUPPORTED_OPERATIONS` 에 `answer_document` 하나뿐이다.

- `edit_existing_canvas` → 지원하지 않는다고 답한다
- `create_calendar_events` → 지원하지 않는다고 답한다

**조용히 다른 것을 해 주지 않는다.** 「캘린더에 등록해줘」 를 답변 Canvas 생성으로
바꿔 치면 사람은 일정이 등록된 줄 안다.

## 4. 테스트

```text
pytest                 2245 passed
ruff check src tests scripts   (아래 '알려진 것' 참조)
```

새 파일:

| 파일 | 덮는 것 |
|---|---|
| `tests/test_pii_screen.py` (13) | §7.1 — 통과해야 할 것과 막혀야 할 것을 **같은 파일에** |
| `tests/test_canvas_harness.py` (18) | §7.3 — 정밀도 보존·반올림 금지·역검증 |
| `tests/test_canvas_artifacts.py` (22) | §7.2, §7.4 — 제목·Disclaimer·재수집·판정 분리 |

기존 테스트 중 **정책이 바뀌어 고친 것** 둘:

- `test_ingest_skips_bot_output_and_pii` — "등기부등본 첨부합니다" 는 이제 아카이브된다
- `test_staged_attachment_rejects_...` — 용어만 있던 fixture 를 직접 식별자로 바꾸고,
  용어만 있는 경우는 새 테스트(`test_sensitive_term_alone_is_collected_with_a_notice`)로 분리

### 되돌리기 실험

고친 곳을 일부러 되돌려 **테스트가 실제로 잡는지** 확인했다.

| 되돌린 것 | 결과 |
|---|---|
| `BLOCK_THRESHOLD` 5 → 99 | 4 failed ✅ |
| `screen_line` 을 항상 `None` 으로 | 2 failed ✅ |
| canvas_id 방어선 제거 | 1 failed ✅ |
| Disclaimer 대신 옛 H1 | 2 failed ✅ |
| 지원 밖 동작을 조용히 허용 | 2 failed ✅ |
| 날짜 정밀도 검증 전체 제거 | 1 failed ✅ |
| 금액 역검증 제거 | 1 failed ✅ |
| 제목을 다시 고정값으로 | 1 failed ✅ |

## 5. 아직 검증되지 않은 것

**운영에서 사람이 확인해야 한다.**

- [ ] 실제 Slack 에서 `등기부등본` 단어가 있는 일정 이미지가 통과하는지
- [ ] 실제 등기부등본 PDF 가 계속 차단되는지 — **비식별 fixture 로만 검증했다**
- [ ] 동적 제목 Canvas 가 파일 목록·Canvas·백필 **세 경로 모두**에서 재수집되지 않는지
- [ ] 이미 만들어진 `TYBot 정식 답변` Canvas 가 계속 제외되는지
- [ ] Hermes 가 실행 거절을 실제로 덜 하는지 (보정 요청 1회가 충분한지)
- [ ] 하네스가 실제 정산서 표에서 `fallback` 을 얼마나 내는지

점수표(`+3/+1/+2`, 임계 5)는 **합성 fixture 로 고정한 값**이다. 실제 변환 결과에서
오탐·미탐이 나오면 점수만 만지지 말고 **표식 목록**을 먼저 본다. 점수를 낮추면
실제 등기부등본이 통과한다.

## 6. DB·환경 변경

**없다.** 스키마 변경도, 새 환경변수도 없다.

`STATE_DIR/generated-canvases.jsonl` 이 새로 생기지만 없어도 동작한다(방어선 3겹 중
하나가 빌 뿐이다). 별도 생성·마이그레이션이 필요 없다.

## 7. 알려진 것 — 내 변경 밖

`ruff check src tests scripts` 에 2건이 남아 있고 **둘 다 다른 에이전트가 작업 중인
`summary_review`** 것이다.

- `src/tybot/slack/pilot.py` 의 import 정렬 — 그쪽이 `summary_review` 를 끼워 넣은 자리
- `tests/test_summary_review.py` 의 SIM115

건드리지 않았다. 내가 만지거나 만든 파일만 검사하면 0건이다.
