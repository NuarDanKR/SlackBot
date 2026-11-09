# B-12 색인 운영 연결 — 실행 잡 · 실행 이력 · 타이머 · 콘솔

작성 2026-11-09 (Claude). **커밋하지 않았다.** Codex 소유 파일(`embedding_index_*` ·
`embedding_gateway` · `embedding_search` · `search_embedding_schema.sql` 등)은 **수정 0**.

## 왜 필요했나

2026-11-09 운영 DB 실측:

| 표 | 행 |
|---|---|
| `search_embedding_model_revision` | 0 |
| `search_embedding_workspace_policy` | 0 |
| `search_embedding_query_policy` | 0 |
| `archive_search_document_revision` | 0 |
| `archive_search_chunk` (vector 컬럼) | 0 |

판정·발행·재대조 경계는 다 있었고 `run_embedding_index` 가 그것을 한 번에 묶고
있었다. 그런데 **그 함수를 부르는 곳이 없었고**, 레지스트리·정책 표를 채울 수단도
psql 뿐이었다. 앞선 작업은 CLAUDE.md 「운영 기능은 콘솔에 둔다」 때문에 CLI 를
일부러 만들지 않았다 — 그래서 남은 일은 **콘솔과 실행 잡을 같이** 만드는 것이었다.

## 무엇을 만들었나

| 파일 | |
|---|---|
| `src/tybot/embedding_index_cli.py` | 실행 진입점. 기본은 계획(모델 호출 0 · DB 쓰기 0), `--execute` 로만 실행 |
| `deploy/sql/search_embedding_run_schema.sql` | 실행 이력 `search_embedding_index_run`. 숫자·고정 코드만 |
| `deploy/tybot-embedding-index.{service,timer}` | 30분마다 한 회차 |
| `src/tybot/console/embedding_admin.py` · `embedding_routes.py` | 모델 등록·상태·외부 승인, 워크스페이스 정책, 미리 보기, 현황 |
| `console-web/src/pages/EmbeddingIndex.tsx` | 운영 > 임베딩 색인 |
| 기존 수정 | `apply-schema.sh` FILES 1줄, `install.sh` TIMERS 1단어, `tybot-console-timers` UNITS 1줄, `timer_manager.TIMERS` 1항목, `console/app.py` 라우터 등록, `console-web` 의 `App.tsx`(메뉴·경로)·`api/client.ts`(객체형 오류 사유 보존) |

### 실행 잡이 하는 것 — 셋뿐

1. **무엇을 돌릴지**: 켜진 정책이 가리키는 **활성** 모델 revision. 없으면
   `nothing_enabled` 로 끝나고 아카이브도 열지 않는다 → 타이머를 먼저 깔아도 안전
2. **한 번에 하나만**: 이력 표를 advisory xact 락 아래에서 확인한다. `running` 이
   150분보다 오래되면 `run_abandoned` 로 닫고 넘어간다(서비스 `TimeoutStartSec`
   7200초보다 길게)
3. **무엇이 일어났는지**: `ReconciledIndexResult` 의 숫자만 이력에 남긴다. 예외는
   타입으로 고정 코드를 정하고 메시지는 버린다

판정·발행 규칙은 하나도 새로 쓰지 않았다. `scan_complete=True` 는 아카이브 **전체**를
필터 없이 넘길 때만 참이므로, 워크스페이스 필터 옵션은 일부러 두지 않았다 — 일부만
넘기며 「완전하다」 고 하면 reconcile 이 안 보인 문서를 사라진 것으로 판정한다.

## 찾은 빈틈 — `transport` 칸은 사람이 적는 값이다

`authorize_index_request` 는 `transport='local'` 이면 외부 전송 승인 두 개(모델·
워크스페이스)를 보지 않는다. 지금 어댑터가 있는 프로바이더는 OpenAI 하나뿐이고
그것은 언제나 외부다. 정책 행에 `local` 이 적히면 **승인 없이 원문이 OpenAI 로
나간다.** psql 로 행을 채우던 상태에서는 실제로 일어날 수 있었다.

Codex 파일은 고치지 않고 두 겹으로 막았다.

| 자리 | 동작 |
|---|---|
| 콘솔 `embedding_admin._blockers` | 외부 프로바이더 모델이면 `external` 이 아닐 때 **켜든 끄든** 저장 거절 (`provider_requires_external_transport`) |
| 실행 잡 `misdeclared_policies` | 그런 켜진 행이 하나라도 있으면 **전체 실행을 멈춘다** (`transport_misdeclared`) |

**근본 수정은 판정 쪽에 들어갔다**(Codex, 같은 날). `embedding_index_policy.PROVIDER_TRANSPORTS`
가 프로바이더별 실제 전송 경계(`openai → external`)를 정하고, `authorize_index_request` 는
정책 행의 `transport` 가 그것과 다르면 거절한다. 표에 없는 프로바이더는
`embedding_model_provider_invalid` 로 닫힌다. 콘솔·실행 잡의 대조도 같은 표를 읽는다.

## 콘솔이 막는 것

| | |
|---|---|
| 모델 등록 | 언제나 `draft` · 외부 미승인으로 시작. 승인은 별도 사유 |
| 모델 변경 | 차원·모델명은 못 바꾼다(새 revision). 퇴역은 되돌리지 않는다. 켜진 정책이 쓰는 모델은 비활성·퇴역 불가 |
| 정책 켜기 | 활성 모델 · 채널/첨부 중 하나 허용 · 외부면 모델·워크스페이스 양쪽 승인 |
| 정책 끄기 | 언제나 된다(transport 위장만 예외) |
| 충돌 | `expectedRevision` 이 다르면 409. 없다고 본 행이 생겼어도 409 |
| DM | `allow_dm` 은 화면에 없고 저장할 때마다 false |
| 권한 | 관리자. 모델은 전체 워크스페이스 권한, 정책은 그 워크스페이스 권한 |
| 실행 이력 | 일부 워크스페이스만 보는 사람에게는 비운다(회차가 여러 곳을 함께 돈다) |

미리 보기는 `raw_line` 의 문서 수·글자 수 합만 읽어 분량과 추정 비용을 보여 준다.
저장 단추는 **지금 칸 그대로** 미리 본 뒤에만 열린다.

「지금 한 회차 실행」 은 이 화면에 없다. 배치 관리의 타이머 「지금 실행」 을 쓴다 —
실행 경로를 둘로 만들면 하나는 단일 실행 판정을 빼먹는다.

## 시험

| | |
|---|---|
| `tests/test_embedding_index_cli.py` | 20건 — 켜진 곳 없음 · 계획 모드 쓰기 0 · 겹친 실행 거절 · 실패 시 본문 미노출 · transport 위장 차단 |
| `tests/test_embedding_admin.py` | 38건 — 모델·정책 차단 사유 · 충돌 · 범위 · CSRF · 감사 · 라우트 등록 |
| `tests/test_console_timers.py` | 콘솔 표 · root 헬퍼 · install.sh 세 목록 일치 검사 추가 |
| `tests/test_embedding_index_policy.py` | (Codex) 외부 프로바이더의 `local` 위장 거절 · 미분류 프로바이더 닫힘 |
| `tests/test_deploy_scripts.py` | (Codex) 임베딩 표 PUBLIC 회수 · 이력 DELETE 금지 · 시퀀스 GRANT |

`ruff check src tests scripts` 0건. `console-web` 의 `tsc -b --noEmit` · `vite build` 통과.

## 운영에 켜는 순서 (사람이 한다)

1. 배포 후 `sudo /opt/tybot/deploy/apply-schema.sh` — 이력 표 생성
2. 서버에서 프로바이더 스모크 1회 (`2026-11-05-embedding-provider-smoke.md`)
3. 콘솔 > 운영 > 임베딩 색인: 모델 등록 → 활성화 → **외부 전송 승인(오너 결정)**
4. 같은 화면에서 워크스페이스 하나를 미리 보기 → 승인 저장
5. 배치 관리에서 「임베딩 색인」 사용 시작
6. 실행 이력에서 비용·실패 코드를 본다

**3번은 오너 결정이다.** 사내 원문을 OpenAI 로 보내는 일이다.

## 아직 안 한 것

1. **검색 연결 없음.** 색인이 쌓여도 답변은 키워드 검색 그대로다. shadow 비교
   (`embedding_query_run`) → 재현율·지연·비용 측정 → 결합 결정은 B-12 다음 단계다
2. **질의 정책 화면 없음.** `search_embedding_query_policy` 는 소비자가 없어서
   이번 범위에서 뺐다. shadow 를 돌릴 때 같은 방식으로 붙인다
3. **콘솔 화면 배포 경로.** `install.sh` 주석에 따르면 운영 화면은 별도
   `console-web` 저장소에서 배포될 수 있다(`CONSOLE_DIST`). 그렇다면 화면 변경을 그
   저장소에도 옮겨야 메뉴가 보인다. API 는 이 저장소 배포로 나간다
