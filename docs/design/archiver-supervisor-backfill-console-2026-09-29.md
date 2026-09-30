# Archiver 단일 Supervisor·콘솔 운영·소급 수집 작업지시서

작성: 2026-09-29  
대상: Claude Code 구현 담당자, Codex QA 담당자, TYBot 운영자  
상태: 구현 전. 현재 shadow 파일럿의 workspace별 systemd 인스턴스를 대체하는 후속 사양

## 0. 이 문서가 바꾸는 결정

이 문서는 `archiver-shadow-console-operations-2026-09-28.md`의 workspace별 env 파일과
`tybot-archiving-shadow@<workspace>.service`를 최종 운영 모델로 삼았던 부분을 대체한다.

- Archiver 운영 프로세스는 `tybot-archiver.service` 하나다.
- 프로세스 안에서 활성 Slack 앱 연결과 workspace worker를 관리한다.
- workspace별 Slack token, desired state, cursor, health는 DB와 콘솔에서 관리한다.
- workspace를 추가할 때 env 파일이나 systemd 인스턴스를 만들지 않는다.
- 실시간 이벤트 수집을 기본으로 하고 Slack history 소급 수집을 복구 수단으로 추가한다.
- shadow archive root는 하나만 사용한다.
- `raw/`는 사람 원문과 첨부·파생 자료를 분리하는 계약이므로 유지한다.

**2026-09-30 경로 결정:** 아래 §1의 workspace/channel 단위 구조가 이 문서의
기존 `workspaces/<ws>/channels/<id>__<name>` 예시와
`archive-layout-benchmark.md`의 운영 경로 예시보다 우선한다. 구조 2의
날짜별 raw 파일과 별도 첨부 정본이라는 결정은 유지한다. 신규 shadow writer와
reader는 `ARCHIVER_SHADOW_LAYOUT=per-channel-v1` 선택 시 이 경로를 지원한다.
기존 파일럿 자료의 이관 도구와 TY/PF 과거 자료 변환은 아직 완료되지 않았다.
경로 결정 자체가 서버 자료의 이동이나 삭제를 허가하지 않는다.

현재 `tyit`, `mgmt`에 수동 설치된 shadow 서비스와 자료는 파일럿 근거다. 새 구조 검증 전에
삭제하거나 운영 archive로 합치지 않는다.

## 1. 목표 구조

```text
tybot-archiver.service
  └── ArchiverSupervisor
      ├── Socket worker: app token A
      │   ├── workspace tyit authorization
      │   └── workspace mgmt authorization (같은 Slack 앱인 경우)
      └── Socket worker: app token B (다른 Slack 앱인 경우)

/var/lib/tybot/archiver-shadow/
  ├── tyit/<channel-id>__<first-name>/
  │   ├── archive/raw/YYYY-MM-DD.md
  │   ├── archive/attachments/<file-id>/<revision>.md
  │   ├── archive/provenance/<source-id>.jsonl
  │   ├── objects/<file-id>/...
  │   └── staging/<file-id>/...
  └── mgmt/<channel-id>__<first-name>/
      ├── archive/raw/YYYY-MM-DD.md
      ├── archive/attachments/<file-id>/<revision>.md
      ├── objects/<file-id>/...
      └── staging/<file-id>/...
```

경로의 첫 칸이 workspace, 둘째 칸이 채널이다. `workspaces/`와 `channels/`를
다시 붙이지 않는다. `channel-id`가 신원이고 `first-name`은 사람이 찾기 위한
표시다. 채널 이름이 바뀌어도 기존 디렉터리를 계속 사용하고 문서 메타데이터의
표시 이름만 갱신한다. 같은 workspace에 동일 ID의 디렉터리가 둘이면 자동으로
하나를 고르지 않고 수집을 멈춘다. 경로의 각 칸은 정해진 안전 문자로 제한한다.

PF 과거 자료처럼 실제 Slack 채널 ID가 없으면 `legacy-<결정적 해시>__<이름>`을
사용하고, 그 값을 실제 Slack ID로 취급하지 않는다. 채널 소속을 증명할 수 없는
일반 문서는 채널 디렉터리에 억지로 넣지 않고 workspace의 예약된 `_documents/`
영역에 둔다. provenance에는 원본 상대경로와 스냅샷 지문을 보존한다.

다음과 같은 중복 경로는 새로 만들지 않는다.

```text
/var/lib/tybot/archiver-shadow/tyit/archive/workspaces/tyit/...
```

`archive/raw`는 제거하지 않는다. 검색기는 이 경로의 사람 메시지와
`archive/attachments`의 검증된 변환 정본만 읽고, `objects` 원본 파일과
`staging` 중간 산출물은 일반 근거로 읽지 않는다. 이 경계를 채널별로 적용한다.

## 2. Slack 연결 모델

### 2.1 App Token 기준으로 Socket 연결을 묶는다

Slack은 같은 앱의 Socket Mode 연결이 여러 개면 payload를 어느 연결로 보낼지 보장하지
않는다. 따라서 workspace마다 무조건 WebSocket을 하나씩 열면 안 된다.

- supervisor는 활성 Archiver 연결의 App Token을 메모리에서만 비교한다.
- 같은 App Token은 Socket 연결 하나만 연다.
- 다른 App Token은 supervisor 안에서 별도 socket worker를 연다.
- 토큰 평문·평문 hash를 DB, 로그, 감사 payload에 남기지 않는다.
- 토큰 교체 시 영향받은 socket worker만 재연결한다.

### 2.2 멀티 workspace authorization

한 Slack 앱이 여러 workspace에 설치된 경우 이벤트의 `team_id`로 설치를 찾고 해당
workspace Bot Token을 선택한다. Bolt의 multi-workspace `authorize` 계약을 사용한다.

- 알 수 없는 `team_id`는 ACK 후 저장하지 않고 안전한 reason code만 기록한다.
- event의 team/channel 좌표와 선택된 workspace가 다르면 fail closed다.
- Slack Connect 채널은 별도 승인 전까지 자동 cross-workspace 근거로 취급하지 않는다.
- Bot/App 작성 메시지와 DM/MPIM은 기존 정책대로 수집하지 않는다.

## 3. 런타임과 콘솔 책임

### 3.1 단일 서비스

`tybot-archiver.service`는 서버 부팅 후 한 번 시작되고 계속 유지된다. 콘솔은 workspace마다
`systemctl`을 호출하지 않는다.

- 서비스 env에는 Archiver 전용 DB DSN, `PGPASSFILE`, 암호화 key 경로, 공통 shadow root만
  둔다.
- Slack token과 workspace 목록은 env에 넣지 않는다.
- supervisor는 DB desired generation을 주기적으로 읽거나 안전한 notification으로 갱신한다.
- 한 worker 예외가 supervisor와 다른 worker를 종료하지 않는다.
- 동일 App Token socket worker는 한 프로세스 안에서 하나만 존재한다.
- supervisor 자체 중복 기동은 전역 singleton lock으로 막는다.

### 3.2 workspace desired/observed 상태

기존 `bot_connection`은 credential 정본으로 유지한다. 프로세스 운영 상태를 연결 상태와
섞지 않도록 별도 runtime state를 둔다.

최소 상태:

| 구분 | 값 |
|---|---|
| desired mode | `off`, `shadow`, `live` |
| observed state | `stopped`, `starting`, `running`, `degraded`, `error` |
| generation | 설정 변경 세대 |
| heartbeat | 마지막 worker heartbeat |
| last event | 마지막 Slack 이벤트 수신 시각 |
| last write | 마지막 원문 durable write 시각 |
| error | 본문·토큰 없는 reason code와 안전한 문구 |

파일럿에서는 `off`와 `shadow`만 허용한다. `live`는 기존 release gate, 채널별 writer 인수,
Master writer 중지 조건을 모두 통과하기 전에는 API와 UI에서 선택할 수 없다.

### 3.3 콘솔 화면

위치: `봇 관리 > 워크스페이스 연결 > Archiving Bot`

표시:

- Slack 연결/identity와 Manifest 대조 상태
- 수집 희망 상태와 실제 worker 상태
- Slack 앱 참여 채널과 operator hold
- 최근 실시간 이벤트, 최근 durable write, 최근 오류
- 채널별 소급 cursor와 최근 소급 결과
- 공통 shadow root의 workspace 상대경로

동작:

- `Shadow 수집 시작`, `수집 일시 중지`
- `소급 수집 미리보기`, `소급 수집 실행`, `실패 재시도`
- 채널별 operator hold
- 안전한 최근 오류 보기

화면에 `archiver_writes_live`, `revision_reader_ready` 같은 내부 feature flag 이름을 주 조작으로
노출하지 않는다. 필요한 준비 조건은 사람이 이해할 수 있는 문장으로 표시한다.

## 4. 소급 수집 계약

### 4.1 역할

소급 수집은 실시간 수집의 대체가 아니라 누락 복구와 대조 수단이다.

복구할 수 있는 것:

- Slack에 현재 남아 있는 사람 메시지와 thread reply
- 현재 접근 가능한 첨부와 permalink
- 현재 시점의 수정된 본문

완전히 복구할 수 없는 것:

- 수정 전 본문을 실시간 revision 이벤트로 받지 못한 경우
- 이미 삭제되어 Slack history에 남지 않은 본문
- edit/delete 사건의 정확한 과거 순서
- Slack 보존 정책으로 사라진 메시지·파일

콘솔과 보고서는 이를 `완전 복구`라고 표현하지 않는다.

### 4.2 cursor와 job

DB에는 최소한 다음 정본이 필요하다.

- 채널 cursor: workspace, channel ID, last realtime ts, last history ts, last success,
  retry-after, status
- backfill job: workspace, optional channel ID, from/to ts, dry-run, 상태, 요청자, 사유,
  발견/기록/중복/거부/실패 건수, 안전한 오류 코드, 시작/완료 시각

job 상태는 `queued`, `running`, `partial`, `succeeded`, `failed`, `cancelled`를 구분한다.
0건과 실패를 같은 결과로 만들지 않는다.

### 4.3 수집 절차

1. Archiver 앱이 실제 참여 중이고 operator hold가 아닌 채널만 처리한다.
2. `conversations.history`를 끝까지 pagination한다.
3. thread가 있는 메시지는 `conversations.replies`로 reply를 보충한다.
4. bot/app/system 메시지와 DM/MPIM은 제외한다.
5. 첨부는 별도 attachment pipeline으로 보낸다. raw에는 참조만 남기는 것이
   목표이지만, `separate_attachments` 전환 전에는 실시간 writer와 같은 형식을 쓴다.
   전환 전 소급 실행을 별도 첨부 형식의 완료로 보고해서는 안 된다.
6. 기존 writer와 revision classifier를 재사용한다.
7. `(workspace, channel_id, message_ts, revision identity)`로 멱등 처리한다.
8. cursor는 모든 durable write와 ACK 기록이 성공한 뒤에만 전진한다.
9. 재실행은 작은 overlap 구간을 다시 읽고 dedupe한다.
10. Slack `Retry-After`를 지키고 workspace/channel별 진행률을 기록한다.

dry-run은 메시지 본문을 콘솔이나 로그에 출력하지 않고 예상 발견 건수, 대상 채널, 기간,
제외/권한 오류만 반환한다.

### 4.4 이미 raw에 들어간 첨부 본문

소급 수집은 기존 raw 파일의 `[첨부추출:...]` 본문을 수정하거나 삭제하지 않는다.
운영 아카이브의 원문은 특히 shadow 작업의 수정 대상이 아니다. raw를 재작성하면
줄 번호, 검색 색인 좌표, 본문 해시와 과거 답변의 출처가 달라진다. Slack을 다시 읽어도
과거 변환본과 동일한 바이트를 복원한다는 보장이 없다.

감사 편의는 별도 조회 모델로 해결한다. 기존 raw 줄과 첨부 정본을 동일 사건으로
표시하되, 원본 경로와 줄 번호, 첨부 file ID, 변환 revision, 연결 근거와 불확실성을
함께 보여준다. 파일 이름만 같은 경우는 자동으로 동일 첨부라고 단정하지 않는다.
일반 검색에서의 중복 제외와 감사 화면에서의 원본 보존은 별개의 계약이다.

## 5. 현재 shadow 자료와 새 경로

기존 `tybot-archiving-shadow@tyit`, `@mgmt`의 자료는 이 결정 이전 경로로
수집됐다. 기존 `shadow_root.py`는 상대경로를 그대로 복사하므로 새 §1 구조로
변환하는 도구가 아니다. 새 writer·attachment pipeline·reader와 소급 러너의
기본 경로는 PC의 빈 root 통합 시험을 통과했다. 서버의 기존 자료 이관과
provenance·색인·ACL 동등성 검증까지 완료했다는 뜻은 아니다.

오너가 shadow 자료를 새로 만들기로 했더라도 **새 writer가 배포되고 검증되기 전에는
기존 shadow root를 비우지 않는다.** 서버에서의 순서는 다음과 같다.

1. 기존 서비스와 변환 작업을 멈추고, shadow의 파일 목록·SHA-256과 별도 읽기 전용
   사본을 남긴다. 운영 `/var/lib/tybot/archive`는 건드리지 않는다.
2. 새 코드의 빈 목적지 사전 점검을 통과시킨다. TY/PF 과거 자료 변환 검증은
   별도 이관의 선행조건이며, 새 Slack 메시지의 한 채널 shadow 파일럿을 막지 않는다.
3. 새로운 shadow 수집을 시작하고 workspace/channel 경계, 첨부 정본, ACL,
   검색·감사 경로를 확인한다.
4. Slack 소급을 별도 시험한다. 소급은 현재 Slack에 남은 것만 복구하므로
   삭제된 메시지, 수정 전 본문, 만료된 첨부나 과거 변환 이력을 되살린다고
   가정하지 않는다. 기존 사본은 검증 기간 동안 유지한다.

PC 회귀시험은 빈 root에 대한 `plan()` 무쓰기, `run()`의 새 경로 기록,
봇 메시지 배제, 중복 재실행, 첨부 변환 본문과 raw 참조의 분리를 확인한다.
이는 서버 Slack 권한·rate limit·운영 색인과 실제 파일 권한의 검증을 대신하지 않는다.

`scripts/archive_layout_convert.py --to b`는 이전
`workspaces/<ws>/channels/<id>__<name>/raw`만 출력하는 실험용 변환기다.
새 운영 경로에 `--out`을 직접 지정하지 않는다. TY 기존 raw는 바이트 보존
이행과 출처·색인 경로 검증이 필요하다. PF Slack 대화는 실제 채널 ID가 없는
`legacy-` 채널로 변환할 수 있지만, 일반 문서는 채널 소속을 검증하거나
`_documents/`로 분리해야 한다. PF/TY 이행과 Slack 소급은 서로 다른 작업이다.

운영 경로 이행은 shadow 성공 뒤 별도의 승인·동등성 검증·색인 재구축을 거친다.

## 6. 소급 수집 파일럿 시나리오

구현이 완료된 뒤 `tyit` 테스트 채널 하나에서 다음 순서로 시험한다.

1. 콘솔에서 채널의 마지막 realtime cursor와 기준 시각 `T0`를 기록한다.
2. 해당 workspace worker를 `일시 중지`한다. supervisor 서비스 전체는 유지한다.
3. 사람이 일반 메시지 2건, thread reply 1건, 작은 테스트 첨부 1건을 올린다.
4. shadow 경로에 즉시 기록되지 않았음을 확인한다.
5. `T0`부터 현재 `T1`까지 소급 dry-run을 실행한다.
6. 대상 채널·발견 건수·첨부 건수를 확인하고 실제 소급을 승인한다.
7. job이 `succeeded` 또는 누락 사유가 명시된 `partial`로 끝나는지 확인한다.
8. raw 메시지와 별도 attachment 정본이 올바른 workspace/channel 아래 생겼는지 확인한다.
9. 같은 범위를 다시 실행해 새 durable write가 0건인지 확인한다.
10. worker를 재개하고 새 메시지 1건이 실시간으로 기록되는지 확인한다.
11. 운영 archive의 hash/mtime이 시험 전후 동일한지 확인한다.

추가 실패 시험:

- pagination 2페이지 이상
- thread reply 중복
- bot 메시지 제외
- private channel 권한 상실
- Slack 429 후 재시도
- 중간 write 실패 후 cursor 비전진
- 프로세스 재시작 후 job 재개/안전한 재실행
- 같은 App Token의 두 workspace가 Socket 연결 하나를 공유하고 `team_id`로 분리됨
- 다른 App Token worker 하나의 실패가 다른 worker를 중단하지 않음

## 7. 구현 순서

1. 현재 token topology를 평문 비노출 방식으로 조사하고 같은 App Token 그룹을 확인한다.
2. DB schema와 pure state transition을 구현한다.
3. 소급 수집 engine과 dry-run을 먼저 구현한다.
4. single supervisor와 multi-workspace authorization을 구현한다.
5. §1의 새 shadow 경로를 writer·reader·첨부·색인에 적용하고 빈 root 및
   기존 파일럿 사본 검증 도구를 구현한다. 옛 상대경로를 그대로 복사하는
   `shadow_root.py`만으로는 이 단계를 완료한 것이 아니다.
6. 콘솔 API/read model을 구현한다.
7. PC 콘솔 화면을 구현한다. 모바일 QA는 범위에서 제외한다.
8. isolated DB, 전체 pytest, Ruff, frontend build를 통과한다.
9. `tyit` 한 채널에서 §6을 실행한 뒤 `mgmt`로 확대한다.

각 단계는 별도 커밋으로 나눈다. schema를 바꾸면 release gate artifact를 다시 만들기 전까지
운영 DB에 적용하거나 supervisor를 시작하지 않는다.

## 8. Claude Code 구현 금지사항

- 현재 서버의 systemd 서비스 시작/중지, 운영 DB schema 적용, token 등록을 수행하지 않는다.
- `/var/lib/tybot/archive`에 쓰지 않는다.
- 기존 shadow 자료를 삭제·이동하지 않는다.
- Slack token, DSN, 원문을 로그·시험 fixture·커밋에 넣지 않는다.
- 같은 App Token에 workspace별 Socket 연결을 여러 개 만들지 않는다.
- backfill 성공을 edit/delete 과거 이력의 완전 복구라고 표시하지 않는다.
- 콘솔 FastAPI 프로세스에서 임의 shell 또는 `systemctl`을 실행하지 않는다.
- `raw/` 경로 계약을 제거하지 않는다.

## 9. 완료 정의

- 서버에는 Archiver systemd 서비스가 하나만 필요하다.
- workspace 연결 추가·중지·재개·상태 확인이 콘솔에서 가능하다.
- 같은 Slack 앱의 multi-workspace 이벤트가 Socket 연결 하나에서 올바른 workspace로 간다.
- workspace worker 장애가 다른 workspace 수집을 중단하지 않는다.
- 새 레이아웃을 선택한 신규 자료는 공통 root의
  `<key>/<channel-id>__<first-name>/archive/raw/`에만 기록된다.
- 소급 dry-run, 실행, 진행률, 실패, 재실행 멱등성이 자동 시험된다.
- 실시간 수집과 소급 수집이 같은 writer·PII·bot 제외·ACL·revision 계약을 사용한다.
- 운영 archive와 기존 Master writer는 파일럿 동안 변경되지 않는다.
- 전체 pytest, Ruff, frontend build, isolated DB 검증이 통과한다.

