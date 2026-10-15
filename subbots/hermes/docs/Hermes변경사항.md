# Hermes TYBot 통합 변경 내역

작성일: 2026-10-12  
대상: Hermes 원본 개발자 및 운영 담당자  
기준 원본: `82c120f chore: import current Hermes source under specialist path`  
현재 TYBot 저장소: `4e34a68` (`82c120f` 이후 Hermes 변경 포함)

## 목적

이 패키지는 PF팀 Hermes의 검색·추론 기능을 유지하면서 다음 두 실행 경로를 분리합니다.

1. 사용자가 Slack에서 `@Hermes`를 직접 호출하는 기존 실행
2. TYBot Master가 Unix socket RPC로 Hermes를 전문봇으로 호출하는 실행

RPC 실행은 기존 TYBot 내부 Python `ToolSpecialist`의 대체 경로입니다. Hermes가 직접
Slack에 답하지 않고 TYBot에 근거가 연결된 결과를 반환하며, 최종 Slack 전송은 TYBot이
담당합니다.

## 변경 규모

기준 원본부터 현재까지 `subbots/hermes` 아래 **89개 파일**, 약 **7,800줄 추가 및
194줄 삭제**가 있습니다. 아래는 기능 단위 분류이며, 정확한 파일 차이는 다음 명령으로
확인할 수 있습니다.

```bash
git diff --stat 82c120f..4e34a68 -- subbots/hermes
git diff --name-status 82c120f..4e34a68 -- subbots/hermes
```

## 1. 실행 모드와 책임 분리

`src/mode.js`를 추가하고 Hermes의 역할을 명시적인 모드로 분리했습니다.

| 모드 | 역할 |
|---|---|
| `pf` | 기존 PF 직접 실행. 기존 Slack 질문·수집·요약 동작 유지 |
| `pf-archiver` | Slack 질문·요약은 유지하되 원문은 TYBot Archiver 정본에서 읽고 Hermes writer는 차단 |
| `tybot` | TYBot RPC worker 전용. Slack 수집·직접 게시·예약 게시를 하지 않음 |

쓰기 차단은 한 진입점만 막지 않고 Slack 수집, backfill, 파일 반영, Git 반영 경로에 각각
적용했습니다. `tybot` 모드에서 잘못된 직접 실행 경로를 사용하면 조용히 동작하지 않고
기동 또는 실행을 거부합니다.

주요 파일:

- `src/mode.js`
- `src/index.js`
- `src/scheduler.js`
- `src/ingest/**`
- `scripts/run-server.js`
- `scripts/run-ingest.js`
- `scripts/run-backfill.js`
- `.claude/skills/**`의 writer 진입점

## 2. Archiver 정본 읽기

`pf-archiver` 모드에서 Hermes 자체 `slack-export` 대신 TYBot Archiver가 보존한 정본을
읽도록 reader를 추가했습니다.

- 채널 원문과 첨부 변환본을 함께 검색
- 편집·삭제 및 revision 좌표 처리
- 변하지 않은 소스의 재파싱 방지
- 활성 원문과 shadow 자료 분리
- 파일 경로·링크 순회 차단
- Archiver 자료가 비었거나 설정이 틀린 경우 fail closed

주요 파일:

- `src/archive-reader/archiver.js`
- `src/archive.js`
- `scripts/archiver-channel.js`
- `scripts/check-archiver-reader.js`
- `scripts/check-attachment-marks.js`

## 3. 공개 범위와 privacy manifest

Hermes에 운영 DB 자격증명을 주지 않고 파일로 전달된 privacy manifest를 권위 있는 공개
범위로 사용하도록 변경했습니다.

- `visibility: public`이 명시되지 않으면 비공개
- manifest의 생성 시각, 중복 채널 ID, 정본과의 불일치를 검사
- manifest가 유효하지 않으면 `pf-archiver` 전환을 차단
- 비공개 채널은 요청자 권한 범위를 벗어나면 검색·인용하지 않음

주요 파일:

- `src/archive-reader/privacy-manifest.js`
- `scripts/check-archiver-privacy.js`
- `config.example.json`

## 4. 인스턴스 격리와 도메인 프로필

같은 호스트에서 여러 Hermes 인스턴스를 실행할 수 있도록 자료, 상태, 잠금, manifest,
시크릿을 인스턴스별로 분리했습니다.

- `HERMES_DATA_ROOT`
- `HERMES_STATE_DIR`
- `HERMES_ARCHIVER_ROOT`
- `HERMES_ARCHIVER_WORKSPACE`
- `HERMES_PRIVACY_MANIFEST`

표시 문구는 자료 경로나 실행 모드에서 추론하지 않고 `HERMES_DOMAIN`으로 지정합니다.

| 값 | 용도 |
|---|---|
| `pf-construction` | PF/사업장 용어 |
| `enterprise` | 사내 채널/자료 영역 용어 |

설정이 없거나 잘못되면 기본값으로 추측하지 않고 기동을 거부합니다.

주요 파일:

- `src/config.js`
- `src/domain.js`
- `scripts/_check-profile.js`
- `scripts/_child-run.js`

## 5. TYBot 전문봇 RPC worker

Node.js Hermes가 TYBot의 전문봇으로 동작하는 v3 RPC worker를 추가했습니다.

```text
TYBot → /run/tybot-subbots/hermes-<workspace>/http.sock → Hermes
Hermes → /run/tybot/specialist-tools.sock → TYBot 권한 도구
```

특징:

- 외부 TCP 포트를 열지 않는 Unix domain socket
- workspace별 프로세스·계정·소켓·HMAC 분리
- 요청 서명, 응답 서명, nonce 재사용 차단, deadline 및 취소
- 요청별 단기 도구 토큰 사용
- Hermes 프로세스에 Archiver 경로, Slack 토큰, TYBot DB 자격증명을 직접 제공하지 않음
- TYBot이 발급한 `eN` 근거만 최종 답변에서 인용 가능
- `answered`, `no_evidence`, `refused`, `failed` 상태 구분
- RPC worker는 Slack 메시지를 직접 게시하지 않음

주요 파일:

- `scripts/run-rpc-worker.js`
- `src/rpc/answer.js`
- `src/rpc/config.js`
- `src/rpc/prompt.js`
- `src/rpc/replay.js`
- `src/rpc/server.js`
- `src/rpc/tool-client.js`
- `src/rpc/tools.js`
- `src/rpc/wire.js`
- `src/prompts/rpc-answer.md`
- `tybot-specialist.toml`

## 6. Rocky Linux 8 배치

Rocky Linux 8의 systemd 239 제약을 반영한 템플릿 unit과 설치 점검 helper를
추가했습니다.

- 인스턴스: `hermes-rpc@<workspace>.service`
- 전용 계정: `hermes-rpc-<workspace>`
- 공유 그룹: `tybot-specialist-rpc`
- 소켓: `0660`, 공유 그룹 접근
- 코드와 설정은 읽기 전용
- 원문 저장소와 다른 인스턴스 비밀 경로는 접근 차단
- systemd 239에 없는 `LoadCredential=` 대신 worker 전용 파일 경로 사용
- 설치 helper는 계정·시크릿·env를 임의 생성하거나 서비스를 자동 활성화하지 않음

주요 파일:

- `deploy/hermes-rpc@.service`
- `deploy/install-rpc.sh`
- `deploy/README.md`

## 7. Slack 권한 축소

Archiver가 원문과 첨부 수집을 담당하므로 운영 Hermes Slack manifest에서 수집 전용
권한을 제거했습니다.

- 제거: `files:read`, `channels:join`
- 유지: 질문 수신, 채널/DM 읽기, 답변 전송, 사용자 조회, 반응 표시 등에 필요한 권한

manifest를 변경한 Slack 앱은 재설치가 필요할 수 있습니다.

주요 파일: `slack-app-manifest.yaml`

## 8. 검토 결정과 좌표

요약 검토 결과를 단순 문장으로 넘기지 않고 원문 좌표와 해시를 포함한 결정 자료로
내보내도록 확장했습니다. 이전 답변 문장을 다음 답변의 근거로 재사용하지 않고, 원문
좌표를 현재 권한으로 다시 여는 원칙을 따릅니다.

주요 파일:

- `.claude/skills/archive-inbox/scripts/decision_export.py`
- `.claude/skills/archive-inbox/scripts/decide_work.py`
- `.claude/skills/archive-inbox/scripts/review_work.py`

## 9. 점검과 회귀시험

아래 독립 점검을 추가했습니다.

- RPC wire의 Python/Node 상호 호환성
- RPC 서버 요청·응답·취소·deadline
- Slack 직접 게시 및 원문 직접 접근 차단
- systemd unit, SGID/소켓 그룹, 설치 helper 계약
- Archiver reader와 privacy manifest
- 실행 모드별 writer 차단
- 인스턴스별 domain/state 격리

대표 명령:

```bash
npm ci
npm run check:offline
node scripts/check-rpc-wire.js
node scripts/check-rpc-server.js
node scripts/check-rpc-isolation.js
node scripts/check-rpc-unit.js
```

## 변경 커밋

원본 반입 이후 Hermes에 직접 영향을 준 커밋은 다음과 같습니다.

```text
1f96822  Hermes TYBot 연동 모드와 writer 차단
15c36cf  발송 역할 분리와 검토 좌표·해시
f931c67  TYBot 연동에서도 요약 규칙 유지, 직접 게시만 차단
a266358  TYBot 모드 standalone 격리
7a511fe  검토 결정 대조와 원문 좌표
393e309  pf-archiver 모드
fb9a8c8  Archiver 정본 reader
a98156b  첨부 변경 및 경로 순회 방어
7515e8a  privacy manifest 전환 관문
444c2db  privacy manifest 런타임 검증
be1524e  취약한 전이 의존성 갱신
edbb85f  archive 검사와 live 모드 격리
e22b267  변경 없는 Archiver 소스 재파싱 방지
36f58ea  검사 진행 상태와 시간 표시
6d9d5c1  검사 실패·예산 판정 보정
7b646d0  신호 종료와 시간 초과 구분
befebec  활성 첨부 정본 기준 점검
6b27be9  인스턴스별 도메인 프로필
850c582  활성 정본 기준 pf-archiver 점검
8c745a1  인스턴스별 런타임 격리
3b8492b  운영 Hermes 수집 권한 제거
73fa751  v3 전문봇 RPC worker
fb57033  RPC 공유 그룹·enable 배치 계약
45e3e41  Rocky 8 RPC worker 기동 호환
82f14fc  Rocky 8 RPC 소켓 그룹 보정
1553497  systemd 239 권한 보정
```

## TYBot 저장소 쪽 필수 상대 변경

이 ZIP만 배포해서는 TYBot RPC 호출이 완성되지 않습니다. TYBot 쪽에는 다음 구성요소가
함께 있어야 합니다.

- v3 wire 검증과 HMAC credential store
- `/run/tybot/specialist-tools.sock` 도구 서버
- `/run/tybot-subbots/hermes-<workspace>/http.sock` 클라이언트
- 요청 workspace와 현재 사용자 권한으로 묶인 ToolBox
- `specialist_route`의 workspace별 `active` 설정
- `TYBOT_MASTER_V2_WORKSPACES`의 workspace별 rollout 설정

특히 TYBot 커밋 `4310e33`은 Hermes 도구 조회를 요청 workspace와 현재 요청의 실시간
Slack 조회 권한으로 제한합니다. 이 변경은 TYBot Python 코드이므로 Hermes ZIP에는
포함되지 않습니다.

## 알려진 운영 확인 사항
- `HERMES_RPC_VERSION`을 설정하지 않으면 health에는 `unversioned`가 표시됩니다. 운영
  배포에서는 추적 가능한 빌드 버전을 지정하는 편이 좋습니다.

## ZIP에 포함하지 않은 것

다음은 의도적으로 전달 패키지에 넣지 않습니다.

- `.env` 및 실제 Slack/Anthropic 토큰
- HMAC 비밀
- 운영 `config.json`
- Slack 원문, 첨부, archive, 상태 파일, 로그
- `node_modules`
- TYBot Python 소스와 운영 DB 내용

수신자는 `.env.example`, `config.example.json`, `deploy/README.md`를 기준으로 자기
인스턴스의 설정과 비밀을 별도로 주입해야 합니다.
