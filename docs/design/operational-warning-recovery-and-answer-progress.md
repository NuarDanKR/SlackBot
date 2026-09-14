# 운영 경고 복구·첨부 변환 신뢰성·답변 진행 안내

작성: Codex / 2026-09-14. 구현 담당: Claude. 상태: 설계, 운영 변경 미실행.

## 1. 목표와 근거

오너가 제공한 2026-09-14 경고와 주간보고 답변을 기준으로 한다.
완료 기준은 경고 삭제가 아니라 기록 저장 복구, 원본 보존, 누락 범위의 정확한 표시,
장애 후 재처리, 그리고 긴 답변 요청의 진행 상태 전달이다.

| 현상 | 확인된 사실 | 아직 확인할 것 |
|---|---|---|
| specialist_call.decision_id 없음 | 운영 INSERT가 실패했다. 저장소 SQL에는 컬럼 추가가 있다 | 실제 접속 DB·포트·스키마·배포 SHA, 마이그레이션 실행 여부 |
| v1 문서 5개 경고 | v1도 현재 답변 근거다. 삭제하면 손실 가능 | 5개 문서의 v2 원문 대응·권한·참조 좌표 |
| Hermes timeout | 호출 계층이 시간 초과로 종료했다 | 10:10 실행본의 제한값, provider 지연, 도구 횟수, 대기시간 |
| kordoc Node/V8 fatal | 실행파일 발견 이후 메모리 권한 설정 중 비정상 종료 | systemd 제한 충돌, 메모리·가상주소 한도, Node/kordoc 버전 |
| 일부 표·그림 미확인 | 답변에 변환 안내와 본문 누락이 표시됨 | 현재 변환 메타데이터인지, 과거 대화의 실패 설명인지, 실제 미지원 영역인지 |

화면 문장만으로 문서 전체 실패를 확정하지 않는다. 파일별 현재 상태와 실제 산출물을
대조해야 한다. 시간 제한을 늘렸다는 사실만으로 속도가 개선됐다고 보고하지 않는다.

## 2. 현재 코드와 수정 지점

- `deploy/sql/console_schema.sql`: specialist_call 컬럼·제약·인덱스.
- `deploy/apply-schema.sh`: postgres가 SQL 파일을 직접 읽는 경로 및 마지막 drift 검사 `|| true`.
- `scripts/check_schema_drift.py`: 현재 CREATE TABLE/ADD COLUMN 선언과 실제 DB 대조.
- `src/tybot/console/specialist_store.py`, `specialist_router.py`: 호출 감사 기록 저장.
- `src/tybot/archive/store.py`, `scripts/diagnose_collection.py`: v1 읽기와 v2 대응 진단.
- `src/tybot/archive/files.py`: 다운로드·보존·변환을 같은 try에서 처리한다.
- `src/tybot/archive/external_convert.py`: 변환 프로세스, 120초 제한, stderr 300자 절단.
- `src/tybot/attachment_review.py`, `attachment_trace.py`, `attachment_view.py`:
  기존 첨부 상태·콘솔 원본 열람 경로를 확장한다. 별도 중복 관리 화면은 만들지 않는다.
- `scripts/convert_staged_attachments.py`: 기존 재처리 CLI를 큐와 같은 실행 함수로 통합한다.
- `deploy/tybot.service`: `MemoryDenyWriteExecute=true`가 있다.
- `src/tybot/slack/pilot.py`: `_handle`, `_handle_request`, `finish`와 QA response_ts 기록.
- `specialist_contract.py`, `specialist_router.py`, `specialist_adapters.py`,
  `specialist_tools.py`: 실행 제한·provider·도구 루프 측정 및 중단 전파.

기존 thread-follow-up-evidence, document-pipeline-trace-and-report-summary,
console-answer-trace, hermes-integration-fidelity 설계를 확장한다.
구현 착수 시 최신 변경과 충돌 여부부터 확인하고 이 문서의 수치를 실제 코드와 대조한다.

## 3. P0: DB 스키마 누락과 배포 성공 판정

### 즉시 운영 복구 절차

1. tybot이 사용하는 연결 대상으로 DB명·포트·current_schema·current_user만 출력한다.
   DATABASE_URL·비밀번호는 출력하지 않는다. 알려진 운영 포트는 55432다.
2. public을 가정하지 말고 해당 연결에서 specialist_call이 해석되는 스키마를 확인한다.
3. 관리자가 최신 SQL을 적용한다. /opt 경로를 postgres가 못 읽는 경우 root가 읽어 stdin으로 전달한다.

```bash
sudo cat /opt/tybot/deploy/sql/console_schema.sql | sudo -u postgres psql -p 55432 -d tyslackai -v ON_ERROR_STOP=1
sudo -u tybot env TYBOT_ENV_FILE=/etc/tybot/tybot.env /opt/tybot/.venv/bin/python /opt/tybot/scripts/check_schema_drift.py
```

4. 실제 봇 계정으로 decision_id, task_index, task_kind, required_capability,
   qa_record_id를 읽고 INSERT 권한·sequence 사용 권한을 검사한다.
   쓰기 검증은 테스트 행을 트랜잭션 rollback하며 sequence 번호 공백은 허용한다.
5. 현재 요청의 QA ID와 호출 행의 decision/task 연결을 확인한다.
   이미 누락된 호출 기록을 추정해 성공 행으로 복원하지 않는다.

### 영구 수정

- 실제 콘솔 배포 러너와 update 진입점을 추적한다. 테스트 성공만으로 배포 성공 처리하지 않는다.
- 배포 순서: 코드 검증 -> 호환 가능한 additive 스키마 적용 -> 앱 계정 계약 검사 ->
  새 프로세스 시작 -> 건강 검사 -> 성공 표시. 이 단계들의 exit code를 전달한다.
- apply-schema의 최종 검사 `|| true`를 제거하고 연결 환경을 명시한다.
  SQL 읽기는 root stdin 전달로 변경하되 chmod 777 같은 권한 완화는 하지 않는다.
- DB 목적지가 봇 설정과 다르면 중단한다. 운영 기본값을 추측해 다른 DB에 적용하지 않는다.
- 새 필수 컬럼뿐 아니라 타입·NOT NULL·result CHECK·필요 인덱스·실제 역할 권한도 확인한다.
  마이그레이션은 2회 실행 가능해야 한다. 서비스 런타임에는 DDL 권한을 주지 않는다.
- 마이그레이션 실패 시 새 앱을 기동하지 않는다. 이미 적용된 additive 컬럼을 롤백 삭제하지 않는다.
- 런타임 감사 저장 장애는 답변 장애와 구분하고 콘솔 운영 상태를 degraded로 표시한다.
  동일 장애 로그는 첫 발생 즉시, 이후 5분 단위 집계한다. 실패 횟수는 생략하지 않는다.
- 원한다는 이유만으로 질문 본문을 새 복구 로그에 복제하지 않는다. 기존 QA 기록은 유지한다.

## 4. P1: Node/V8 변환 장애의 재현과 격리

`MemoryDenyWriteExecute`와 JIT 메모리 권한의 충돌은 유력 가설이다.
errno 문구만 보고 OOM 또는 정책 충돌로 단정하지 않는다.

### 먼저 수집할 증거

- 실제 unit와 drop-in의 MemoryDenyWriteExecute, MemoryMax, LimitAS, TasksMax,
  NoNewPrivileges, SystemCallFilter, WorkingDirectory, User, PATH.
- node/kordoc/LibreOffice 버전·실행 경로와 변환기 종료 코드·signal·elapsed_ms.
- 같은 시각 journal의 OOM, 커널 종료, SELinux AVC 및 cgroup 메모리 이벤트.
- 운영 원문을 외부로 보내지 않고 합성 이미지/HWPX로 동일 실행 조건에서 재현한다.
- `sudo -u tybot ... --version` 성공은 서비스 제한하의 실제 변환 성공을 증명하지 않는다.
- 비교는 운영 봇 unit를 바꾸기 전에 별도 테스트 unit에서 수행한다.
  제한값 하나씩 차이를 두고 동일 fixture의 변환 산출물까지 비교한다.

### 실행 구조

- 변환을 TYBot 수집 프로세스에서 별도 로컬 worker로 분리한다. 새 네트워크 포트는 열지 않는다.
- `tybot-convert.service`를 제안한다. 프로세스 분리만으로 권한 격리가 된다고 간주하지 않는다.
  별도 변환 계정, 작업별 입력 읽기·출력 쓰기만 허용한다. 봇 토큰·LLM 키·DB 자격증명은 전달하지 않는다.
- 마스터가 DB 작업을 claim하고 고정 로컬 spool에 제출, worker는 변환만 실행,
  마스터가 산출물 검증·PII 검사·수집·색인을 수행한다. 사용자 지정 명령은 받지 않는다.
- JIT 제한 충돌이 재현될 경우 변환 worker에만 필요한 예외를 적용한다.
  봇·콘솔 전체의 MemoryDenyWriteExecute를 해제하지 않는다.
- worker의 CPU·메모리·동시 실행 제한은 운영 측정으로 정하고 초기 동시 실행은 1이다.
  입력 용량 자체의 일괄 거절 제한을 다시 도입하지 않는다. 큰 파일은 작업 큐와 분할로 처리한다.
- OCR 모델은 배포 시 준비한다. 실행 중 임의 패키지 설치나 외부 모델 다운로드는 금지한다.
- timeout 시 부모뿐 아니라 하위 Node/LibreOffice 프로세스 그룹을 종료·회수한다.
  작업별 임시 폴더만 정리하고 원본은 보존한다. 경로 검증과 symlink/압축 해제 제한을 둔다.
- 우선 배포가 큰 경우 별도 실행 helper부터 만들고 큐 연결을 다음 커밋으로 나눈다.
  단, 근본 원인 재현 없이 Node 실행 옵션을 전역으로 덧붙이지 않는다.

## 5. P1: 변환 실패·부분 성공의 상태 모델

### 상태를 단계별로 보존

기존 attachment metadata와 attachment_trace에 호환 필드를 추가한다.
기존 status는 reader 호환 기간 동안 유지하되 새 코드가 업무 상태를 추론하는 유일한 값으로 쓰지 않는다.

```text
download: pending | running | succeeded | failed
original: missing | retained
conversion: pending | running | succeeded | partial | failed | unsupported | blocked
index: pending | running | succeeded | failed | not_applicable
```

추가 필드: schema_version, workspace, channel_id, file_id, original_sha256,
converter/version, pipeline_version, attempt_id/count, error_code, retryable,
started_at/finished_at/next_retry_at, last_success_attempt_id,
pages/sheets_total, pages/sheets_converted, missing_units, quality_flags,
diagnostic_id, index_generation. 알 수 없는 개수는 null이며 0이나 100%로 만들지 않는다.

`original=retained, conversion=failed`는 '원본 저장됨 · 변환 실패'로 표시한다.
현재처럼 '격리 저장 실패' 하나로 뭉치지 않는다. 한 파일 장애가 다른 파일 수집을 중단하지 않는다.
산출물 0줄·헤더만 있음·제목만 있음은 성공이 아니다. 제목 아래 빈 단락은 원본도 비었는지
확인 전까지 '본문 미확인'으로 남긴다. 텍스트가 있다는 것만으로 표·그림까지 이해했다고 표시하지 않는다.

### 오류 코드와 재처리

| 코드 | 자동 처리 |
|---|---|
| download_transient / provider_unavailable | 제한된 재시도, HTTP Retry-After 우선 |
| converter_timeout / resource_exhausted | 지연 재시도, 분할·저동시성 경로 검토 |
| converter_policy_denied / converter_missing | 환경 복구까지 보류. 파일마다 반복 실행하지 않음 |
| converter_crashed | 1회 격리 재시도 후 동일 fingerprint면 보류·관리자 알림 |
| encrypted / corrupt / unsupported | 자동 반복 금지, 원본 링크와 필요한 조치 표시 |
| empty_output / partial_output | 다른 지원 변환 경로 1회 또는 부분 결과로 분류 |
| pii_refused | 정책 제외. 실패 재시도 대상에서 제외하고 자동 우회 금지 |

- 기본 transient 재시도: 1분, 5분, 30분 + jitter, 최초 포함 최대 4회.
- 키는 `(workspace, channel_id, file_id, original_sha256, pipeline_version)`.
  DB 작업 큐 claim/lease로 중복 실행을 막고 재시작 후 lease 만료 작업을 회수한다.
  원문/토큰은 큐 payload에 넣지 않는다. worker에는 job ID와 고정 경로만 전달한다.
- 같은 변환 환경 오류가 반복되면 converter/version별 circuit breaker로 보류한다.
  버전·설정 복구 후 합성 smoke test를 통과한 경우에만 재개한다.
- 재처리 성공 전까지 이전 유효 산출물을 덮어쓰지 않는다. 새 산출물과 상태는 검증 후 원자적으로 교체한다.
- 새 변환 결과 수집은 기존 writer의 중복 방지 경로를 사용한다. 과거 `## 원문` 수정·삭제 금지.
  과거 실패 표시는 현재 메타데이터와 구분한다. 오래된 실패 문장을 현재 장애로 재해석하지 않는다.
- index 완료까지 추적한다. 변환 성공만 하고 검색에서 못 찾는 상태를 완료로 처리하지 않는다.
- `/첨부 승인`은 일반 변환의 선행조건이 아니다. 정책 제외를 기술 실패처럼 자동 해제하지 않는다.

## 6. P1: 문서 유형별 손실 대응과 콘솔

- PDF/이미지: 페이지별 텍스트 추출, 텍스트 부족 페이지 OCR, 페이지 번호·원본 연결.
- HWP/HWPX: 지원되는 정밀 변환 -> 지원되는 기본 파서 fallback.
  표·병합셀·내장 이미지 처리 여부를 별도 품질 플래그로 기록한다.
- PPT/PPTX: 슬라이드 텍스트와 렌더링/OCR 범위를 대조해 그림만 있는 슬라이드도 추적한다.
- XLSX: 표 헤더·병합셀·빈 값·수식 캐시·숨김/제외 시트를 구분한다.
  현재 외부 변환의 max-rows 1000도 누락 원인이 될 수 있으므로 전체/처리 행 수와 분할 계획을 남긴다.
- fallback이 성공해도 정밀 변환과 동등하다고 표시하지 않는다. 원본 링크와 미확인 범위를 함께 표시한다.
- 원본 이미지의 전문 봇 전달은 기존 권한·민감정보 검사를 통과하는 경로만 사용한다.
  OCR 실패를 이유로 미검사 이미지를 외부 LLM에 자동 전송하는 우회는 만들지 않는다.

기존 첨부 진단 화면에 상태/오류코드/문서유형/워크스페이스/재시도 여부 필터,
원본 보존 여부, 마지막 성공, 다음 재시도, 페이지·시트 누락을 추가한다.
원본 미리보기는 기존 관리자 권한·감사·path validation을 재사용하고 HTML은 escape한다.
관리자 재처리 버튼은 동일 큐에 작업을 등록한다. 일괄 재처리는 선택 대상·건수 확인 후 실행한다.
공개 원본 URL이나 임의 파일 경로 API를 만들지 않는다.

채널 담당자와 검토자 알림은 파일별 중복 DM을 피하고 채널 단위 묶음으로 보낸다.
파일명·Slack 원본 링크·오류 분류·다음 조치만 제공하고 채널 접근 권한을 확인한다.
최종 실패 또는 부분 누락이 검토를 방해할 때 알린다. 일시적 첫 실패는 운영 기록으로 남긴다.
Hermes 검토 DM에도 해당 요약의 파일별 현재 변환 범위를 포함한다.

## 7. P2: v1 아카이브 정리

1. diagnose_collection의 읽기 전용 결과로 v1 5개 파일의 원문 수·v2 대응·미대응·파손 목록을 만든다.
2. workspace/channel ID를 확정할 수 없는 파일은 자동 이전하지 않는다.
3. 이전 CLI는 dry-run 기본, 명시적 apply, 파일별 체크포인트와 재실행 중복 방지를 제공한다.
4. v1 원문을 보존하며 v2에 복제하고 원문 지문·작성자·시각·첨부 좌표·권한을 검증한다.
5. QA의 과거 evidence_refs가 v1 경로를 가리킬 수 있다. 매핑/호환 조회와 후속 질문 회귀를 먼저 검증한다.
6. 검색 중복 제거와 재색인을 확인한다. v1 삭제는 이번 자동화 범위가 아니다.

경고는 프로세스/아카이브 루트별 최초 1회와 개수 변경 시 출력하고,
콘솔 아카이브 진단에는 해결 전까지 계속 표시한다. 경고만 숨기는 수정은 완료가 아니다.

## 8. P1: 스레드 대기 안내

마스터가 정해진 진행 안내를 보내는 것은 업무 답변을 대신 생성하는 것이 아니므로 역할 정책과 일치한다.
추가 LLM 호출 없이 결정적 문구로 구현한다.

### UX

- 요청 3초 후 미완료면 1회 게시: `요청을 확인하고 있습니다. 자료 확인과 답변 작성에 시간이 걸리고 있습니다.`
- 실제 전문 봇 실행이 시작됐으면 `전문 봇이 자료를 확인하고 있습니다.`로 표시 가능하다.
  시작 전부터 Hermes가 읽고 있다고 주장하지 않는다.
- 30초 이후 미완료면 같은 메시지를 1회 갱신한다. 새 댓글을 계속 만들지 않는다.
- 통계가 없을 때 '곧', '30초 안에' 같은 시간 약속을 하지 않는다.
  성공·timeout·실패를 함께 집계하고 최근 동종 성공 30건 이상이면 p50-p90 범위를 '최근 처리 기준'으로 표시한다.
- 최종 답변은 진행 메시지를 chat.update로 교체한다. 실패도 같은 메시지에 종료 상태를 표시한다.
- thread root는 `event.thread_ts or event.ts`. 채널 본문 답변 설정이 있어도 진행 안내를 게시한
  요청은 같은 스레드에서 마무리한다. 이 예외를 문서화하고 질문 본문에 중복 답변을 남기지 않는다.
- Canvas는 최종 메시지에 Canvas 링크를 넣고 기존 권한 부여·실패 fallback 동작을 유지한다.

### 동시성·감사

- request progress 객체: request_id, channel_id, root_ts, progress_ts, stage,
  started_monotonic, terminal, lock, cancel_event, timer handle.
- 완료와 3초 timer가 동시에 실행돼도 terminal 검사와 잠금으로 게시가 최대 1회여야 한다.
  느린 post 진행 중 완료가 오면 post 결과 ts를 얻은 후 즉시 최종 상태를 갱신한다.
- 정상 finish뿐 아니라 `_handle` 예외 처리에서도 timer를 취소하고 종료한다.
- update 실패 시 최종 답변 신규 게시를 1회 시도하며 실제 성공 response_ts를 QA에 남긴다.
  진행 안내 실패는 업무 답변을 중단하지 않는다. Slack 429에는 Retry-After와 요청 deadline을 적용한다.
- 진행 문구를 QA 답변 본문·근거·수집 대상으로 저장하지 않는다. 최종 답변은 정확히 1건 기록한다.
- 프로세스 종료 시 고아 메시지 처리를 위해 본문 없는 progress 좌표를 지속 저장하고,
  재시작 시 처리 중단 상태로 정리한다. 자동 업무 질문 재실행은 중복 비용 때문에 하지 않는다.
- Slack event 재전달 dedupe를 유지한다. 다른 사용자의 동시 요청은 독립 객체로 관리한다.

## 9. 지연 측정과 timeout 후 작업

- 요청 전체, planner, registry, 원문 복원, 검색/읽기, provider 각 호출,
  최종 생성, Slack 전송 시간을 decision/task/request에 연결한다.
- 전문 봇 모델·버전·도구 횟수·입력량·출력량·캐시 사용량(제공되는 경우)·대기시간을 기록한다.
  질문·근거 본문은 새 metric에 저장하지 않는다.
- 현재 외부 90초/도구 45초/변환 120초는 서로 다른 작업의 제한이다.
  부하에 따른 무조건 상향 대신 한 요청 deadline 안에서 후보와 재시도 예산을 분배한다.
- ThreadPool future.cancel은 이미 실행 중인 provider 호출을 중단하지 못한다.
  cancellation token/deadline을 도구 루프와 provider timeout까지 전파하고,
  timeout 후 다음 검색·다음 모델 호출을 시작하지 못하게 한다.
- 이미 진행 중인 네트워크 호출을 중단할 수 없다면 동시 worker 상한과 대기 큐로 누적을 제한하고
  늦은 응답은 게시하지 않는다. 늦게 확정된 비용은 동일 attempt ID로 별도 정산한다.
- 먼저 재서식 1회 호출, 검색 중복, 불필요한 문서 재변환, 직렬 registry 조회부터 측정한다.
  검증 없이 모델을 낮추거나 전체 질문을 캐시로 응답하지 않는다.
- 비교 말뭉치: 단일 사실 질문, 주간보고 5문서 요약, 같은 스레드 bullet 변경, 변환 부분 실패 질문.
  같은 권한·자료·질문으로 변경 전후 성공률, 수치 유지, p50/p90, timeout 비율, 비용을 비교한다.

## 10. Claude 구현 순서와 승인 기준

독립 PR/커밋 단위로 수행하고 BACKLOG에서 상태를 갱신한다.

1. 스키마 복구 및 배포 fail-fast. 기존 운영 로그 누락과 새 호출 정상 저장을 확인.
2. 단계별 지연 측정과 스레드 진행 안내. 가짜 시간·Slack client로 race와 실패 검증.
3. Node/V8 합성 재현과 변환 worker. 실제 Rocky8 서비스 조건으로 smoke test.
4. 첨부 단계별 상태·재시도·부분 성공·색인 연계와 기존 콘솔 확장.
5. v1 이전 진단·호환 참조와 경고 집계.

필수 테스트:

- 구형 DB -> 마이그레이션 2회 -> 앱 계정 INSERT/SELECT; 잘못된 DB/권한/컬럼 누락이면 배포 중단.
- 원본 저장 후 crash에도 원본 보존, corrupt/PII는 자동 반복 없음, 성공 0줄은 실패.
- 동시 claim, lease 만료, 재시도 상한, 원본 SHA 변경, 부분 성공 후 개선 변환, 색인 실패 복구.
- 표 머리글·병합셀·숫자·페이지/시트 범위 fixture, OCR 없는/실패한 페이지의 partial 판정.
- 실제 Node/kordoc/LibreOffice subprocess의 제한 실행 smoke test. --version만으로 대체 금지.
- 3초 전 완료 시 안내 0건, 지연 시 1건, race/예외/429/update 실패/재시작/Canvas/DM 동작.
- 안내·봇 출력의 수집 제외 및 QA response_ts 정확성.
- timeout 후 추가 도구 호출 0, 늦은 결과 미게시, 동시 실행 상한과 비용 기록.
- v1->v2 원문 지문 보존, 권한 동일, 과거 스레드 좌표 복원, 검색 중복 없음.
- pytest, Ruff, 콘솔 타입 검사·빌드, 데스크톱 화면 검증. 모바일은 이번 범위 제외.

최종 인계에는 변경 SHA, 마이그레이션 결과, 적용 unit 설정, converter 버전,
실제 재처리 파일의 전/후 상태와 실패 잔여 목록, 답변 지연 비교를 첨부한다.
코드 테스트 통과와 운영 원인 확인을 구분해서 보고한다.
