# B-45 다음 작업 인계 (Codex → Claude, 2026-09-14)

앞 인계: [`2026-09-14-b45-implementation-handoff.md`](2026-09-14-b45-implementation-handoff.md)
설계: [`operational-warning-recovery-and-answer-progress.md`](../design/operational-warning-recovery-and-answer-progress.md)

이 문서는 **다음에 할 일**만 적는다. 이미 한 것은 위 문서에 있고, 여기서 다시
구현하면 안 된다.

## 0. 지금 어디까지 와 있나

### Codex 완료 작업 (2026-09-14, 커밋 반영됨)

- `--backfill` 서버 오류는 기능 누락이 아니라 **서버 소스가 `9183fe4` 이전인 배포
  불일치**다. Git 체크아웃은 `/var/lib/tybot/src`, 실행 배포본은 `/opt/tybot`이다.
  `/opt/tybot`에서 `git` 명령을 실행하지 않는다.
- 재처리 큐는 이제 변환 미리보기만으로 성공 처리하지 않는다. 기존 첨부 원문에
  멱등 추가하고, 반영 지문을 확인하고, 해당 문서를 검색 색인에 넣은 뒤 닫는다.
- 콘솔 첨부 진단에 관리자 전용 단건 재처리 API와 확인 버튼을 추가했다. PII 정책
  제외 문서에는 버튼을 노출하지 않는다.
- 질문 시점의 채널 Canvas를 실시간 근거 도구에 포함했다. TYBot 답변 Canvas는
  재귀 근거가 되지 않게 계속 제외한다.
- 채널 Canvas 답변은 채널 읽기 권한을 부여하고, DM 답변은 요청자 권한을 부여하는
  기존 경로를 검증했다. Markdown 표는 Slack의 300셀 제한에 맞춰 행 단위 분할한다.
- "캔버스 내용을 읽고 답하느냐" 같은 기능 질의는 LLM 분류 전에 고정 처리하여
  전체 사용법이 아니라 읽기 범위와 제약만 직접 답한다.
- 명시적인 DM 후속 질문만 최근 2시간의 동일 사용자·워크스페이스·DM 문답 좌표를
  연결한다. 새 DM 주제에는 과거 문맥을 섞지 않는다.
- 검증: 관련 테스트 218건, 전체 테스트 1902건, `ruff`, 콘솔 타입 검사·빌드 통과.
  Windows Git 가드 subprocess의 기존 UTF-8 디코딩 경고와 Starlette 경고만 남았다.
- 아직 남음: A의 페이지·시트 coverage/`partial`, D의 채널 단위 최종 실패 알림.

### Claude가 이번에 맡을 범위

아래 순서대로 진행한다. A가 끝나기 전에는 D를 시작하지 않는다. 알림이 참조할
coverage가 부정확하면 사람에게 잘못된 실패 범위를 통지하게 된다.

1. **A: 부분 변환 coverage와 `partial` 상태**
2. **D: 채널 단위 최종 실패/부분 누락 알림과 Hermes 검토 DM coverage**
3. **잔여 안정화: progress 고아 메시지, Node/V8 변환 장애, v1 아카이브 5건 이전**

이번 인계에서 다시 구현하지 않을 것:

- Canvas 실시간 읽기·공개 범위·표 분할
- DM 후속 문맥
- 콘솔 단건 재처리 버튼/API
- 큐 SHA 검증, 원문 반영, 반영 확인, 검색 재색인

병행 작업자가 수정할 수 있는 `specialist_store.py`, `deploy_approval_store.py`와 관련
테스트는 이 작업 때문에 정리하거나 되돌리지 않는다. 미추적
`.claude/settings.local.json`도 수정·커밋하지 않는다.

| 커밋 | 내용 |
|---|---|
| `4dbe625` | Codex 의 B-45 부분 구현 + 재처리 큐(스키마·정책·러너·타이머) |
| `e001619` | 운영 문서가 서버에 없는 `python` 을 부르던 것 |
| `a8de6de` | 러너가 `load_env_file()` 을 안 부르던 것 |
| `9183fe4` | `--backfill`, 회로 차단기를 `claim()` 에 연결 |

**서버에서 확인된 것**(2026-09-14, `tyai`):

- `conversion_queue_schema.sql` · `console_schema.sql` 적용 완료.
  `check_schema_drift.py` → `✅ 선언과 실제가 같다`
- `tybot-convert-retry.timer` enable 됨
- `drain_conversion_queue.py --status` → `임대 회수 0건 / 큐가 비어 있습니다`

**아직 서버에서 안 한 것**: 최신 커밋 배포 후 `--backfill --apply`. 그래서 **큐는 여전히 비어 있고
재처리가 실제로 돈 적은 없다.** 첫 실행 결과를 보고 이 문서의 3-A 를 정해야 한다.

```bash
# Git 소스와 실제 배포 커밋을 따로 확인한다.
git -C /var/lib/tybot/src rev-parse --short HEAD
cat /opt/tybot/.deployed-commit
grep -n -- '--backfill' /opt/tybot/scripts/drain_conversion_queue.py

# 최신 배포본에 --backfill이 보인 뒤 실행한다.
sudo -u tybot /opt/tybot/.venv/bin/python /opt/tybot/scripts/drain_conversion_queue.py --backfill
sudo -u tybot /opt/tybot/.venv/bin/python /opt/tybot/scripts/drain_conversion_queue.py --backfill --apply
sudo -u tybot /opt/tybot/.venv/bin/python /opt/tybot/scripts/drain_conversion_queue.py --apply
```

## 1. 이 저장소에서 지켜야 할 것 (사람이 아니라 코드가 강제한다)

- **서버에서 `python` 은 없다.** Rocky 8 은 `python3` 뿐이고 의존성도 없다.
  운영 문서(`docs/deploy/`·`docs/verification/`)의 명령은 `.venv/bin/python` 이어야 하고,
  `test_operational_docs_call_python_through_the_venv` 가 막는다.
- **새 스키마 파일은 `apply-schema.sh` 목록과 GRANT 가 함께 있어야 한다.**
  `test_every_postgres_schema_file_is_applied` · `test_every_created_table_is_named_in_a_grant`.
  두 번째는 **표 이름까지** 본다 — GRANT 라는 낱말만 있으면 통과하던 예전 검사는
  시퀀스에만 주고 표를 빼먹은 것을 못 잡았다.
- **새 타이머는 `install.sh` 의 `TIMERS` 배열에 넣는다.** 안 넣으면 파일만 깔리고
  아무도 안 켠다. 설치 끝에 꺼진 타이머를 이름으로 알려 주는 장치가 그것을 드러낸다.
- **DB 를 쓰는 스크립트는 `main()` 에서 `load_env_file()` 을 부른다.**
  `test_scripts_that_need_the_database_load_the_env_file`. 안 부르면 systemd 에서는
  돌고 사람 손으로는 안 되는, 가장 헷갈리는 모양이 된다.
- **소스 문자열 검사로 동작을 증명하지 않는다.** `inspect.getsource()` 에 낱말이
  있는지 보는 검사는 실제 고장을 못 잡은 전례가 있다(`execution_mode` 행 매핑).
  가짜 커서·가짜 어댑터로 **실제 경로**를 지나게 한다.
- **되돌림 실험.** 고친 것을 일부러 되돌려 테스트가 잡는지 확인한다.
  이번 작업에서 21건 돌렸고, 그 과정에서 테스트 두 개가 실제로는 아무것도 보장하지
  않는다는 것이 드러났다.

## 2. 우선순위 — 왜 이 순서인가

1. ~~**partial 상태·coverage**~~ **완료(Claude/2026-09-14)**
2. ~~**색인 연계** — 변환은 됐는데 검색에서 못 찾는 상태가 완료로 집계된다.~~
3. ~~**콘솔 재처리 버튼** — 백엔드는 있고 사람이 쓸 자리만 없다.~~
4. ~~**알림**~~ **완료(Claude/2026-09-15, `ced89fe`)**
5. 나머지(progress 고아 메시지, Node/V8, v1 이전)는 앞 인계 문서의 3·4·8 그대로.

## 3. 다음 작업

### A. partial 상태와 페이지·시트 coverage (설계 §5·§6)

**구현 완료 (Claude / 2026-09-14).** `convert.Coverage` + `collect_coverage()`
컨텍스트로 모은다. `convert()` 의 반환형은 **그대로 뒀다** — 호출부가 여럿이라
바꾸면 하나만 놓쳐도 조용히 깨진다. 아래 남은 것은 §A-잔여 참조.

**문제.** 지금 `conversion_state` 는 `succeeded | failed | blocked | unsupported |
pending` 뿐이다. 10페이지 PDF 에서 3페이지만 읽혀도 `succeeded` 다. 그 답변에
우리 출처가 붙고, 사람은 전부 본 줄 안다.

설계가 요구하는 것:

```text
conversion: pending | running | succeeded | partial | failed | unsupported | blocked
추가 필드: pages_total, pages_converted, sheets_total, sheets_converted,
          missing_units, quality_flags
```

**알 수 없는 개수는 `null` 이다. 0 이나 100% 로 만들지 않는다** — 모르는 것을
안다고 적으면 그 표는 더 이상 근거가 아니다.

이미 있는 재료:

- `archive/convert.py:284` — PDF 페이지 루프(`for i, page in enumerate(reader.pages, 1)`).
  페이지별 성공/실패를 이미 알 수 있는 자리다.
- `archive/convert.py:106 _sheet_rows()` — 시트별 행 수를 이미 센다.
- `archive/external_convert.py:166` — XLSX 외부 변환이 `--max-rows 1000` 이다.
  **1000행에서 잘린 것을 지금은 아무 데도 안 적는다.** 전체 행 수와 처리 행 수를
  남기면 곧바로 `partial` 판정 근거가 된다.

주의할 것:

- `convert()` 의 반환형은 `list[str]` 이고 호출부가 여럿이다. 반환형을 바꾸면
  넓게 깨진다 — **별도 채널로 coverage 를 돌려주는 편**이 안전하다(예: 예외가 아닌
  결과 객체를 선택적으로 받는 함수, 또는 모듈 수준 컨텍스트).
  어느 쪽이든 기존 호출부가 그대로 돌아야 한다.
- **산출물 0줄·제목만 있음은 성공이 아니다**(설계 §5). 지금 `drain_conversion_queue.reconvert()`
  는 `empty_output` 으로 막는데, 제목 아래 빈 단락은 아직 구별하지 않는다.
- `attachment_trace.py` 의 단계 판정도 함께 봐야 한다. 여기만 고치면 추적 도구가
  `partial` 을 모르는 상태로 남는다.

완료 판정: 3페이지만 읽힌 PDF 가 `partial` 로 남고, 콘솔과 답변의 「근거」 줄에
확인 못 한 범위가 보인다. 표·그림만 있는 슬라이드가 성공으로 집계되지 않는다.

#### A 구현 체크리스트

1. `convert()`의 기존 `list[str]` 계약은 유지한다. 새 `ConversionResult` 또는
   선택적 `convert_with_coverage()`를 추가하고 기존 호출은 얇은 호환 래퍼로 둔다.
2. coverage 필드는 숫자를 아는 형식에서만 채운다. 추출기가 전체 개수를 제공하지
   않으면 `null`로 둔다. `missing_units`에는 페이지/시트 번호 같은 비민감 좌표만
   넣고 본문·파일 내용은 넣지 않는다.
3. PDF는 전체 페이지 수, 텍스트/OCR에 성공한 페이지 수, 실패 페이지를 기록한다.
   한 페이지라도 실패하면 본문이 있어도 `partial`이다.
4. XLSX는 전체 시트 수와 처리한 시트 수를 기록한다. 외부 변환기의 1000행 제한에
   걸린 시트는 `quality_flags`에 `row_limit_reached`를 남기고 `partial`로 판정한다.
5. PPT/PPTX는 전체 슬라이드 수를 알 수 있을 때만 기록한다. 제목만 추출되고 본문,
   표, 그림 설명이 하나도 없는 슬라이드는 성공으로 세지 않는다.
6. HWP/HWPX·OCR처럼 외부 도구가 상세 coverage를 주지 않으면 숫자는 `null`이고,
   도구가 부분 실패를 명시한 경우에만 `partial`로 둔다.
7. `archive/files.py`, `convert_staged_attachments.py`,
   `drain_conversion_queue.py`가 같은 결과 판정 함수를 사용하게 한다. 실시간 수집과
   재처리의 상태가 달라지면 안 된다.
8. `attachment_review.py`, `attachment_trace.py`, 콘솔 진단 API/UI에 coverage를
   전달한다. 답변 근거 안내에는 `확인 3/10페이지 · 미확인 4~10페이지`처럼 범위만
   표시하고 원문을 복제하지 않는다.
9. 기존 metadata에는 필드가 없으므로 `unknown`/`null`로 읽는다. 마이그레이션을
   이유로 기존 파일을 다시 쓰지 않는다.

필수 합성 테스트:

- PDF 10페이지 중 3페이지 성공 → `partial`, `3/10`, 누락 7개
- XLSX 다중 시트 중 한 시트 실패 → `partial`
- XLSX 1000행 절단 → `row_limit_reached`, 성공 금지
- 빈 결과·제목만 결과 → `failed` 또는 `partial`, `succeeded` 금지
- 전체 개수 미상 → `null`; 0/0이나 100% 표시 금지
- 구형 metadata를 읽어도 콘솔과 답변이 깨지지 않음

### B. 색인 연계 (설계 §5 마지막 줄)

**구현 완료(작업 트리, 배포 전).** 큐 워커가 원문 반영·지문 확인·해당 문서 재색인을
모두 통과한 뒤에만 `succeeded`로 닫는다. 색인 실패는 `index_failed`로 재시도한다.

**문제.** 변환에 성공해도 `search_index` 에 들어가기 전까지는 검색에서 못 찾는다.
지금은 그 상태를 완료로 본다.

설계가 요구하는 것: `index: pending | running | succeeded | failed | not_applicable`
와 `index_generation`. 재처리 완료를 **색인까지** 확인하고 닫는다.

이미 있는 재료:

- `search_index.reindex(docs, root)` · `indexed_counts(doc_paths)` · `indexed_at()`
- `conversion_job` 에 `finished_at` 이 있고 상태는 `succeeded` 로 닫힌다.
  **색인 확인 전 단계를 하나 더 두는 것**과 `succeeded` 뒤 별도 열로 두는 것 중
  하나를 고른다. 큐 상태를 늘리면 스키마 CHECK 와
  `test_the_states_the_code_knows_are_the_states_the_database_allows` 가 함께 움직인다.

주의: 색인은 `DATABASE_URL` 이 없으면 `None` 을 돌려주는 선택 기능이다.
**색인이 없는 설치에서 재처리가 영영 안 닫히면 안 된다** — 그때는 `not_applicable`.

### C. 콘솔 재처리 버튼 (설계 §6)

**구현 완료(작업 트리, 배포 전).** 관리자 단건 재처리 API와 확인 UI를 추가했다.
좌표만 감사하며 파일명·업무 본문은 감사 메타데이터에 넣지 않는다.

백엔드는 있다. `conversion_queue.enqueue(..., force=True)` 가 그것이고
`scripts/drain_conversion_queue.py --backfill` 이 같은 일을 CLI 로 한다.

없는 것:

- `POST /api/diagnostics/archive/reprocess` — 관리자 전용, CSRF, 감사 기록.
  기존 첨부 미리보기(`_attachment_preview_type`)와 같은 권한·감사 경로를 재사용한다.
- 일괄 재처리는 **선택 대상과 건수를 확인한 뒤** 실행한다(설계 §6).
- `console-web` 의 진단 화면 버튼. 표시 필드는 이미 내려간다
  (`retryState`·`retryAttempts`·`nextRetryAt`·`errorCode`·`retryable`).

**`pii_refused` 는 버튼에서 제외한다.** `enqueue` 가 `FORCE_BLOCKED` 로 막지만,
화면에 버튼이 보이면 사람은 눌러 보고 「안 된다」 만 겪는다. 아예 안 보이는 편이 낫다.

### D. 알림 — 채널 단위 묶음 (설계 §6)

**구현 완료 (Claude / 2026-09-15, `ced89fe`).** `src/tybot/conversion_alerts.py` ·
`deploy/sql/conversion_alert_schema.sql` · `scripts/send_conversion_alerts.py` ·
`tybot-convert-alert.{service,timer}` · `tests/test_conversion_alerts.py`(25건).
아래는 원래 지시이며, 지킨 내용은 부록 참조.

- 파일별 중복 DM 금지. **채널 단위로 묶는다.**
- 담을 것: 파일명, Slack 원본 링크, 오류 분류, 다음 조치. 그뿐이다.
- 보낼 조건: **최종 실패 또는 부분 누락이 검토를 방해할 때.**
  일시적 첫 실패는 알림이 아니라 운영 기록이다 — 큐가 알아서 다시 한다.
- 받는 사람의 채널 접근 권한을 확인한다.
- Hermes 검토 DM 에도 그 요약이 쓴 파일의 **현재 변환 범위**를 넣는다.

**주의: DM 발송 코드는 다른 에이전트가 손댄 이력이 있다**(`daily_review.py`,
`review_digest_schema.sql`). 착수 전에 `git log -- src/tybot/daily_review.py` 로
최근 변경을 확인한다.

#### D 구현 체크리스트

1. 알림 이벤트에는 `workspace/channel_id/file_id/error_code/coverage/permalink`만
   저장한다. 파일 본문, 질문, 요약, 토큰은 저장하지 않는다.
2. `queued`와 첫 일시 실패는 알리지 않는다. `failed`, `held`, 검토를 막는
   `partial`만 대상으로 한다.
3. 같은 채널의 대상 파일을 한 건의 DM으로 묶고, 동일 상태·동일 pipeline version은
   다시 보내지 않는 멱등 키를 둔다.
4. 수신자는 DB의 채널 관리자와 검토자다. Slack 표시명으로 추측하지 말고 저장된
   사용자 ID를 사용하며, 발송 직전에 채널 접근 권한을 다시 확인한다.
5. 메시지는 파일명, 원본 Slack 링크, 비민감 오류 분류, 다음 조치, coverage만 담는다.
   원본 링크가 없으면 링크를 만들어 내지 않는다.
6. Hermes 검토 DM은 요약에 사용한 첨부의 coverage를 붙인다. 누락이 검토 판단을
   방해하면 승인 버튼보다 경고를 먼저 보이고, 읽지 못한 범위를 명시한다.
7. 알림 실패가 변환 큐 상태를 되돌리면 안 된다. 별도 전달 상태로 재시도한다.

필수 합성 테스트:

- 같은 채널의 최종 실패 3건 → DM 1건
- 두 채널 실패 → 권한 있는 담당자에게 채널별 1건
- 첫 retryable 실패 → DM 0건
- 동일 이벤트 재실행 → 중복 DM 0건
- 채널 접근 권한이 사라진 사용자 → DM 0건
- 감사/DB/로그에 파일 본문·질문·시크릿 없음
- Hermes 검토 DM에 `partial` 범위가 표시되고 완전 변환으로 오인시키지 않음

## 4. 알고 있는 위험

| 위험 | 설명 |
|---|---|
| 큐가 한 번도 안 돌았다 | 서버에서 `--backfill --apply` 전이라 실제 동작 미확인. **첫 실행 로그를 보고 이 문서를 갱신한다** |
| `http` 실행 모드 | `serve()` 가 `http` 를 `prompt` 와 같은 길로 보낸다. 지금 `http` 로 등록하면 프롬프트형으로 돈다 |
| 도구 예산 기본값 | `MAX_TOOL_CALLS=12` 등은 실측이 아니다. 긴 종합에서 일찍 끊길 수 있다. `tool_calls=`/`budget=` 로그로 조정 |
| Linux 셸 테스트 | `test_specialist_runtime_*` 33건은 개발 PC 의 `bash` 가 WSL 런처라 실패한다. **서버에서 별도 확인 필요** |
| 실제 PostgreSQL 통합검증 | 큐 SQL 은 문법 대조와 코드/스키마 parity 만 거쳤다. `FOR UPDATE SKIP LOCKED` 동시성은 실측 안 함 |

## 5. 검증 명령

```powershell
.venv\Scripts\python.exe -m pytest -q --ignore=tests/test_specialist_runtime_unit.py --ignore=tests/test_specialist_runtime_store.py
.venv\Scripts\python.exe -m ruff check src tests scripts
cd console-web; npm.cmd run build
```

현재 기준선: **1902 passed**, ruff 0건(전문 봇 Linux 셸 테스트 2개 파일 제외).

서버 쪽:

```bash
cd /opt/tybot
sudo -u tybot .venv/bin/python scripts/check_schema_drift.py
sudo -u tybot .venv/bin/python scripts/drain_conversion_queue.py --status
journalctl -u tybot-convert-retry --since -1h
```

## 6. 인계 규칙

- 이 문서와 앞 인계 문서를 **먼저 읽고** 완료 항목을 다시 구현하지 않는다.
- 끝낸 항목은 여기서 지우지 말고 **취소선과 커밋 해시**로 남긴다. 무엇을 이미
  시도했는지가 다음 사람에게 가장 필요한 정보다.
- 서버에서만 확인할 수 있는 것은 **미검증으로 명시**한다. 통과로 간주하지 않는다.
- 업무 원문·질문 본문은 저장소에 커밋하지 않는다. 합성 fixture 와 비민감 집계만.


## 부록. A 구현 결과 (Claude / 2026-09-14)

```
src/tybot/archive/convert.py      Coverage · collect_coverage() · convert_with_coverage()
                                  PDF 쪽 단위, XLSX 시트·행 상한, 줄 접기 플래그
src/tybot/archive/files.py        수집 시 metadata 에 coverage 기록, partial 판정
scripts/drain_conversion_queue.py 재변환도 같은 판정
src/tybot/attachment_review.py    `부분 변환` 이름표 + `확인 3/10쪽` 한 줄
tests/test_conversion_coverage.py 16건
```

### 지킨 것

- **모르는 개수는 `null`.** 0 이나 100% 로 만들지 않는다. 외부 도구가 상세를 안
  주면 `unknown` 이고, `partial` 도 `succeeded` 도 단정하지 않는다.
- **플래그만으로도 `partial`** — 행 상한에 걸린 시트는 숫자로는 전부 읽은 것처럼
  보인다(`row_limit_reached`·`line_limit_reached`·`ocr_unavailable`·`title_only`).
- **글자 없는 쪽을 읽은 것으로 세지 않는다.** 그림만 있는 쪽인지 추출 실패인지
  모르기 때문이다.
- `convert()` 반환형 불변. 호출부는 `collect_coverage()` 로 감싸기만 한다 —
  `convert_with_coverage()` 로 바꿨더니 그 이름을 갈아 끼우던 테스트가 조용히
  무력해졌다(`test_image_is_ocr_converted_...`). 이음매를 지키는 쪽으로 되돌렸다.

### 되돌림 실험 8건 — 전부 잡힌다

**처음엔 4건이 안 잡혔다.** 전부 「그 경로를 테스트가 안 지난다」 였다.

- PDF 예외 분기(손상된 쪽)를 빈 쪽 검사가 대신하고 있었다 → `pypdf.PdfReader` 를
  갈아 끼워 실제로 터지는 쪽을 만들었다
- `title_only` 는 `_has_body()` 를 직접 부르는 검사뿐이라 **배선을 증명하지 못했다**
- 구형 metadata 검사가 `Attachment` 를 손으로 만들어 `_as_count()` 를 안 지났다
- 수집 경로가 coverage 를 쓴다는 것을 아무도 확인하지 않았다

### 상한 제거 (오너 지시 2026-09-14)

**변환 단계에서는 자르지 않는다.** 여기서 자르면 아카이브에 영구히 없어진다 —
답변 단계의 상한(`AnswerEngine._max_hits`·`MAX_EVIDENCE_CHARS`)과 다르다. 그쪽은
요청마다 다시 고르므로 손실이 아니다.

| 있던 상한 | 지금 | 비상용 환경변수 |
|---|---|---|
| `convert.MAX_LINES` 20,000 | 무제한 | `TYBOT_CONVERT_MAX_LINES` |
| `convert.MAX_TOTAL_CHARS` 300,000 | 무제한 | `TYBOT_CONVERT_MAX_CHARS` |
| `convert.MAX_CELL` 200 | 무제한 | `TYBOT_CONVERT_MAX_CELL` |
| `convert.FOLD_HEAD/TAIL` 12,000/4,000 | 무제한 | `TYBOT_CONVERT_FOLD_HEAD/TAIL` |
| `files.MAX_TEXT_BYTES` 256KB | 무제한 | `TYBOT_TEXT_MAX_BYTES` |
| `files.MAX_TEXT_LINES` 20,000 | 무제한 | `TYBOT_TEXT_MAX_LINES` |
| `canvas.MAX_LINES` 300 | 무제한 | `TYBOT_CANVAS_MAX_LINES` |
| 외부 XLSX `--max-rows` 1,000 | 10,000,000 | `TYBOT_XLSX_MAX_ROWS` |

**`0` 이 무제한이다.** 그래서 `[:LIMIT]` 로 자르던 자리를 전부 찾아 고쳤다 —
`0` 이면 `[:0]` 이라 **본문이 통째로 사라진다.** 네 군데 있었고 그게 이 변경의
가장 조용한 실패였다.

마지막 줄이 특히 중요하다. 벤더 스크립트의 `--max-rows` 는 **자르는 값이 아니라
버리는 값**이었다 — 행 수가 넘으면 그 시트를 통째로 건너뛴다(`build_blocks`).
우리 정산서·기성 표는 대부분 1,000행을 넘고, 그래서 **시트가 통째로 사라진 채
「변환 성공」** 이 됐다.

### A-잔여 (아직 안 한 것)

- ~~PPT/PPTX 슬라이드 수~~ **기본 경로는 완료** — `_pptx_basic` 이 슬라이드
  단위로 센다. 글자 없는 슬라이드는 읽은 것으로 세지 않는다.
  **외부 변환(LibreOffice→PDF) 경로는 여전히 쪽 단위**다 — 슬라이드 번호와 쪽
  번호가 같다고 보장할 수 없다(**남은 것**).
- ~~HWP/HWPX~~ **폴백 표시는 완료** — 정밀 변환기를 못 써서 기본 파서로 내려가면
  `fallback_converter` 플래그가 붙고 `partial` 이 된다. 다만 kordoc 이 성공한
  경우의 **상세 개수는 여전히 `unknown`**(**남은 것**).
- ~~외부 XLSX 변환기 1,000행~~ **완료** — 사실상 무제한. 위 「상한 제거」 참조.
- ~~답변 「근거」 줄에 coverage 표시~~ **완료** — `일부만 읽은 첨부: 정산서.pdf
  (확인 3/10쪽)`. 못 읽은 것(`withheld`)과 **줄을 나눠** 적는다. 섞으면 사람이
  할 일이 달라진다 — 하나는 원본을 열어 보는 것이고 하나는 재변환이다.
- **남은 것**: 외부 PPT 경로의 슬라이드 단위, kordoc 성공 시 상세 개수,
  검색 색인이 커진 뒤의 성능(상한을 없앴으므로 아카이브가 커진다 — 실측 필요).


## 부록. D 구현 결과 + A 마무리 (Claude / 2026-09-15)

### D. 채널 단위 알림 (`ced89fe`)

- **검토를 막을 때만 알린다.** `failed`·`held`·`partial` 만. 큐에 재시도가 남았으면
  안 알린다 — 곧 성공할 일로 부르면 사람은 그 DM 을 안 읽게 되고 정작 중요한 한
  건도 묻힌다. `pii_refused` 는 정책이라 고칠 것이 없어 안 알린다
- **`partial` 을 넣은 이유는 답이 나가기 때문**이다. 범위를 모르면 전부 본 줄 안다
- **멱등 키 = 파일 목록 + 각 파일의 상태.** 이름이 바뀌어도 같은 건이고, 상태가
  바뀌면(실패 → 부분 성공) 다시 알릴 값이 있어 새 키가 된다
- **권한은 발송 직전에 다시 본다.** 등록 뒤 채널에서 빠진 사람에게 파일명을
  보내면 그 자체가 유출이다. 확인 실패는 통과가 아니라 **차단**이다
- 발송 기록 표에 파일명·본문 없음. 좌표·개수·상태 지문뿐
- 큐를 못 읽어도 알림은 계속된다(`held` 판별만 생략)

운영 전:

```bash
sudo cat /opt/tybot/deploy/sql/conversion_alert_schema.sql   | sudo -u postgres psql -p 55432 -d tyslackai -f -
cd /opt/tybot && sudo -u tybot .venv/bin/python scripts/send_conversion_alerts.py
sudo systemctl enable --now tybot-convert-alert.timer
```

### A. 외부 PPT coverage

슬라이드 수를 pptx zip 항목에서 정확히 센다. **`converted` 는 채우지 않는다** —
그려진 것과 읽은 것은 다르다. 렌더에서 쪽 수가 줄면 `render_lost_units`.

### 되돌림 실험 14건 — 전부 잡힌다

**한 건이 처음에 안 잡혔다.** `pii_refused` 가드는 그냥 차단 파일로는 어차피
실패 상태가 아니라 그 분기를 지나지 않았다. **부분 변환 뒤 차단**으로 바꾸니 잡혔다.
같은 함정을 이번 인계에서 세 번째 만났다 — 검사가 실제 분기를 안 지나면 그 검사는
통과해도 아무것도 보장하지 않는다.

### 남은 것

- **B-46(긴급)**: 채널 파일 목록·Canvas 첨부·링크 수집
- ~~**B-47(신규)**: 의도 분류를 LLM 으로~~ **구현 완료(Codex/2026-09-15)** —
  기존 planner 호출에 `reference_mode`·`asks_about_our_sources`를 추가하고, 정상 LLM
  경로의 정규식 가로채기·후속 질문 덮어쓰기를 제거했다. 기간·권한·capability 검증은
  코드에 남겼고 LLM 장애 때 후속 범위를 규칙으로 추측하지 않는다.
- **B-45 알림 보완(Codex/2026-09-15)**: 알림 실행기가 `channel-owners.json`의
  전체 TYBot 담당자를 수신자 계산에 전달한다. 기존 구현은 검토자만 전달해 채널
  관리자 알림이 누락될 수 있었다.
- kordoc 성공 시 상세 개수(외부 도구가 안 준다)
- **상한을 없앴으므로 아카이브·색인이 커진다 — 성능 실측 필요**
- 서버에서 `--backfill --apply` 미실행. **큐는 여전히 비어 있다**

### Codex 검증 (2026-09-15)

- 전체 Python 테스트(Linux 전용 전문 봇 셸 테스트 제외): **1997 passed**
- `ruff check src tests scripts`: 통과
- 콘솔 프로덕션 빌드: 통과
- 서버 운영 검증은 수행하지 않았다. 최신 배포 뒤 §0의 `/opt/tybot/... --backfill
  --apply`와 알림 타이머 실제 전송을 별도로 확인해야 한다.
