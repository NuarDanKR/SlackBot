# 통합 봇 관리·워크스페이스 연결 콘솔 구현 사양

작성: 2026-09-28

상태: **구현 지시 가능**

대상: TYBot 콘솔을 구현하는 개발자와 AI 에이전트

관련 문서:

- [Archiving Bot 분리](archiving-bot-separation-2026-09-23.md)
- [Archiving Bot 결정](archiving-bot-decisions-2026-09-25.md)
- [전문 봇 실행 런타임 v2](specialist-runtime-v2.md)
- [콘솔 설계](console.md)

## 0. 오너 결정

콘솔에서 `Hermes Direct`와 `Hermes Specialist`를 서로 다른 봇으로 만들지 않는다.
**Hermes는 하나의 논리 봇·하나의 핵심 런타임**이고, 워크스페이스에 연결되는 방식만
다르다.

```text
전환 기간

PF Slack -- Slack 직접 연결 어댑터 --+
                                      +-- 하나의 Hermes Runtime
TY Slack -- TYBot Master -- 내부 호출 -+

최종 구조

모든 Slack -- TYBot Master -- 내부 호출 -- Hermes Runtime
                         +-- 내부 호출 -- Clio Runtime

별도 Archiving App -------------------- 중앙 Archive
```

`direct`와 `specialist`는 봇의 종류나 복제본이 아니라 **연결 방식**이다.

- `slack_socket`: Slack 사용자가 해당 앱을 직접 호출한다. Bot/App Token이 필요하다.
- `master_internal`: Master가 Unix socket의 내부 계약으로 호출한다. Slack Token이 없다.
- PF 공존 기간에는 하나의 Hermes에 두 연결이 동시에 있을 수 있다.
- PF 전환 완료 시 `slack_socket` 연결만 중지한다. Hermes 런타임과
  `master_internal` 연결은 유지한다.
- TY 워크스페이스에는 특별한 사유가 없으면 Hermes Slack 앱을 설치하지 않는다.
- Hermes와 Clio의 핵심 런타임은 Slack 이벤트를 직접 해석하지 않는다. 직접 호출용
  Slack 어댑터도 같은 내부 요청 계약으로 동일 런타임을 호출한다.

## 1. 목적과 완료 모습

현재 콘솔은 워크스페이스 화면, Archiving 패널, 전문 봇 화면에 연결 설정이 나뉘어 있다.
목표는 다음 세 개념을 분리하면서 한 화면 체계에서 관리하는 것이다.

| 개념 | 정본 | 예시 |
|---|---|---|
| 봇 정체성·런타임 | 봇 레지스트리와 specialist runtime | Master, Archiver, Hermes, Clio |
| 워크스페이스 연결 | Slack connector 또는 Master route | `pilot-Hermes-master_internal` |
| 조직·자료 범위 | `workspace`와 ACL·archive 설정 | pilot, tyit, pf |

완료 후에는 다음이 가능해야 한다.

1. `봇 관리`에서 Master, Archiver, Hermes, Clio를 각각 한 번만 본다.
2. Hermes 상세에서 배포 버전·health·Master 호출 배정·임시 Slack 직접 연결을 함께 본다.
3. `워크스페이스 연결`에서 워크스페이스별 앱 설치와 내부 라우팅 상태를 비교한다.
4. 신규 워크스페이스 등록은 조직 metadata만 만들고, 앱 연결은 같은 wizard의 다음
   단계 또는 `봇 관리`에서 수행한다.
5. Archiver를 연결해도 채널 writer가 자동으로 바뀌지 않는다.
6. PF 직접 Hermes를 중지해도 TY/PF가 사용하는 Hermes 런타임은 중지되지 않는다.

## 2. 용어와 소유권

### 2.1 논리 봇

| key | 종류 | 책임 | Slack 직접 연결 |
|---|---|---|---|
| `master` | orchestrator | 요청 수신, ACL, 라우팅, 답변 전달 | 필수 |
| `archiver` | collector | 채널 원문·revision·첨부·Canvas 수집과 변환 | 필수 |
| `hermes` | specialist | archive 근거 검색, 검토·요약, Q&A | PF 공존 기간에만 선택 |
| `clio` | specialist | Clio 고유 전문 업무 | 필요가 확정될 때만 선택 |

Archiver는 일반 사용자 DM, 검토 DM, 요약 승인, 질문 답변을 담당하지 않는다. Master는
Archiver 인수 후 운영 archive writer와 파일 변환을 담당하지 않는다. Hermes는 수집과
파일 변환을 담당하지 않는다.

### 2.2 연결

연결은 봇 자체가 아니라 **봇이 한 워크스페이스에서 요청을 받는 방법**이다.

| 연결 종류 | 자격증명 | 정본 | 사용 예 |
|---|---|---|---|
| `slack_socket` | Bot/App Token | `bot_connection` | Master, Archiver, PF 임시 Hermes |
| `master_internal` | Slack Token 없음 | specialist workspace route | Master가 Hermes/Clio 호출 |

독립 프로세스로 실행되는 것과 Slack 앱으로 설치되는 것은 다른 문제다. Hermes는 독립
runtime이지만 Master 내부 호출만 받는 워크스페이스에서는 Slack Token이 필요 없다.

### 2.3 어댑터와 핵심 런타임

PF 직접 호출과 Master 호출은 다음 경계에서 합쳐진다.

```text
Slack direct adapter --+
                       +-- Hermes request contract -- Hermes core -- response contract
Master adapter --------+
```

- 두 어댑터는 같은 Hermes active deployment와 contract version을 사용한다.
- Slack direct adapter에 검색·프롬프트·요약 로직을 복제하지 않는다.
- 직접 연결 어댑터는 Slack payload를 내부 계약으로 정규화하고 답변을 전달할 뿐이다.
- Hermes core에는 archive 수집, 파일 다운로드, 파일 변환 writer를 두지 않는다.
- 같은 Slack Token으로 두 Socket Mode 프로세스를 기동하지 않는다.

## 3. 정보 구조

상위 메뉴 `전문 봇 관리`는 **`봇 관리`**로 이름을 바꾼다. Master와 Archiver는 전문
봇이 아니므로 기존 이름은 범위를 잘못 설명한다.

### 3.1 봇 관리

다음 탭을 둔다.

1. `봇`
2. `워크스페이스 연결`
3. `라우팅`
4. `Slack 앱 Manifest`
5. `변경·감사`

#### 봇 탭

목록은 카드 나열이 아니라 비교 가능한 표를 기본으로 한다.

| 열 | 의미 |
|---|---|
| 봇 | 표시 이름과 고정 key |
| 종류 | orchestrator, collector, specialist |
| 런타임 | active version/digest, contract version |
| 상태 | 정상, 대기, 오류, 중지 |
| 연결 | Slack 직접 연결 수, Master 호출 배정 수 |
| health | 마지막 성공, 연속 실패, p95 |
| 소유 팀 | 운영 책임자 |

Hermes는 항상 한 행이다. 행 상세에는 다음 영역을 둔다.

- `런타임`: 소스, 승인 build, active deployment, health, rollback
- `Master 호출`: 배정 workspace와 `shadow|active|disabled`
- `Slack 직접 연결`: PF 공존용 연결, Token mask, Slack identity, 종료 예정 상태
- `계약`: request/response contract와 capability
- `최근 호출`: 성공, fallback, contract violation, 지연

`Hermes Direct`라는 별도 봇 카드나 별도 runtime 행을 만들지 않는다.

#### 워크스페이스 연결 탭

워크스페이스를 행으로, 논리 봇을 열로 둔 matrix를 기본으로 한다.

```text
워크스페이스   Master       Archiver       Hermes                         Clio
pilot          Slack 정상   Slack shadow   Master active                  미배정
tyit           Slack 정상   미연결         Master active                  Master active
pf             Slack 정상   Slack shadow   Master shadow + Slack direct   미배정
```

셀을 선택하면 오른쪽 상세 패널을 열고 페이지 높이를 바꾸지 않는다. 패널은 다음을
연결 방식별로 보여 준다.

- Slack 연결: 설치 단계, Token mask, Team ID, Bot User ID, Socket 상태, Manifest 확인
- 내부 호출: route mode, runtime health, contract version, 최근 호출 결과
- 공통: 마지막 변경자·변경 시각·변경 사유·오류

PF Hermes 셀에 두 연결이 있다고 두 Hermes 행을 만들지 않는다.

#### 라우팅 탭

Master가 specialist를 호출할 수 있는 범위와 상태를 관리한다.

- workspace
- specialist bot key
- route mode: `disabled`, `shadow`, `active`
- capability와 routing hint
- 최소 confidence
- fallback 대상
- 마지막 shadow 비교 결과

`shadow` 호출 결과는 사용자에게 전달하지 않고 품질·계약 비교에만 사용한다. PF 전환
중 직접 Hermes가 답하는 동안 Master route를 `shadow`로 검증할 수 있어야 한다.

### 3.2 워크스페이스 관리

워크스페이스 관리는 다음만 소유한다.

- key와 표시 이름
- 조직 역할과 교차 조회 허용 목록
- 비용 상한
- archive 경로와 수집 현황
- 보존·PII·ACL 정책
- 사용 상태

봇 Token 입력과 Manifest 관리는 워크스페이스 관리의 정본이 아니다. 목록에는 봇 연결
요약과 `봇 연결 관리` 링크만 보여 준다.

신규 등록 흐름:

1. workspace metadata를 `disabled`로 생성하고 UI에서는 `연결 필요`로 표시한다. 현재
   workspace state에 별도 `draft` enum을 추가하지 않는다.
2. `봇 관리 > 워크스페이스 연결`의 해당 workspace로 이동한다.
3. Master Slack 연결을 가장 먼저 설정한다.
4. 필요하면 Archiver Slack 연결과 specialist route를 추가한다.
5. 필수 연결 검증이 끝나야 사용자 요청을 받는 운영 상태로 전환한다.

워크스페이스 생성 transaction에 Token 저장을 강제로 묶지 않는다. Slack 장애가 workspace
metadata 생성까지 롤백시키지 않도록 하고, 연결되지 않은 draft를 운영 가능 상태처럼
표시하지 않는다.

### 3.3 Archiving 화면의 이동

현재 `ArchivingPanel`을 다음처럼 분리한다.

- Archiver Token·Slack identity -> `봇 관리 > 워크스페이스 연결`
- 채널 mode·writer owner·shadow 비교 -> `워크스페이스 관리 > 채널 수집`
- feature flag·보존 -> `워크스페이스 관리 > 정책·보존`
- ACK·revision·첨부 상태 -> `워크스페이스 관리 > 아카이브 상태`
- 설정 변경 이력 -> 통합 `변경·감사`

연결 성공과 writer 인수를 같은 버튼으로 합치지 않는다.

## 4. 데이터 모델

### 4.1 정본 원칙

- `workspace`: 조직·ACL·archive identity
- `bot_catalog`: 논리 봇 identity와 종류
- `specialist_bot` 및 runtime 표: Hermes/Clio 배포와 계약
- `bot_connection`: Slack 앱 연결
- `bot_connection_secret`: 연결별 암호화 Token
- `specialist_workspace`: Master 내부 호출 route

동일 상태를 `bot_connection`과 `specialist_workspace`에 동시에 쓰지 않는다. API가 두
정본을 하나의 화면용 read model로 합친다.

### 4.2 `bot_catalog`

```text
bot_catalog
  key                   PK, ^[a-z][a-z0-9-]{1,31}$
  display_name
  category              orchestrator | collector | specialist
  owner_team
  slack_connectable     boolean
  internally_invokable  boolean
  state                 active | retired
  created_at
  updated_at
```

초기 key는 `master`, `archiver`, `hermes`, `clio`이며 migration에서 멱등하게 seed한다.
향후 승인된 전문 봇을 등록할 수 있도록 이 네 값만 허용하는 DB CHECK는 만들지 않는다.
`Hermes Direct` 행은 만들지 않는다.
`specialist_bot.key`의 Hermes/Clio는 같은 `bot_catalog.key`를 사용한다. specialist runtime
상태를 `bot_catalog`에 복제하지 않는다.

### 4.3 `bot_connection`

```text
bot_connection
  id                    PK
  workspace             FK workspace
  bot_key               FK bot_catalog
  connector_type        slack_socket
  state                 draft | disabled | enabled | error | retired
  team_id
  bot_user_id
  identity_ok
  identity_error
  identity_checked_at
  manifest_id
  manifest_attested_sha256
  manifest_attested_at
  manifest_attested_by
  last_heartbeat_at
  last_event_at
  runtime_version
  runtime_error
  note
  created_at
  updated_at
  updated_by
  UNIQUE (workspace, bot_key, connector_type)
```

현재는 Slack connector만 저장한다. `master_internal`을 이 표에 가짜 행으로 넣지 않는다.
그 정본은 `specialist_workspace`다. 미래에 다른 transport가 실제로 생길 때 enum과 계약을
확장한다.

같은 workspace의 enabled Slack 연결끼리는 `bot_user_id`가 중복될 수 없다. Team ID는
해당 workspace에서 검증된 다른 Slack 연결과 같아야 한다.

### 4.4 `bot_connection_secret`

```text
bot_connection_secret
  connection_id         FK bot_connection
  kind                  bot | app
  ciphertext
  mask
  updated_at
  updated_by
  PRIMARY KEY (connection_id, kind)
```

- 평문 Token 조회 API를 만들지 않는다.
- Token 한쪽만 저장하거나 교체하지 않는다.
- 교체하면 identity와 Socket 검증 결과를 초기화하고 연결을 disabled로 바꾼다.
- Archiver DB role은 자기 연결을 제한된 SECURITY DEFINER 함수로만 읽는다.
- Hermes direct adapter도 자기 연결 이외의 Master/Archiver Token을 읽지 못한다.

### 4.5 specialist route 확장

기존 `specialist_workspace`에 다음 운영 상태를 추가하거나 동등한 별도 route 표로
이관한다.

```text
route_mode              disabled | shadow | active
fallback_bot_key        default master
last_shadow_checked_at
last_shadow_result
updated_at
updated_by
```

기존 행은 현재 실제 라우팅 의미를 확인한 뒤 `active`로 backfill한다. 확인 없이 일괄
`active`로 바꾸지 않는다. 행이 없는 것은 `disabled`다.

## 5. 기존 데이터 마이그레이션

기존 `workspace_service`와 `workspace_service_secret`을 즉시 삭제하거나 의미만 바꾸지
않는다. 다음 순서를 지킨다.

1. 새 표와 제약조건을 추가하고 `bot_catalog`를 seed한다.
2. 기존 연결을 멱등하게 복사한다.

```text
workspace_service.master
  -> bot_connection(bot_key=master, connector_type=slack_socket)

workspace_service.archiver
  -> bot_connection(bot_key=archiver, connector_type=slack_socket)

workspace_service.hermes_direct
  -> bot_connection(bot_key=hermes, connector_type=slack_socket)
```

3. 기존 secret ciphertext와 mask를 연결 ID에 복사한다. 이미 새 표에 값이 있으면 옛
   값으로 덮지 않는다.
4. `specialist_workspace(hermes, workspace)`는 내부 호출 연결로 그대로 유지한다.
5. read 비교 도구로 workspace별 연결 수, mask, Team ID, Bot User ID, state가 같은지
   확인한다.
6. 새 API와 화면을 배포하되 런타임 credential reader는 아직 기존 표를 사용한다.
7. Master, Archiver, Hermes direct adapter를 차례로 새 secret reader로 전환한다.
8. 한 배포 주기 동안 read fallback과 불일치 경고만 유지한다. 장기 dual-write는 하지
   않는다.
9. 옛 write API를 닫고 사용량이 0임을 확인한 뒤 legacy 표 정리는 별도 승인 작업으로
   수행한다.

마이그레이션 감사에는 `hermes_direct -> hermes/slack_socket` 좌표를 남긴다. 이관 때문에
Slack 앱을 재설치하거나 Token을 재발급하지 않는다.

## 6. Slack App Manifest 관리

Slack App Manifest와 specialist runtime contract를 같은 `manifest`로 표시하지 않는다.

### 6.1 Slack Manifest catalog

```text
slack_app_manifest
  manifest_id
  bot_key
  connector_type        slack_socket
  version
  sha256
  source_path
  status                active | retired
  created_at
```

초기 정본:

| 봇 | 용도 | 파일 |
|---|---|---|
| Master | Slack 진입·답변·명령 | `docs/pilot/slack-app-manifest.yaml` |
| Archiver | 채널·첨부·Canvas 수집 | `docs/pilot/archiving-app-manifest.yaml` |
| Hermes | PF 임시 직접 호출 | PF팀 승인 후 별도 정본 추가 |

`Hermes Direct Manifest` 대신 `Hermes / Slack 직접 연결 Manifest`라고 표시한다. 내부 호출
연결에는 Slack Manifest가 없다.

### 6.2 검증 상태

다음 상태를 합치지 않는다.

- `신원 확인`: Team ID와 Bot User ID
- `Socket 연결`: App Token으로 연결 가능
- `기능 확인`: 허용된 read-only probe
- `Manifest 적용 확인`: 관리자가 정본 hash와 Slack 설정 대조
- `런타임 health`: 실제 프로세스 상태

`auth.test` 성공만으로 Manifest가 일치한다고 표시하지 않는다.

## 7. API 계약

### 7.1 봇과 연결 조회

```text
GET  /api/bots
GET  /api/bots/{bot_key}
GET  /api/bot-connections?workspace={workspace}
GET  /api/workspaces/{workspace}/bot-connections
```

응답의 Hermes는 한 객체이며 연결을 배열로 가진다.

```json
{
  "key": "hermes",
  "runtime": {"state": "active", "version": "...", "health": "ok"},
  "bindings": [
    {"workspace": "pf", "type": "master_internal", "mode": "shadow"},
    {"workspace": "pf", "type": "slack_socket", "state": "enabled"}
  ]
}
```

### 7.2 Slack 연결 관리

```text
PUT  /api/workspaces/{workspace}/bot-connections/{bot_key}/slack
POST /api/workspaces/{workspace}/bot-connections/{bot_key}/slack/verify-identity
POST /api/workspaces/{workspace}/bot-connections/{bot_key}/slack/probe
PUT  /api/workspaces/{workspace}/bot-connections/{bot_key}/slack/manifest-attestation
POST /api/workspaces/{workspace}/bot-connections/{bot_key}/slack/disable
POST /api/workspaces/{workspace}/bot-connections/{bot_key}/slack/retire
```

Slack 연결 생성은 해당 `bot_catalog.slack_connectable=true`일 때만 허용한다. Hermes의 직접
연결은 공존 목적과 종료 계획을 변경 사유에 필수로 받는다.

### 7.3 Master route 관리

```text
GET /api/workspaces/{workspace}/bot-routes
PUT /api/workspaces/{workspace}/bot-routes/{bot_key}
```

요청에는 `mode`, `fallbackBotKey`, `reason`을 받는다. `bot_key`가 specialist이고 active
runtime health와 contract가 확인된 경우에만 `active`를 허용한다. `shadow`는 사용자에게
결과를 전달하지 않는다.

### 7.4 호환 endpoint

기존 `/api/workspaces/{key}/archiving/services/{service}` endpoint는 새 store로 전환하는
동안 호환 adapter로만 유지한다.

- `master` -> `master/slack_socket`
- `archiver` -> `archiver/slack_socket`
- `hermes_direct` -> `hermes/slack_socket`

신규 frontend는 이 endpoint를 호출하지 않는다. 호환 adapter 사용량을 계측하고 0이 된
뒤 삭제한다.

### 7.5 오류와 보안

- 잘못된 Token 형식: `422`
- 다른 Slack Team ID: `409`
- 같은 Bot User ID 중복: `409`
- specialist가 아닌 봇에 내부 route 요청: `422`
- health가 나쁜 runtime의 active route: `422`
- Slack 일시 장애: `503`, 저장 여부 명시
- 응답·로그·감사·예외에 Token 평문 금지

## 8. 전환 운영

### 8.1 TY 파일럿

1. pilot workspace의 Master 연결을 새 화면에서 조회한다.
2. Archiver Slack 연결을 등록하고 `shadow` 수집만 기동한다.
3. Hermes runtime을 한 개 배포한다.
4. pilot의 Hermes `master_internal` route를 `shadow`, 이후 `active`로 올린다.
5. Hermes Slack 직접 연결은 만들지 않는다.

### 8.2 PF 한 달 공존

PF의 Hermes에는 다음 두 binding이 함께 보인다.

- `slack_socket=enabled`: PF 사용자가 기존 방식으로 직접 호출
- `master_internal=shadow`: Master가 같은 질문 계약을 비교 실행

두 경로는 같은 Hermes active deployment를 사용한다. shadow 결과는 Slack에 보내지
않는다. 수집은 Archiver만 수행한다.

### 8.3 PF cutover

1. Archiver의 PF archive 수집·첨부 변환 gate를 통과한다.
2. Master route shadow 비교가 기준을 통과한다.
3. PF Master 연결과 ACL을 확인한다.
4. `master_internal`을 active로 바꾼다.
5. Hermes `slack_socket`을 disabled로 바꾼다.
6. 중복 답변이 없고 fallback이 동작하는지 확인한다.
7. 관찰 기간 뒤 직접 연결을 retired로 바꾼다.

4번과 5번은 한 운영 변경으로 묶되 실패 시 직접 연결을 다시 enabled로 돌릴 수 있어야
한다. Hermes runtime 자체를 중지하거나 다른 배포로 바꾸지 않는다.

## 9. Archiver 안전장치

콘솔 개편은 Archiver 운영 전환 gate를 완화하지 않는다.

- 연결 성공만으로 채널 mode를 `active`로 바꾸지 않는다.
- `off -> active` 직접 전환을 허용하지 않는다.
- `shadow` 대조와 cutover watermark가 필요하다.
- `revision_reader_ready`, `attachment_reader_ready`, `separate_attachments`,
  `archiver_writes_live`, ACK, 보존 정책 gate를 그대로 검사한다.
- Slack 채널 이름 규칙은 Archiver 수집 허용 조건이 아니다. 채널 ID와 콘솔 정책으로
  대상을 결정한다.
- Archiver는 일반 사용자 DM을 수집하지 않는다.
- Token 연결과 writer owner 변경은 별도 감사 이벤트다.

## 10. Frontend 세부 계약

- 메뉴 이름은 `봇 관리`로 한다.
- 정본 URL은 `/manage/bots`로 하고 기존 `/manage/specialists`는 선택 상태를 보존해
  redirect한다. 기존 bookmark를 즉시 깨지 않는다.
- 하위 URL은 `/manage/bots/connections`, `/manage/bots/routing`,
  `/manage/bots/manifests`, `/manage/bots/audit`로 고정한다.
- `Workspaces.tsx`에서 Token 입력과 `ArchivingPanel` 연결 UI를 제거한다.
- workspace 목록에는 연결 요약과 봇 관리 deep link만 둔다.
- 봇/연결 목록은 조밀한 표를 사용하고 카드 안에 카드를 넣지 않는다.
- 상태는 색만으로 구분하지 않고 아이콘과 텍스트를 함께 쓴다.
- 연결 방식은 `Slack 직접`, `Master 호출`로 표시한다. 사용자 화면에
  `Hermes Specialist`라는 두 번째 봇 이름을 만들지 않는다.
- Token 입력은 password input이고 저장 성공 후 즉시 비운다.
- Token은 mask만 다시 표시한다.
- 긴 workspace/봇 이름은 줄바꿈하되 명령 버튼과 겹치지 않는다.
- 선택한 탭, workspace, bot은 URL query 또는 path로 보존한다.
- 위험 동작은 변경 영향과 rollback을 보여 주는 확인 modal을 사용한다.
- 연결 disable과 runtime disable을 다른 명령과 문구로 표시한다.

## 11. 시험 계약

### 11.1 스키마·마이그레이션

- 빈 DB 최초 적용과 재적용이 모두 통과한다.
- 기존 `workspace_service` 세 종류가 정확한 bot key로 멱등 이관된다.
- `hermes_direct`와 `specialist_workspace.hermes`가 하나의 Hermes read model로 합쳐진다.
- 기존 ciphertext와 mask가 변하지 않는다.
- 새 값이 있는 행을 legacy migration이 덮지 않는다.
- 동일 workspace의 Slack Bot User ID 중복을 DB와 store가 모두 막는다.
- Archiver와 Hermes adapter DB role이 다른 연결의 secret을 읽지 못한다.

### 11.2 Backend

- `/api/bots`에 Hermes가 한 번만 나온다.
- Hermes가 한 workspace에서 direct와 internal binding을 동시에 반환할 수 있다.
- internal binding에는 Token 필드가 없다.
- Token 교체 시 identity 결과가 초기화되고 연결이 disabled가 된다.
- 연결 disable이 specialist runtime state를 바꾸지 않는다.
- runtime disable이 Slack 연결을 enabled 상태로 방치하지 않도록 blocker가 동작한다.
- shadow route 결과가 사용자 응답으로 전달되지 않는다.
- active route는 ACL 필터를 통과한 evidence만 Hermes에 전달한다.
- legacy endpoint와 신규 endpoint의 read 결과가 이관 기간에 일치한다.
- 응답·감사·예외에 평문 Token이 없다.

### 11.3 Frontend

- 봇 목록에 Master, Archiver, Hermes, Clio가 한 번씩만 보인다.
- PF Hermes 한 행에 `Slack 직접`과 `Master 호출` badge가 함께 보인다.
- workspace 등록 화면은 봇 Token을 요구하지 않는다.
- 연결 화면에서 Master를 먼저 설정하도록 안내하지만 Archiver/Hermes와 같은 연결
  모델을 사용한다.
- Hermes 내부 호출에는 Token 입력란이 없다.
- Slack 연결에는 Bot/App Token 쌍 입력란이 있다.
- Manifest 적용 확인과 identity/probe/health를 별도 상태로 표시한다.
- 연결 중지와 runtime 중지 확인 문구가 다르다.
- 데스크톱과 모바일에서 표·상세 패널·긴 이름이 겹치지 않는다.

### 11.4 운영 회귀

- 기존 Master와 Archiver 프로세스가 새 콘솔 배포로 재시작되지 않는다.
- 신규 화면 배포만으로 writer owner가 바뀌지 않는다.
- 같은 Slack Token의 중복 Socket Mode 기동이 없다.
- PF direct와 Master shadow를 함께 켜도 사용자에게 답변은 한 번만 전달된다.
- PF cutover rollback이 Hermes runtime 재배포 없이 가능하다.
- 전체 `pytest`, `ruff`, frontend build, manifest hash 검사가 통과한다.

## 12. Claude 구현 순서

### 12.1 현재 코드 변경 지도

구현 전에 최소 다음 위치를 읽고 inventory 결과에 포함한다.

| 현재 위치 | 현재 의미 | 목표 변경 |
|---|---|---|
| `deploy/sql/workspace_service_schema.sql` | `master|archiver|hermes_direct` 고정 | 새 연결 표와 무손실 backfill, legacy 호환 |
| `src/tybot/console/workspace_service_store.py` | Slack 서비스 enum·secret | `bot_key/slack_socket` store로 이전 |
| `src/tybot/console/workspace_service_identity.py` | Slack identity 검증 | 신규 connection ID 기준으로 일반화 |
| `src/tybot/console/app.py`의 archiving service route | 기존 연결 API | 신규 API 호출, legacy adapter 유지 |
| `console-web/src/components/ArchivingPanel.tsx` | Token·수집 정책 혼재 | 연결 UI와 수집 정책 분리 |
| `console-web/src/pages/Workspaces.tsx` | workspace와 Master Token 동시 편집 | metadata-only 등록과 연결 deep link |
| `console-web/src/pages/Specialists.tsx` | specialist만 관리 | 통합 봇 상세의 runtime·route 영역으로 편입 |
| `console-web/src/App.tsx` | `전문 봇 관리` 메뉴 | `봇 관리` URL과 legacy redirect |
| `deploy/sql/console_schema.sql` | `specialist_bot/workspace` | route mode와 bot catalog 연계 |
| `docs/pilot/*manifest.yaml` | 역할별 Slack 정본 | bot key 기반 catalog와 hash 검증 |

기존 `Specialists.tsx`의 소스·승인·배포·health 기능을 폐기하거나 다시 구현하지 않는다.
Hermes 상세의 `런타임` 영역으로 재사용한다.

### 12.2 커밋 순서

각 단계는 별도 커밋으로 만들고 그 단계의 테스트를 통과한 뒤 다음으로 간다.

1. **현재 경로 inventory**
   - `workspace_service`, `workspace_service_secret`, `workspace_secret`
   - `specialist_bot`, `specialist_workspace`, runtime deployment
   - 기존 API, React 호출부, systemd credential reader
   - 결과를 verification 문서에 파일·함수 단위로 기록
2. **schema와 migration**
   - `bot_catalog`, `bot_connection`, `bot_connection_secret`
   - specialist route mode
   - legacy backfill과 격리 DB 시험
3. **store와 통합 read model**
   - 하나의 Hermes + 여러 binding
   - secret 격리와 감사
   - legacy/new 비교 도구
4. **신규 API**
   - 봇 조회, Slack 연결, route, Manifest attestation
   - 기존 endpoint 호환 adapter
5. **봇 관리 frontend**
   - 메뉴 이름 변경
   - 봇 표, workspace matrix, 상세 패널, 라우팅 탭
   - Hermes 단일 행 회귀시험
6. **워크스페이스 화면 정리**
   - metadata-only 등록
   - Token UI 제거
   - 연결 deep link
   - ArchivingPanel 책임 분리
7. **Manifest catalog**
   - Master/Archiver 정본 연결
   - PF Hermes 직접 연결 Manifest는 PF 승인본을 사용
8. **runtime credential 전환**
   - Master, Archiver, PF Hermes adapter 순서
   - read 비교 후 legacy write 중지
9. **파일럿**
   - pilot 또는 tyit에서 Master/Archiver/Hermes 내부 route 검증
   - 운영 active 전환은 Archiver release gate와 별도 승인
10. **PF 공존·cutover 도구**
    - direct + shadow 동시 표시
    - 원자적 전환과 rollback
    - 관찰 뒤 legacy UI/API 정리

임의 리팩터링을 섞지 않는다. schema, secret reader, frontend를 한 커밋에 넣지 않는다.
운영 DB 적용, Token 교체, 채널 active 전환, PF cutover는 코드 배포와 별도의 사람 승인
작업이다.

## 13. 구현 금지 사항

- `Hermes Direct`와 `Hermes Specialist`를 별도 bot row로 만들지 않는다.
- 내부 호출을 표현하려고 가짜 Slack Token이나 가짜 `bot_connection`을 만들지 않는다.
- PF 직접 어댑터에 별도 검색·요약·답변 로직을 복사하지 않는다.
- workspace를 앱마다 중복 생성하지 않는다.
- frontend에서 임의 socket path, endpoint URL, shell command를 입력받지 않는다.
- Token 평문 조회·복호화 API를 만들지 않는다.
- 연결 생성과 동시에 Archiver writer 또는 specialist active route를 켜지 않는다.
- 새 인터넷 인바운드 포트를 열지 않는다. specialist transport는 Unix socket을 유지한다.
- 기존 `workspace_service`를 검증 없이 삭제하거나 in-place로 다른 의미로 바꾸지 않는다.

## 14. 완료 정의

다음을 모두 만족해야 완료다.

- 콘솔에서 Hermes가 하나의 봇으로만 보인다.
- PF 공존 기간의 Slack 직접 연결과 Master 호출이 같은 Hermes 아래에 보인다.
- Master, Archiver, Hermes, Clio를 통합 `봇 관리`에서 관리한다.
- workspace 화면과 봇 연결 화면의 책임이 분리됐다.
- Slack 연결과 내부 route의 정본이 중복되지 않는다.
- 기존 `hermes_direct` Token과 신원이 무손실 이관됐다.
- PF 직접 연결 중지가 Hermes runtime 중지로 이어지지 않는다.
- Master가 Hermes를 호출할 때 Slack Token이 필요하지 않다.
- Archiver 연결과 writer cutover가 별도 gate로 유지된다.
- 시크릿·ACL·감사·Manifest·중복 기동 방어 시험이 통과한다.
- 운영 적용과 rollback 절차가 verification 문서에 실제 명령과 결과로 남는다.
