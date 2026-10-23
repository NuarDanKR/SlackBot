# 콘솔 담당 구분

콘솔은 **화면과 API 모두 최수연(@choisy5570-bot)** 이 운영한다. 봇 본체는 단라운(@NuarDanKR) 담당이다.
이 문서는 「어디까지가 콘솔인가」 를 파일 단위로 정한다. 실제 검토 요청은 `.github/CODEOWNERS` 가 자동으로 보낸다.

## 왜 폴더째 떼어내지 않는가

화면(`console-web`)은 별도 저장소(`choisy5570-bot/console-web`)로 분리했다. API(`src/tybot/console`)는 분리하지 않는다.
이 폴더에는 콘솔 전용 코드와 **봇이 돌아가는 데 필요한 저장소**가 섞여 있다.

- 봇 코드가 이 폴더의 파일을 직접 읽는다 (`workspace_store` 하나만 봇 9곳이 읽는다)
- 콘솔 API도 봇 내부 모듈을 22개 가져다 쓴다
- 둘은 같은 DB 와 같은 설정 파일(`/etc/tybot/tybot.env`)을 쓴다

폴더를 다른 저장소로 옮기면 봇이 바로 멈춘다. 그래서 **저장소는 그대로 두고 담당만 나눈다.**

## 구분 기준

**봇 프로세스가 그 파일을 읽는가.** 직접 import 하거나, 봇이 읽는 파일이 의존하면 「공유」 다.
이 기준은 코드에서 기계적으로 뽑았다(2026-10-02, master `c99c736` 기준). 파일이 늘거나 import 가 바뀌면 다시 뽑는다.

## 1. 콘솔 전용 — 최수연 담당 (24개, 10,434줄)

봇 코드가 import 하지 않는다. 이 파일의 코드를 고쳐도 봇 프로세스가 깨지지는 않는다. 봇 담당 검토 없이 병합해도 된다. 다만 콘솔 기능 대부분이 봇 운영을 다루므로, **무엇을 바꾸는지 PR 설명에 적는다.**

| 파일 | 줄 | 역할 |
|---|---:|---|
| `account_store.py` | 126 | 관리 콘솔 사용자 DB 저장소 |
| `answer_records.py` | 273 | 콘솔 질문·답변 원본 조회 |
| `app.py` | 3185 | 관리 콘솔 API |
| `archiving_admin.py` | 360 | Archiving Bot 운영 손잡이 — 콘솔에서만 돌린다. |
| `bot_admin.py` | 627 | 봇 관리의 판단 — 하나의 Hermes, 여러 연결 |
| `bot_identity.py` | 137 | 저장된 Slack 토큰 쌍을 확인하고 신원만 적는다 |
| `bot_manifest.py` | 178 | Slack App Manifest 정본과 대조 확인 |
| `bot_migration_compare.py` | 72 | Compare legacy and unified bot connection metadata without exposing… |
| `bot_repo.py` | 470 | 봇·연결·라우트의 저장소 경계 — SQL 은 전부 여기 있다 |
| `bot_routes.py` | 348 | 콘솔 API — 봇 관리 (`/api/bots`, 워크스페이스별 봇 연결) |
| `channel_admin.py` | 434 | 콘솔 채널 관리 — 한 화면에서 담당자를 정한다 |
| `health.py` | 747 | 헬스 체크 — 봇이 "돌고는 있는데 제 일을 못 하는" 상태를 드러낸다 |
| `license_routes.py` | 105 | 콘솔 API — 라이선스 현황 (`GET`·`PUT /api/licenses`) |
| `license_store.py` | 389 | Slack 라이선스 현황 — 할당(사람이 입력)과 활성(Slack API)을 한 표로 만든다 |
| `reader.py` | 705 | 화면이 쓸 데이터를 파일에서 만든다 |
| `review_jobs.py` | 300 | 콘솔에서 시작한 채널별 요약 검토 DM 작업 |
| `rule_test.py` | 208 | B-17 rule QA: compare deployed and draft specialist rules in memory |
| `service_logs.py` | 65 | 관리 콘솔용 TYBot 서비스 로그 조회 |
| `specialist_runtime_store.py` | 486 | 전문 봇 2단계 런타임 — 제출·빌드·배포 상태 저장소 |
| `specialist_source.py` | 445 | 전문 봇 2단계 소스 검역 — 받되 실행하지 않는다 |
| `specialist_zip.py` | 182 | Validate uploaded specialist prompt-contract ZIP bundles without… |
| `summary_review_store.py` | 187 | 콘솔이 읽는 요약 검토 회차 상태(B-50) |
| `supervisor_admin.py` | 338 | Archiver supervisor 운영 손잡이 — 콘솔에서만 돌린다. |
| `workspace_service_identity.py` | 67 | Verify a stored Slack service token without returning its plaintext |

## 1-1. 운영 제어 — 최수연 담당, 단라운 검토 (4개, 784줄)

봇 프로세스가 import 하지는 않지만 **봇 운영을 직접 바꾼다.** 담당은 최수연, 병합 전 단라운 검토.

| 파일 | 줄 | 왜 검토가 필요한가 |
|---|---:|---|
| `env_settings.py` | 162 | 관리 콘솔용 환경설정 조회·검증·저장 |
| `timer_manager.py` | 144 | 관리 콘솔의 TYBot systemd 타이머 조회·제어 |
| `release_gate.py` | 247 | 스키마 검증 게이트 — 검증 안 된 스키마로 운영에 쓰지 않는다. |
| `collection_jobs.py` | 231 | 콘솔에서 시작한 채널 파일·과거 원문 소급 수집 작업 |

## 2. 공유 저장소 — 둘 다 검토 (10개, 3,141줄)

콘솔 폴더에 있지만 **봇도 읽는다.** 콘솔 쪽 요구로 고치더라도 봇 담당 검토를 받는다.

| 파일 | 줄 | 역할 | 봇에서 쓰는 곳 |
|---|---:|---|---|
| `archiving_repo.py` | 328 | 아카이빙 봇 상태 저장소 | archiving_bot.py |
| `audit_store.py` | 154 | 감사 기록. 요약 검토 기능이 씀 | summary_review.py |
| `auth.py` | 460 | 콘솔 로그인·세션. PF 콘솔도 같이 씀 | tybot_pf/__init__.py, tybot_pf/auth.py |
| `deploy_approval_store.py` | 272 | 배포 승인 기록. 봇 배포 요청이 씀 | deploy_request.py |
| `llm_secret_store.py` | 167 | LLM API 키 저장소. 모델 호출부가 읽음 | gateway/providers/anthropic_provider.py, gateway/providers/openai_provider.py |
| `specialist_git.py` | 294 | 전문봇 Git 연동(공유 저장소가 의존) | 공유 파일 specialist_store 가 의존 |
| `specialist_store.py` | 566 | 전문봇 규칙 저장소. 전문봇 라우터가 읽음 | specialist_router.py |
| `supervisor_repo.py` | 277 | 아카이빙 supervisor 상태 저장소 | archiving_bot.py |
| `workspace_service_store.py` | 276 | 워크스페이스별 서비스 연결(공유 저장소가 의존) | 공유 파일 archiving_repo 가 의존 |
| `workspace_store.py` | 347 | 워크스페이스 설정 저장소. 봇 답변·비용·아카이브 전반이 읽음 | answer.py, archive/ingest_ack.py, archive/revision_reader.py, archive/revision_store.py, archiver_runtime_store.py, archiving_bot.py, gateway/budget.py, slack/pilot.py, workspaces.py |

## 3. 그 밖의 경로

| 경로 | 담당 | 이유 |
|---|---|---|
| 봇 본체(`src/tybot/` 의 콘솔 밖), `src/tybot_pf/` | 단라운 | |
| 콘솔 전용 테스트 19개 | 최수연 | CODEOWNERS 에 목록 |
| 공유 저장소를 검사하는 테스트 | 단라운 (기본값) | 봇 동작을 함께 고정한다 |
| `deploy/tybot-console.service`, `deploy/nginx/tybot-console.conf` | 둘 다 | 운영 서버에 바로 닿는다 |
| `deploy/sql/console_schema.sql` | 둘 다 | 같은 DB 를 쓴다 |
| `docs/design/console*.md` | 최수연 | |

## 일하는 방식

1. **콘솔 전용 파일(1번)만 고칠 때**: 브랜치 → PR → 최수연이 병합. 봇 배포와 함께 반영된다(콘솔 API 는 `/opt/tybot` 에서 돈다).
2. **운영 제어(1-1)나 공유 저장소(2번)를 고칠 때**: 브랜치 → PR → 단라운 검토 → 병합.
3. **봇이 공유 저장소를 바꿀 때**: 단라운이 PR 에서 최수연에게 알린다. 콘솔 화면이 그 데이터를 표시한다.
4. **콘솔 API 응답 형태를 바꿀 때**: 화면 저장소(`console-web`)도 같이 고친다. 한쪽만 배포되면 화면이 깨진다.

## CODEOWNERS 가 실제로 막으려면

지금은 **검토 요청만 자동으로 간다.** 검토 없이 병합하는 것까지 막으려면 저장소 소유자가
Settings → Branches → `master` 보호 규칙 → **Require review from Code Owners** 를 켠다.
