# 통합 봇 관리 콘솔 — 현재 경로 inventory

작성: 2026-09-28 · 단계: [구현 사양](../design/workspace-service-console-redesign.md) §12.2 **1단계**

대상: 이 저장소의 §12.2 2~10단계를 구현하는 개발자와 AI 에이전트

이 문서는 **지금 무엇이 어디에 있는지**만 적는다. 설계가 무엇으로 바꿀지는 사양
문서에 있고, 여기서 다시 쓰지 않는다. 바꿀 자리를 빠뜨리면 그 경로만 옛 표를 계속
읽고, 그건 오류가 아니라 **화면과 실제 값이 다른 상태**로 나타난다.

읽은 시점: 커밋 `3bc150c`. 이후 파일이 움직였으면 줄 번호는 다시 확인한다.

---

## 1. 정본 표와 소유자

| 표 | 정의 | 누가 쓰나(write) | 누가 읽나(read) |
|---|---|---|---|
| `workspace` | `deploy/sql/console_schema.sql:28` | `workspace_store.save_workspace` | 콘솔·`runtime_workspaces`·라우터 |
| `workspace_secret` | `console_schema.sql:57` | `workspace_store.save_workspace:236` | **Master 기동**(`runtime_workspaces:253`) |
| `workspace_service` | `deploy/sql/workspace_service_schema.sql:34` | `workspace_service_store.save_service`·`record_identity` | 콘솔 상세·`archiver_runtime_config` |
| `workspace_service_secret` | `workspace_service_schema.sql:91` | `workspace_service_store.save_service:161` | **Archiver 기동**(SECURITY DEFINER 함수로만) |
| `specialist_bot` | `console_schema.sql:309` | `specialist_store` | 콘솔·`specialist_router` |
| `specialist_workspace` | `console_schema.sql:347` | `specialist_store.py:469` (전량 삭제 후 재삽입) | `specialist_router.py:183` |
| `specialist_source`·`specialist_build`·`specialist_deployment`·`specialist_runtime_secret` | `deploy/sql/specialist_runtime_schema.sql:27·60·93·121` | `specialist_runtime_store` | 콘솔 API `/api/specialist-runtime` |

### 1.1 `workspace_service` 의 현재 의미

```sql
service text NOT NULL CHECK (service IN ('master', 'archiver', 'hermes_direct'))
PRIMARY KEY (workspace, service)
```

제약이 셋 붙어 있다(`workspace_service_schema.sql:62·68·74`).

1. `workspace_service_error_only_when_error` — 상태가 `error` 가 아니면 `error` 는 NULL
2. `workspace_service_enabled_needs_identity` — `enabled` 는 `identity_ok IS TRUE` 이고
   `team_id`·`bot_user_id` 가 비지 않아야 한다
3. `workspace_service_distinct_bot_user` — 같은 워크스페이스에서 봇 사용자 중복 금지
   (부분 유니크 인덱스, `bot_user_id <> ''`)

**2·3번은 새 `bot_connection` 으로 그대로 옮겨야 하는 계약이다.** 2번이 빠지면
신원 검사 없이 켜진 연결이 생기고, 3번이 빠지면 같은 토큰으로 Socket Mode 가 두 번
열린다(CLAUDE.md 금지사항).

### 1.2 `archiver_runtime_config(text)` — 유일한 복호화 경로

`workspace_service_schema.sql:110`. `SECURITY DEFINER`, `search_path` 고정.
`tybot_archiver` 역할에는 `workspace_service_secret` 표 권한이 **REVOKE** 돼 있고
(`workspace_service_schema.sql:203`) 이 함수 EXECUTE 만 있다.

함수가 거르는 조건: `service='archiver'` · `state='enabled'` · `identity_ok IS TRUE` ·
**같은 워크스페이스 master 의 `identity_ok` 와 `bot_user_id` 존재**. 즉 Archiver 는
Master 신원이 확인되기 전에는 기동할 수 없다.

`scripts/verify_schema_isolated.py:122` 가 이 EXECUTE 권한을 필수 항목으로 검사한다 —
새 함수를 만들면 그쪽 `REQUIRED`·`FORBIDDEN` 목록도 같이 늘려야 격리 DB 시험이 의미를
갖는다.

---

## 2. Store 계층

### 2.1 `src/tybot/console/workspace_service_store.py` (276줄)

| 심볼 | 줄 | 역할 | 이전 시 주의 |
|---|---|---|---|
| `Service(StrEnum)` | 35 | `master`·`archiver`·`hermes_direct` | **enum 값이 곧 DB CHECK 값**이다. `bot_key` 로 가면 여기가 사라진다 |
| `TOKEN_BEARING` | 50 | `frozenset(Service)` — 전부 | 새 모델에서는 `bot_catalog.slack_connectable` 이 같은 뜻 |
| `Identity` | 56 | `auth.test` 결과 | 그대로 재사용 가능 |
| `check_identity` | 63 | 다른 워크스페이스 토큰·중복 봇 사용자 거절 | 판정 로직, 표 이름과 무관 → 재사용 |
| `list_services` | 85 | 서비스 + `bot_mask`/`app_mask`/`token_count` | 새 read model 의 원형 |
| `save_service` | 110 | 등록·토큰 교체. 토큰 교체 시 **identity 초기화 + enabled → disabled** | §4.4 계약과 동일. 이 동작을 새 store 에서 반드시 유지 |
| `mask_summary` | 198 | 감사에 남길 가린 값 | 재사용 |
| `record_identity` | 206 | 검사 결과 기록 + 통과 시 `enabled` | **켜는 유일한 자리** |
| `_audit` | 263 | `archive_config_audit` 에 `subject='workspace_service'` | 새 subject 이름을 정해야 한다 |

`save_service:142` 이 `pg_advisory_xact_lock(hashtext('ws-service:<ws>'))` 를 잡는다.
같은 워크스페이스의 동시 저장을 막는 자리이고, 새 store 도 같은 키 공간을 써야
두 경로가 공존하는 기간에 경쟁이 안 난다.

### 2.2 `src/tybot/console/workspace_service_identity.py` (67줄)

`verify_service(workspace, service, *, actor, client_factory=None)` 하나.

- `workspace_service_secret` 에서 bot·app ciphertext 를 **직접 SELECT** 한다(20~32줄).
  → 새 모델에서는 `connection_id` 기준 질의로 바뀐다
- `WebClient(token).auth_test()` 와 `apps_connections_open()` 을 둘 다 호출한다.
  앱 토큰까지 확인하는 이유는 Socket Mode 가 그 토큰으로 열리기 때문이다
- 예외 문구에 토큰을 넣지 않는다(`type(exc).__name__` 만) — §7.5 계약
- 끝에서 `record_identity` 를 부른다 → 검증과 상태 전이가 한 경로다

### 2.3 `src/tybot/console/workspace_store.py`

`_connect`(22) · `_fernet`(56) · `_mask`(69) · `_validate_token`(75) 가 **두 store 의
공용 기반**이다. 새 `bot_connection` store 도 이걸 쓴다(암호화 키가 갈리면 안 된다).

- `save_workspace`(139) — 워크스페이스 metadata **와 Master 토큰을 같이** 저장한다
  (236줄 `INSERT INTO workspace_secret`). §12.2 6단계가 떼어 낼 자리
- `runtime_workspaces`(253) — **Master 기동 경로.** `workspace_secret` 을 읽는다.
  `workspace_service` 를 보지 않는다
- `record_runtime_result`(307) · `limits_by_workspace`(328)

### 2.4 `src/tybot/archiver_runtime_store.py` (68줄)

`load_runtime_config(workspace)` — `archiver_runtime_config()` 호출 + `archive_channel_mode`
(shadow/active 채널) + 전역 플래그 두 개(`separate_attachments`·`attachment_reader_ready`)를
함께 읽는다. 플래그 조합이 어긋나면 **기동을 거부한다**(52줄).

`src/tybot/archiving_bot.py:56` 의 `ARCHIVER_CONFIG_SOURCE` 가 `db`(기본)면 이 경로,
`env` 면 환경변수. systemd 는 `deploy/tybot-archiving-shadow@.service:15` 에서 `db` 로 고정.

---

## 3. API

### 3.1 연결 관련 (변경 대상)

| endpoint | 줄 | 본문·응답 |
|---|---|---|
| `GET /api/workspaces/{key}/archiving` | `app.py:2120` | `archiving_admin.workspace_detail` — 한 번에 전부 |
| `PUT /api/workspaces/{key}/archiving/services/{service}` | `app.py:2134` | `service` 가 `Literal["master","archiver","hermes_direct"]` |
| `PUT /api/workspaces/{key}/archiving/services/{service}/verify` | `app.py:2174` | 동일 Literal |
| `PUT /api/workspaces/{key}/archiving/channels/{channel_id}` | `app.py:2207` | 수집 정책(연결과 분리 대상) |
| `PUT /api/workspaces/{key}/archiving/flags` | `app.py:2236` | 〃 |
| `PUT /api/workspaces/{key}/archiving/retention` | `app.py:2263` | 〃 |
| `GET /api/workspaces` · `PUT /api/workspaces/{key}` | `app.py:1978·2018` | metadata + Master 토큰 동시 |

두 `services` endpoint 의 `Literal` 이 **경로 파라미터 검증**이다. `bot_key` 로 일반화할
때 이 자리가 검증을 잃지 않게 해야 한다 — 잃으면 임의 문자열이 표에 들어간다.

`archiving_admin.workspace_detail:114` 가 `"services": store.services(workspace)` 로
넣고, `archiving_repo.ArchivingRepo.services:71` 이 `workspace_service_store.list_services`
를 부른다. **SQL 을 두 곳에 쓰지 않기로 한 계약**이 주석에 명시돼 있다(72~76줄).

### 3.2 전문 봇 (재사용 대상)

- 레지스트리: `GET /api/specialists`(1228) · `GET /api/specialists/{key}`(1246) ·
  `POST /api/specialists/requests`(1420) · `/requests/{id}/{decision}`(1478)
- 소스 반입: `import-preview`(1312) · `import-upload`(1346) · `rule-test`(1263)
- 런타임: `GET /api/specialist-runtime`(1595) · `sources/git`(1612) · `sources/upload`(1640) ·
  `deployments/{id}/activate`(1703) · `specialists/{key}/disable`(1735) · `rollback`(1759)
- 호출 기록: `GET /api/specialist-calls`(1384)

---

## 4. Frontend

| 파일 | 줄수 | 현재 책임 | 확인된 사실 |
|---|---|---|---|
| `console-web/src/App.tsx` | — | 메뉴 `/manage/specialists` = `전문 봇 관리`(50줄), `/answer/specialists` = `봇 분류 상태`(42줄) | 두 경로 모두 `capability: 'specialists'` 로 잠긴다 |
| `pages/Specialists.tsx` | 335 | `SpecialistAnalytics`(37) · `SpecialistManagement`(87) | 소스 반입·승인 요청·rule test·health chip. **`/api/specialist-runtime` 을 부르는 화면은 저장소에 없다** |
| `components/ArchivingPanel.tsx` | 555 | 토큰 + 수집 정책 혼재 | `SERVICES = ['master','archiver','hermes_direct']`(106), `ServiceRowView`(330), 저장 `${base}/services/${service}`(229), 검증 `.../verify`(233) |
| `pages/Workspaces.tsx` | — | 등록 + Master 토큰 | `botToken`/`appToken` 입력(335·341), `tokenInEnv` 분기(289·362) |
| `pages/Deploy.tsx` | — | `/api/deployment` | specialist runtime 과 **다른 배포**다. 섞지 말 것 |

**유의:** §12.1 은 "기존 `Specialists.tsx` 의 소스·승인·배포·health 기능을 재사용" 하라고
한다. 실제로 있는 것은 소스·승인·health 이고 **배포(activate/rollback) UI 는 아직 없다.**
5단계에서 "재사용" 으로 계획하면 없는 것을 옮기게 된다 — 그 부분은 신규 구현이다.

---

## 5. Manifest

- `docs/pilot/slack-app-manifest.yaml` — Master 용
- `docs/pilot/archiving-app-manifest.yaml` — Archiver 용
- **PF Hermes 직접 연결 Manifest 는 이 저장소에 없다**(§12.2 7단계는 PF 승인본을 쓰라고
  한다). bot key 기준 catalog 를 만들 때 `hermes` 행의 정본 출처를 PF 에서 받아야 한다.

---

## 6. 시험

| 파일 | 덮는 것 |
|---|---|
| `tests/test_workspace_service.py` | `Service` enum·`save_service`·`record_identity`·중복 봇 사용자 |
| `tests/test_workspace_service_identity.py` | `verify_service` 경로와 토큰 비노출 |
| `tests/test_workspace_store.py` | `save_workspace`·`runtime_workspaces`·mask |
| `tests/test_archiver_runtime_store.py` | `load_runtime_config` 거부 조건 |
| `tests/test_archiving_admin.py` | `workspace_detail` read model |
| `tests/test_console_api.py` | 콘솔 endpoint 계약 |
| `scripts/verify_schema_isolated.py` | 역할 권한 `REQUIRED`/`FORBIDDEN` 목록(95·112·121) |

새 표를 추가할 때 `verify_schema_isolated.py` 의 `TARGET_FILES`(88)에 새 SQL 파일을
넣지 않으면 **그 스키마는 격리 DB 검증을 한 번도 안 받는다.**

---

## 7. 2단계로 넘기는 사실과 위험

1. **Master 와 Archiver 의 기동 경로가 다르다.** Archiver 는 `workspace_service`
   (+SECURITY DEFINER), Master 는 아직 `workspace_secret` 이다. §12.2 8단계의 "Master,
   Archiver, PF Hermes 순서" 는 **Master 가 두 단계 이동**(workspace_secret →
   workspace_service → bot_connection)이라는 뜻이 아니라, 한 번에 `bot_connection` 으로
   가는 것이 맞다. `workspace_service.master` 행은 이미 migration 으로 복사돼 있으나
   (`workspace_service_schema.sql:156`) **읽는 쪽이 아직 옮겨지지 않았다.**
2. `workspace_service` 의 PK 가 `(workspace, service)` 라 연결에 별도 ID 가 없다.
   `bot_connection.id` 가 생기면 `bot_connection_secret` FK 와 감사 좌표가 그 ID 로
   바뀐다 — 옛 감사 기록(`field='service.<name>.token'`)과 모양이 달라지므로
   **감사 조회 화면이 둘을 함께 보여줄 수 있어야 한다.**
3. `specialist_workspace` 에는 상태 열이 없다. §4.5 의 `route_mode` 를 넣기 전까지
   "행이 있으면 라우팅 대상" 이고(`specialist_router.py:183`), 그 판정에
   `specialist_bot.state='enabled'` 와 `health<>'error'` 가 함께 걸린다. backfill 을
   `active` 로 할지 결정할 때 **이 세 조건이 현재의 실제 의미**다.
4. `specialist_store.py:469` 가 workspace 배정을 `DELETE` 후 재삽입한다. route 상태 열을
   추가하면 이 경로가 **상태를 매번 지운다.** 2단계 스키마와 3단계 store 를 같이
   보지 않으면 놓친다.
5. `ArchivingPanel.tsx` 의 `SERVICE_LABEL`·`SERVICES` 가 화면에서 순서를 고정한다
   (master → archiver → hermes_direct). 새 화면은 `bot_catalog` 순서를 쓰므로
   **화면 순서가 DB 값에 의존**하게 된다 — seed 순서를 정해 두어야 한다.
6. 콘솔 DB 역할 `tyslackai` 에 `workspace_service*` 의 4권한이 모두 있다
   (`workspace_service_schema.sql:197`). 새 표에도 같은 GRANT 가 필요하고,
   `tybot_archiver` 에는 **주면 안 된다**(§4.4).

---

## 8. 이번 단계에서 하지 않은 것

- 스키마·코드 변경 없음. 이 커밋은 문서 하나다
- 운영 DB 조회 없음 — 위 내용은 전부 저장소 파일에서 읽었다
- 운영 DB 적용·토큰 교체·Archiver active 전환·PF cutover 없음(오너 지시)
