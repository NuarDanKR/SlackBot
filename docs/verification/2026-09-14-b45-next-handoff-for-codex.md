# B-45 다음 작업 인계 (Claude → Codex, 2026-09-14)

앞 인계: [`2026-09-14-b45-implementation-handoff.md`](2026-09-14-b45-implementation-handoff.md)
설계: [`operational-warning-recovery-and-answer-progress.md`](../design/operational-warning-recovery-and-answer-progress.md)

이 문서는 **다음에 할 일**만 적는다. 이미 한 것은 위 문서에 있고, 여기서 다시
구현하면 안 된다.

## 0. 지금 어디까지 와 있나

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

**아직 서버에서 안 한 것**: `--backfill --apply`. 그래서 **큐는 여전히 비어 있고
재처리가 실제로 돈 적은 없다.** 첫 실행 결과를 보고 이 문서의 3-A 를 정해야 한다.

```bash
cd /opt/tybot
sudo -u tybot .venv/bin/python scripts/drain_conversion_queue.py --backfill          # 판정만
sudo -u tybot .venv/bin/python scripts/drain_conversion_queue.py --backfill --apply   # 넣기
sudo -u tybot .venv/bin/python scripts/drain_conversion_queue.py --apply              # 한 번 돌리기
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

1. **partial 상태·coverage** — 지금 "성공" 이 거짓일 수 있다. 가장 값이 크다.
2. **색인 연계** — 변환은 됐는데 검색에서 못 찾는 상태가 완료로 집계된다.
3. **콘솔 재처리 버튼** — 백엔드는 있고 사람이 쓸 자리만 없다.
4. **알림** — 위 셋이 정확해진 뒤라야 알릴 내용이 맞다.
5. 나머지(progress 고아 메시지, Node/V8, v1 이전)는 앞 인계 문서의 3·4·8 그대로.

## 3. 다음 작업

### A. partial 상태와 페이지·시트 coverage (설계 §5·§6)

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

### B. 색인 연계 (설계 §5 마지막 줄)

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

- 파일별 중복 DM 금지. **채널 단위로 묶는다.**
- 담을 것: 파일명, Slack 원본 링크, 오류 분류, 다음 조치. 그뿐이다.
- 보낼 조건: **최종 실패 또는 부분 누락이 검토를 방해할 때.**
  일시적 첫 실패는 알림이 아니라 운영 기록이다 — 큐가 알아서 다시 한다.
- 받는 사람의 채널 접근 권한을 확인한다.
- Hermes 검토 DM 에도 그 요약이 쓴 파일의 **현재 변환 범위**를 넣는다.

**주의: DM 발송 코드는 다른 에이전트가 손댄 이력이 있다**(`daily_review.py`,
`review_digest_schema.sql`). 착수 전에 `git log -- src/tybot/daily_review.py` 로
최근 변경을 확인한다.

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

현재 기준선: **1883 passed**, ruff 0건.

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
