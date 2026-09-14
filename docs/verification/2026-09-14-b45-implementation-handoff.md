# B-45 구현 인계 (2026-09-14, Codex)

상태: 부분 구현, 미커밋·미배포. 전체 B-45 완료 아님.
설계: `docs/design/operational-warning-recovery-and-answer-progress.md`.

## 최신 체크포인트 (후속 작업)

- `conversion_worker.py`: UUID spool 요청/응답, 만료 검사, 확장자 허용 목록,
  오류 코드만 반환, 입력 정리, worker 결과 1시간 후 정리, 단일 worker flock.
  `convert_local`로 워커 재귀 호출 차단. `TYBOT_CONVERT_SPOOL` 지정 시에만 사용.
- `deploy/tybot-convert.service`: 별도 계정/전용 `/opt/tybot-convert` 런타임,
  운영 시크릿/아카이브/소스 접근 차단, TCP/IP 금지. 자동 설치/활성화하지 않음.
  준비·한계: [워커 운영 준비](../deploy/conversion-worker.md).
- 워커는 **DB queue가 아님**. Python 내장 변환의 작업별 강제 종료, crash 이후 입력
  자동 회수, 대형 파일 분할, 부분 성공 coverage, 디스크 quota 연계는 미구현.
- 최초 수집/재변환 CLI의 예외 cause 분석을 `failure_details`로 통합.
  CLI도 converter_timeout/retryable 기록. 재시도 가능 표시는 자동 예약을 뜻하지 않음.
- 재변환에서 줄 제한 뒤의 PII까지 검사하도록 수정. 제한 이후 금지 행이 있어도 파일 전체 차단.
- 콘솔 아카이브 진단에 originalState/conversionState/errorCode/retryable 추가.
  화면에 원본 보존 여부·오류 코드·재시도 가능 여부 표시. 기존 워크스페이스 필터 유지.
- CLI 테스트의 실제 `.env` 로드를 차단하여 선택 실행 순서에 따른 설정 오염 방지.

최신 검증:
- 전체 실행(콘솔 후속 수정 전): **1895 passed, 30 failed**. 실패는
  `test_specialist_runtime_store.py` / `test_specialist_runtime_unit.py`의 Linux bash
  실행 테스트이며 이 PC의 WSL 배포판 없는 bash 실행/인코딩 오류. 통과로 간주하지 않음.
- 후속 수정 후 변환 CLI·worker·콘솔 API: **129 passed**.
- Ruff, `git diff --check`, 프론트엔드 `npm.cmd run build`(tsc + Vite) 통과.
- 운영 DB/Slack/실제 kordoc/systemd/데스크톱 시각 검증은 미실행.

다음 담당자는 아래 잔여 목록 중 **DB retry queue/lease/backoff**부터 이어간다.
운영 전에는 별도 Linux 환경에서 실패한 배포 헬퍼 테스트와 schema migration 통합검증이 필요하다.

## 구현된 범위

- deploy/update.sh: 설치 후 스키마 적용·앱 계정 검사, 실패 시 restart와 deployed marker 기록 전에 중단.
- deploy/apply-schema.sh: postgres 직접 파일 열기 대신 root stdin 전달,
  drift 검사 실패 전파, TYBOT_ENV_FILE 명시, 적용 전 앱 DB명/포트/로컬 호스트 확인.
- scripts/check_schema_drift.py: --target-only --expected-db --expected-port 읽기 전용 사전검사.
- slack/progress.py: 요청별 3초 timer, 1회 스레드 안내, 최종 chat.update,
  update 실패 시 같은 스레드 신규 게시, terminal/lock으로 늦은 게시 방지.
- pilot.py: 정상 완료와 공통 예외 처리 모두 progress.send 경유, finally 취소.
- archive/files.py: 원본 보존 여부와 conversion_state를 기존 metadata에 호환 필드로 추가.
  변환 실패 로그에서 원본 보존 여부 명시. 기존 status 독자는 유지됨.
- archive/store.py: v1 경고를 프로세스 내 루트·개수 기준으로 집계. 원문 삭제나 이전은 하지 않음.
- 테스트: test_request_progress.py, test_legacy_warning.py 신설,
  test_attachment_trace.py에 변환 실패 후 원본 보존 검증 추가.

## 아직 구현되지 않은 것 (우선순위 순)

### 추가 완료 (같은 날 후속 작업)

- 오너 추가 요청: 명시 요청만 Canvas였던 정책을 변경. 본문 포함 응답 1200자/20줄 이상이면
  자동 Canvas, 메시지로 요청하면 자동 전환 안 함. 긴 답변은 스레드 Canvas 링크로 전달.
  Markdown 표는 Canvas 전까지 유지하고 메시지 fallback만 제목/강조/표를 mrkdwn으로 변환.
  기존 채널·DM Canvas 권한 경로 유지. 실제 Slack 생성은 배포 후 검증 필요.
- 진행 안내 30초 갱신 추가. 같은 메시지만 갱신하고 완료 뒤 갱신하지 않음.
- Canvas/메시지/진행 안내 관련 33 passed, Ruff 통과.

- external_convert._run을 Popen/communicate로 변경. Linux 새 세션에서 실행하고
  timeout 시 해당 프로세스 그룹 SIGKILL 후 회수. Windows는 직접 자식 kill만 지원.
- ExternalConversionError에 code/retryable 추가. converter_timeout은 재시도 가능,
  converter_crashed와 converter_missing은 환경 확인 전 자동 재시도 불가.
- V8 fatal 문구만으로 OOM/정책 충돌을 단정하지 않음. 정상 실행 경로에서 native stderr를
  사용자 경고에 그대로 싣지 않음. 완전한 별도 진단 로그 저장은 아직 없음.
- 최초 수집 metadata에 error_code/retryable을 예외 cause에서 전달.
- 재변환 CLI에서 pii_refused 제외, original_state/conversion_state 갱신.
  재변환 CLI 상세 cause 통합은 최신 체크포인트에서 완료.
- 변환 관련 84 passed, 최신 관련 77 passed, Ruff 통과.

### 잔여 작업

1. schema 실제 PostgreSQL 통합검증. current_schema·서버 identity까지 비교하고
   앱 역할 INSERT/sequence 권한·타입/CHECK/인덱스 검사 보강.
   현재 사전검사는 host/db/port만 확인하므로 custom socket의 다른 클러스터까지 구분하지 못한다.
2. 실제 콘솔 root 배포 러너가 update.sh를 사용하는 경로를 재확인하고 Linux 셸 smoke test.
   자동 스키마 적용은 운영에서 실행하지 않았다. 이미 누락된 로그도 복원하지 않았다.
3. progress 재시작 후 고아 진행 메시지 정리, 429의 명시적 deadline 처리.
   현재 네트워크 post/update 동안 lock을 보유하므로 Slack SDK timeout만큼 완료가 지연될 수 있음.
   현재 성공 게시 뒤 프로세스 종료 시 안내가 남을 수 있다. 지속 저장은 미구현.
4. Node/V8 systemd 제한 재현, 별도 converter 계정/worker/spool 운영 검증.
   worker 코드/unit은 추가했지만 활성화하지 않았다. 마스터 unit은 변경하지 않음.
   워커 전용 unit만 MemoryDenyWriteExecute=false. 원인 확정 안 됨.
5. ~~DB retry queue/lease/backoff/circuit breaker~~ **완료(Claude/2026-09-14)** — 아래 참조.
   남은 것: 오류코드 확대, partial 상태·페이지/시트 coverage, index generation과 재처리 완료 연결.
6. 콘솔 첨부 진단에 상태 필드는 추가. 파일별 관리자/검토자 알림 및 Hermes 검토 DM coverage는 잔여.
7. 요청 단계별 지연, provider deadline/cancellation, 늦은 응답 비용 정산.
8. v1 dry-run 이전과 과거 evidence_refs 호환. 현재는 경고 집계만 구현.

## 검증 및 이어서 실행할 것

관련 테스트 92 passed, DB 대상 검증 테스트 4 passed. 최종 Ruff 통과.
운영 Slack·DB·Node 변환은 미실행. 실제 서비스는 기동하지 않았다.

```powershell
.venv\Scripts\python.exe -m pytest tests/test_request_progress.py tests/test_handle_compound.py tests/test_attachment_trace.py tests/test_archive_v2.py tests/test_files.py tests/test_legacy_warning.py -q
.venv\Scripts\ruff.exe check src tests scripts
git diff --check
```

현재 .claude/settings.local.json은 다른 작업의 미추적 파일이므로 포함하지 않는다.
코드·설계·테스트 새 파일도 git status로 확인하고 필요한 것만 커밋한다.
토큰 또는 작업 환경 전환 시 이 문서를 먼저 읽고 완료 항목을 재구현하지 않는다.


## 재처리 큐 (Claude / 2026-09-14)

잔여 5번의 앞부분 — **DB 작업 큐 · claim/lease · backoff · circuit breaker** 를 구현했다.

```
deploy/sql/conversion_queue_schema.sql   conversion_job · conversion_breaker + GRANT
src/tybot/conversion_queue.py            정책(순수 함수) + claim/lease/fail/succeed
scripts/drain_conversion_queue.py        큐를 비우는 러너(--status/--apply)
deploy/tybot-convert-retry.{service,timer}  5분 주기. install.sh·콘솔 헬퍼에 등록
src/tybot/archive/files.py               실패 시 enqueue (수집을 막지 않는다)
src/tybot/console/app.py                 첨부 진단에 retryState/retryAttempts/nextRetryAt
tests/test_conversion_queue.py           29건
```

### 설계에서 지킨 것

- **키**: `(workspace, channel_id, file_id, original_sha256, pipeline_version)`.
  내용이 바뀌면 다른 작업이고, 파이프라인을 고치면 과거 실패가 다시 들어온다.
- **payload 에 원문·토큰 없음.** 좌표와 코드만. 워커는 좌표로 고정 경로를 연다.
  큐에서 읽은 값도 경로에 붙이기 전에 `_safe_component()` 를 지난다.
- **backoff** 1분·5분·30분 + 지터, 최초 포함 4회.
- **`held` 와 `failed` 를 가른다.** 환경 문제(`converter_missing`·
  `converter_policy_denied`)는 파일마다 되풀이하지 않는다.
- **`pii_refused` 는 자동 해제하지 않는다.** 재변환 경로에서도 막는다.
- **이전 유효 산출물을 미리 지우지 않는다.** 새 결과가 검증을 통과한 뒤 원자적 교체.
- **임대 만료 회수.** 잡은 프로세스가 죽어도 작업이 멈춘 채 남지 않는다.
  스키마 CHECK 가 「만료 없는 임대」 를 아예 못 만들게 한다.

### 되돌림 실험 13건 — 전부 테스트가 잡는다

정책 제외 재시도 / 환경 문제 뭉개기 / 재시도 상한 제거 / 지터 제거 / 회로가 스스로
회복 못 함 / 큐 장애가 수집을 멈춤 / 좌표 없이 큐에 올림 / 이전 산출물 선삭제 /
PII 자동 해제 / 빈 산출물을 성공으로 / 경로 탈출 / 스키마 적용 목록 누락 / GRANT 누락.

마지막 항목에서 **기존 가드가 부족한 것**을 찾았다. `test_schemas_that_create_tables_also_grant_them`
은 파일에 `GRANT` 라는 낱말이 있는지만 봐서, 시퀀스에만 주고 표를 빼먹어도 통과했다.
표 이름을 대조하는 `test_every_created_table_is_named_in_a_grant` 를 더했다.

### 운영 전 할 일

서버에는 `python` 이 PATH 에 없다(Rocky 8 은 `python3` 뿐이고, 그것도 시스템
파이썬이라 이 프로젝트 의존성이 없다). **venv 를 경로로 부른다.**

```bash
sudo cat /opt/tybot/deploy/sql/conversion_queue_schema.sql   | sudo -u postgres psql -p 55432 -d tyslackai -f -
cd /opt/tybot && sudo -u tybot .venv/bin/python scripts/check_schema_drift.py
sudo systemctl enable --now tybot-convert-retry.timer
cd /opt/tybot && sudo -u tybot .venv/bin/python scripts/drain_conversion_queue.py --status
```

`console_schema.sql` 도 함께 적용해야 한다 — 오케스트레이션 추적 컬럼
(`specialist_call.decision_id`·`required_capability`·`task_kind`·`task_index`)이
거기 선언돼 있다. `check_schema_drift.py` 가 빠진 것을 이름으로 알려 준다.

타이머를 켜지 않으면 **작업만 쌓이고 아무것도 돌지 않는다.** `install.sh` 가 꺼진
타이머를 이름과 함께 알리므로 설치 로그에서 보인다.

### 이미 쌓인 실패를 큐에 넣기 (backfill)

`enqueue` 는 **앞으로 들어올** 실패에만 걸린다. 이미 staging 에 있는 실패는 큐에
없어서, 타이머는 도는데 재처리되는 것이 하나도 없는 상태가 된다(2026-09-14 서버에서
`큐가 비어 있습니다` 로 확인).

```bash
cd /opt/tybot
# 1) 무엇이 들어갈지 먼저 본다
sudo -u tybot .venv/bin/python scripts/drain_conversion_queue.py --backfill
# 2) 넣는다
sudo -u tybot .venv/bin/python scripts/drain_conversion_queue.py --backfill --apply
# 3) 바로 한 번 돌린다(타이머를 기다려도 된다)
sudo -u tybot .venv/bin/python scripts/drain_conversion_queue.py --apply
```

`force` 로 넣는다 — 그때의 「되풀이해도 소용없다」 는 판정은 **그때의 변환기**를
기준으로 한 것이고, 변환기를 고친 뒤에는 결과가 달라질 수 있다. `pii_refused` 만은
넣지 않는다(정책 제외는 변환기와 무관하다).

### 이 구현이 하지 않는 것

- **부분 성공(partial) 과 페이지·시트 coverage** — 상태는 succeeded/failed 둘뿐이다.
- **index generation 연계** — 변환 성공만 하고 검색에서 못 찾는 상태를 아직 구별하지 않는다.
- **관리자 재처리 버튼** — 콘솔은 상태를 **보여 주기만** 한다. 큐에 넣는 API 는 없다.
- ~~회로 차단기가 claim 을 막지 않는다~~ **연결함** — `claim()` 이 열린 회로의
  변환기 작업을 집지 않고, 집었으면 임대와 시도 횟수를 되돌린다. 회로 표를 못
  읽어도 재처리는 계속된다.
- **실제 PostgreSQL 통합검증** — SQL 은 문법 대조와 코드/스키마 parity 테스트만 거쳤다.
