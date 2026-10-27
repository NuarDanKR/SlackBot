# Archiver Vision DB 원장 구현 현황

Written for: Claude, Codex, 콘솔 담당자. 2026-10-19.

## 이번 범위

`archive/vision_store.py`와 `deploy/sql/archiver_vision_schema.sql`을 추가했다.
기본 OFF인 내부 저장소 코드다. API, 실행 worker, 렌더링, 실제 모델 호출,
coverage/revision 발행 및 색인에는 아직 연결하지 않았다. 배포만으로 PDF/HWP의
기존 실패가 복구되거나 외부로 이미지가 전송되지 않는다.

스키마는 `deploy/apply-schema.sh`에 등록했다. 전용 `tybot_vision` 역할이 이미 있을
때만 해당 테이블 권한을 부여한다. 역할·접속 자격·서비스를 자동 생성하지 않고,
기존 `tybot_archiver`나 Master 역할에 LLM 원장/키 권한을 넓히지 않는다.
정책 변경 이력 테이블에는 전용 역할도 UPDATE/DELETE 권한이 없다.

## 저장 및 동시성

- 전역 정책과 workspace override를 DB에 저장한다. 두 활성 플래그 모두 켜져야
  진행한다. 실효 설정과 각 값의 출처를 읽을 수 있으며 Master 등록은 요구하지 않는다.
- 변경 시 expected revision을 대조하고 새 revision·변경자·변경 이력을 보존한다.
  실효 revision은 전역/해당 workspace revision의 결합이다.
- 승인에는 원본/렌더링/이미지 해시, 페이지 기준/번호, renderer 버전,
  workspace/file/model/provider/정책 revision, 만료와 취소를 바인딩한다.
- 모든 변경은 전역 정책 행을 먼저 잠근다. READ COMMITTED에서 뒤따르는 예산 조회가
  선행 예약의 커밋을 보게 한다. 전역·workspace 일일 예산과 동시 상한,
  페이지 작업별 예약 상한을 같은 트랜잭션에서 확인한다.
- 전송된 적 없는 `reserved`만 취소/승인 철회로 해제한다. 예약 만료를 자동 회수하는
  정리 작업은 아직 없다. 만료/정책 변경으로 막힌 예약도 명시적으로 취소해야 한다.
- 작업 식별자는 workspace/file/원본·렌더링·이미지 해시/페이지/모델/provider로 정한다.
  승인 갱신·정책 revision 변경만으로 같은 이미지를 재전송하지 않는다. 취소한 작업도
  같은 식별자로 자동 재생성하지 않는다. 의도적 재시도/재처리 승인은 아직 미구현이다.
- `begin_dispatch`는 최신 정책/승인/현재 근거 검사를 다시 거쳐 `dispatching`을
  커밋한 뒤 반환한다. 중첩 트랜잭션/미커밋 연결은 거절한다. 모델 I/O 동안 DB 잠금은
  잡지 않는다. 이 커밋 이후의 철회는 이미 허가된 전송을 회수할 수 없다.
- 전송 뒤 프로세스가 죽거나 응답을 잃으면 자동으로 reserved로 돌리지 않는다.
  비용 모름은 `unknown`이고 0이 아니다. 미확정/전송 중 예약액은 날짜가 바뀌어도
  남긴다. 확정 비용은 정산일(KST)에 반영한다. 장기 미확정 건은 사람이 대조해야 한다.
- 현재 원장 작업 단위는 페이지 하나다. `job_usd`도 페이지 작업 상한이며 문서 전체
  batch 상한을 구현한 것은 아니다. 여러 페이지 제출/재시도 정책은 worker 연결 때 추가한다.
- 원장에는 본문·이미지·키·자유 오류 문자열을 넣지 않는다. 작업별 승인 좌표,
  실효 정책 snapshot, 예약액/확정액/상태를 보존한다.

원장 상태는 전송·비용 상태다. 모델의 `extracted`/`uncertain`/`failed`,
`preflight_rejected` 진단 이력, 발행/색인 상태를 모두 구현한 통합 작업 API가 아니다.
콘솔 WC-21 상태 목록과 이 금융 원장의 상태를 그대로 같은 enum으로 매핑하면 안 된다.

## 아직 구현해야 하는 경계

이 저장소는 승인된 내부 호출자만 사용하는 라이브러리다. 공개 API에 그대로
연결해서는 안 된다. 콘솔 인증/인가, workspace/channel 원본 접근 재검증,
원본/렌더링 파일 무결성 검증, PII 판단, provider/region/Vision capability·자격
카탈로그와 서버 요금표 기반 보수적 비용 상한 계산은 다음 서비스/worker 단계다.

`checks`는 그 서비스가 신선하게 계산하는 사실이며 HTTP 요청의 boolean이 아니다.
예약액도 이용자 입력/임의 추정액을 받으면 안 된다. 요금이나 이미지 입력 상한을
확인하지 못하면 예약하지 않는다. 같은 프로세스 CostGuard만으로 대체하지 않는다.

전용 역할을 쓰더라도 테이블 직접 UPDATE가 이 저장소의 잠금 규칙을 우회할 수 있다.
운영 코드/콘솔은 반드시 같은 저장소 경로를 사용해야 한다. 전용 역할 배치와 실제
권한 시험, 취소/정산의 감사 API도 활성화 전에 필요하다.

현재 수집기 release gate의 기존 스키마 검증 기록이 이 원장의 동시성/권한까지
검증했다는 뜻은 아니다. 새 Vision 실행 경로는 아래 격리 DB 시험과 전용 권한 검증을
통과하기 전 활성화하지 않는다. 기존 수집기 게이트를 임의로 열거나 표시를 조작하지 않는다.

## 시험

2026-10-27: Vision 페이지 호출 전에 승인된 provider/model을 대조하고
`Router.restricted_to()`로 승인 모델만 호출하도록 제한했다. 일반 Router의 폴백
설정이 다른 provider로 원본을 전송하는 경로를 차단하는 fake gateway 회귀시험을
추가했다. 응답의 provider/model이 비거나 달라도 처리 성공으로 받지 않는다.
이 변경은 페이지 호출 코어의 사전 방어이며 worker/API 연결이나 실제 외부 호출의
운영 검증을 뜻하지 않는다.

- 이전 코어/정책/gateway 138건 통과는 오너 보고 결과다.
- Codex가 해당 집중 시험과 최초 원장 단위시험을 실행해 155건 통과를 확인했다.
- 이후 SQL 바인딩·실효 설정·취소/예산 회귀를 포함한 집중 시험은 Codex 실행으로
  **218 passed, 10 skipped**다. 이 10건은 실제 PostgreSQL 시험이다.
- DB 원장 증분 당시 전체 회귀: **4935 passed, 82 skipped, 7 warnings** (146.93초).
  기존 Windows 환경 실패가 보고된 `test_external_convert.py`는 제외했다.
  경고 7건은 guard hook 시험의 Windows subprocess 디코딩 경고다.
  Ruff(`src tests scripts`)와 diff 공백 검사도 통과했다.
- `tests/test_vision_store_db.py`는 실제 PostgreSQL의 동시 예약·중복 차단,
  OFF/철회, 비용 미확정 유지, 취소 및 정산을 검사한다. `TYBOT_SCHEMA_TEST_DSN`이
  없으면 명시적으로 skip한다. 기존 isolated DSN 보호를 재사용하고 생성한 전용 schema만
  사용/제거한다. 운영 DB·운영 DSN에는 실행하지 않는다.
- 개발 PC에는 PostgreSQL/컨테이너 실행기가 없어 실제 DB 시험은 미실행이다.
  fake cursor 시험과 SQL 문자열 검사를 실제 DB 검증 결과로 보고하지 않는다.
- 실제 provider 호출, 실제 HWP/PDF 렌더링, 표·숫자 정확성은 미검증이다.

## Claude 병렬 작업

`F0C9LHFTHLM`과 `F0C9VH39NTW`의 `converter_crashed` 원인을 종료 코드/환경으로
진단하고 로컬 PDF 렌더링 가능성을 검증한다. 원본 손상/OOM이라고 먼저 단정하지 않는다.
DB/정책/gateway 파일은 Codex 담당이다. 외부 모델로 원본을 보내지 말고, 원본 유지와
HWP 렌더링 페이지 좌표를 검증한 결과 및 회귀시험을 별도 인계한다.
사진 기능은 B-81에 남기고 이번 전사 경로에 포함하지 않는다.

## HWP 로컬 복구 준비 증분

`archive/hwp_render.py`, `scripts/prepare_hwp_recovery.py`를 추가하고 기존
`file_reconversion.reconvert()`에 명시적 `render_hwp` 선택 인자를 붙였다.
기존 호출과 수집기의 HWP 기본 경로는 바꾸지 않았다. 준비 명령은 파일당 새 staging
generation을 만들 뿐, 기존 metadata/preview/원본/정본을 덮거나 수집 영수증을 조작하지 않는다.
메시지에서 수집된 HWP의 `origin_message_ts`도 그대로 유지한다.

새 변환기 신원은 `hwp:rendered-pdf/1`이다. 설치된 LibreOffice/kordoc 실행 파일과
version 출력의 해시, 실제 font 목록/내용 지문, pypdf 판, PDF 파이프라인 판/설정,
실제 렌더링 PDF 해시와 추출문 해시를 revision 설정에 넣는다. 실행 파일/판 표기가
같더라도 OCR 결과가 다르면 서로 다른 revision이다. 실행 전후 도구/폰트 신원이 바뀌면
결과를 거절한다. 도구 판을 확인하지 못하면 임의의 기본 신원으로 발행하지 않는다.
kordoc 자체가 없으면 PDF 텍스트 추출만 가능하며 OCR 미확인은 partial로 남는다.

렌더 PDF는 새 세대의 `rendered.pdf`로 보존한다. 해당 bytes를 다시 렌더하지 않고
재사용할 수 있고, `rendered_path`/`rendered_sha256`/`renderer_version`을 기록한다.
렌더링 결과가 매번 같은 bytes라고 가정하지 않는다. PDF metadata 등의 차이로 해시가
달라지면 다른 파생 산출물이다. 경로/페이지 기준은 `rendered_pdf`이며 원본 HWP
인쇄 쪽수와 같다고 표시하지 않는다.

`convert_with_coverage('pdf', ...)`를 재사용해 페이지 미확인/상한/실패를 보존한다.
렌더 PDF의 모든 쪽이 추출돼도 HWP 표·배치 보존은 입증되지 않았으므로
`rendered_layout_unverified` 품질 표시와 `render_review_state=pending`을 남긴다.
검수 전에는 변환 상태를 성공으로 올리지 않는다. 이 표시가 Vision 호출의 근거는 아니다.
본문 PII 재검사를 통과한 preview만 저장한다. 차단되면 preview와 렌더 PDF 모두
새 세대에 남기지 않는다. 이전 PII 차단도 해제하지 않는다. PDF OCR 실패 로그는
새 경로에서도 본문이 유출되지 않도록 예외 문자열 대신 허용된 실패 코드만 적는다.

준비 명령은 최대 10개 명시적 file ID만 받는다. 기본은 계획이고 `--apply --by`로만
변환하며 수집기의 단일 실행 락을 획득한다. 기존 수집기를 멈추지 않아 락을 못 얻으면
실패해야 한다. 원본 경로/채널 범위/해시를 대조한다. 원문 본문·stderr는 출력하지 않는다.

이 증분은 **준비 명령**이다. 현재 채널 권한·파일 공유 재조회, 검수한 partial의
정본 발행/색인, 렌더링 좌표의 reader/출처 표시 연결은 다음 단계로 남긴다.
원장이든 준비 명령이든 외부 Vision 호출을 켜지 않는다. 실제 두 HWP의 새 코드 실행,
렌더링 도구/폰트 신원 수집, 표/숫자 정확성은 서버에서 아직 확인하지 않았다.

검증 결과(Codex 실행): HWP/PDF/신원/복구 및 최신 Claude 진단 집중 시험
**118 passed**. 전체 회귀 재실행은 **4999 passed, 82 skipped, 7 warnings**
(299.23초)다. 기존 Windows 실패가 알려진 `test_external_convert.py`는 제외했다.
경고는 기존 guard hook subprocess 디코딩 7건이며 Ruff와 diff 공백 검사도 통과했다.
Claude 동시 수정 중 옛 시험과 새 진단 코드가 섞여 실패했던 중간 실행은 최종 결과로
사용하지 않았다. 이 통과 결과는 실제 서버 변환기나 원본의 내용 정확성을 검증한 것이 아니다.

## 중간 커밋 경계

오너 요청으로 Vision 정책/원장/전사 코어와 HWP 진단/복구 준비를 체크포인트로 묶는다.
기본 OFF, 외부 전송·worker/API·정본 발행/색인 미연결 상태는 그대로다.
실제 DB 잠금/권한, 서버의 실제 변환기·폰트 신원, 두 원본의 표·숫자·미확인 쪽은
검증 대기이며 이 커밋으로 완료 처리하지 않는다.

최종 검수 보완: 진단 `--dump-dir`는 새 0700 디렉터리/0600 파일을 사용한다
(POSIX 기준). 공유 권한의 기존 디렉터리·심볼릭 링크·기존 파일 덮어쓰기를 거절한다.
덤프는 검색용 정본이 아니며 검토 후 운영자가 사내 보존 정책에 따라 제거해야 한다.
stderr 회귀시험의 하위 프로세스 코드를 문법 검사하고 실제 출력 문자 수를 대조해,
문법 오류만 발생한 시험이 본문 유출 방지 시험처럼 통과하지 않게 보강했다.

이전 환경에서 제외했던 `test_external_convert.py` 9건은 현재 가상환경에서 독립
실행으로 통과했다. 이번 체크포인트는 이 파일을 제외하지 않고 커밋 훅을 실행한다.

## 배포 대조기 후속 수정

`1850901` 서버 적용에서 DDL은 완료됐으나 `check_schema_drift.py`가 네 Vision
테이블에도 Master의 SELECT/INSERT를 요구해 배포를 막았다. 이는 역할 분리를
누락한 대조기 오류다. 출력된 `GRANT ... TO tyslackai`를 실행해서 해결하면 안 된다.

네 테이블/스키마 파일의 정확한 조합에만 전용 역할 계약을 적용한다. 기존 표 존재·
추가 컬럼 대조는 그대로 두고, Master/수집기의 실효 권한(상속/PUBLIC/컬럼 권한 포함)은
없어야 한다. `tybot_vision`이 있으면 필요한 권한을 검사하고 감사 이력의 UPDATE/DELETE
등 초과 권한도 거절한다. 역할이 없으면 아직 준비 단계임을 출력하고 기존 봇 배포는
허용한다. 이 검사는 Vision 활성화 승인이나 실제 worker 점검을 대체하지 않는다.

일반 테이블 권한 오류에서도 일괄 CRUD GRANT 명령은 더 이상 생성하지 않고,
해당 스키마의 역할별 권한 선언을 확인하도록 안내한다. 기존 권한을 자동 변경하지 않는다.
고정된 역할 이름뿐 아니라 실제 `current_user`도 검사해 별도 봇 DSN 이름을 쓰는
환경에서 접근 권한을 놓치지 않는다. 배포 검사는 기존처럼 봇 DSN으로 수행해야 한다.
집중 회귀 대상: `test_schema_drift_vision.py`, `test_schema_target.py`, `test_deploy_scripts.py`.
현재 DB 역할/컬럼 단위 권한 보완까지 포함해 **74 passed**.
실제 서버의 권한 조회는 후속 코드 배포 후 재실행해야 한다.
