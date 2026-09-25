# Hermes archive handoff and TYBot merge contract

> **현재 상태 (2026-09-23):** 이 문서는 개발용 archive snapshot 인계 계약으로만
> 사용한다. 수집·파일 변환은 별도 Archiving Bot이 소유하고, PF Hermes는 그 기능을
> 제거하는 것으로 결정됐다. 실제 구현은
> [PF Hermes 수정 요청](pf-hermes-archiver-integration-request.md), 전체 책임 경계는
> [Archiving Bot 분리](archiving-bot-separation-2026-09-23.md)를 우선한다.

작성: 2026-09-22
독자: 프금팀 Hermes 개발자와 개발 AI 에이전트

이 문서에서 프금팀 제품은 `Hermes`라고 부른다. `pf-hermes`는 TYBot 저장소에서
대상을 구분하는 내부 식별자일 뿐 제품명이나 버전명이 아니다.

프금팀 개발자나 AI 에이전트가 실제 코드를 수정할 때는 먼저
[`pf-hermes-developer-work-order.md`](pf-hermes-developer-work-order.md)를 따른다. 이 문서는
양측의 인계 범위와 책임 계약이고, 작업지시서는 파일·schema·명령·완료 기준을 정의한다.

## 1. 확정된 방향

이전의 "Hermes 프로세스를 GCP에서 TYBot 서버로 옮긴다"는 계획은 취소됐다.

1. Hermes 원본 서비스와 소스는 GCP에서 계속 운영한다.
2. 2026-09-23에는 **Hermes 자료 Git의 archive snapshot만** TYBot 서버의 격리 구역으로
   복제한다.
3. Hermes와 TYBot Archive Specialist의 장점은 TYBot 코드 안에서 통합한다.
4. 통합 기능은 TYBot 워크스페이스에서 먼저 검증한다.
5. 검증이 끝나면 프금팀에도 독립 Hermes가 아니라 TYBot을 적용한다.

따라서 프금팀은 Rocky용 systemd, 환경 파일, 서비스 계정 또는 배포 스크립트를 만들지
않는다. GCP의 Slack token, Anthropic key와 `.env`도 TYBot 서버로 옮기지 않는다.

## 2. 내일 제공할 archive snapshot

GCP Hermes를 중지하거나 자료 Git 이력을 다시 쓰지 않고 다음을 제공한다.

| 제공물 | 필수 내용 |
|---|---|
| 기준 commit | 자료 Git full SHA, 기준 시각(KST), 기본 branch |
| 접근 방법 | 만료 가능한 read-only deploy key 또는 암호화한 archive 묶음 |
| 구조 설명 | `config.json`, `slack-export/`, `documents/`, 상태 JSON의 의미 |
| 채널 표 | workspace/team ID, channel ID, 현재 이름, 공개/비공개 여부 |
| 자료 분류 | 사람 원문, 첨부 변환본, 사람 승인 요약, AI 요약, 봇 답변 구분 |
| 제외 목록 | 봇 답변, digest, 대화 로그, cache, lock, 임시 파일과 local stamp |
| 증분 기준 | 마지막 수집 timestamp/cursor와 이후 변경분을 다시 내는 방법 |
| 검증값 | 파일 수, Git tree SHA, 가능하면 파일별 SHA-256 manifest |

Git URL, deploy key와 API token은 문서나 채팅에 적지 않는다. 승인된 별도 비밀 전달
경로로만 공유한다.

## 3. 프금팀 개발자가 해야 할 일

### 3.1 자료 의미와 권한을 확정한다

다음 경계를 파일·필드 단위로 설명한다.

- `slack-export/channels/*.md`: 사람 메시지와 스레드 원문 범위, timestamp와 permalink
- `documents/projects/**/*.md`: Slack 첨부 변환본인지 사람이 만든 파생 문서인지,
  원본 file ID와 Slack 링크
- `slack-export/index.md`, `documents/index.md`: 색인이며 원문이 아님
- `.sync-state.json`, `.pending-work.json`, `.pending-edits.json`, `.backfill-state.json`:
  운영 상태이며 답변 근거가 아님
- digest, summary, convo-log와 bot placeholder: 원문으로 import하면 안 되는 파생 출력

모호한 자료는 `unknown`으로 둔다. TYBot은 분류가 끝날 때까지 검색에서 제외한다.
채널은 이름이 아니라 `workspace_id + channel_id`로 식별하고, 권한을 확정하지 못한 자료는
비공개로 분류한다. 삭제·보관·이름 변경 상태도 함께 제공한다.

### 3.2 다섯 skill의 동작 계약을 fixture로 제공한다

`.claude/skills`를 TYBot에 그대로 복사하지 않는다. skill은 대화형 Claude 도구, 로컬 Git,
개인 승인 흐름을 전제하기 때문이다. 대신 아래 불변조건을 비식별 합성 fixture로 제공한다.

| PF skill | 제공해야 할 계약/fixture | TYBot에서 그대로 가져오지 않는 것 |
|---|---|---|
| `archive-run` | partial/unchecked/error를 0으로 보지 않음, 마감 순서, 동시 실행 방지 | 대화형 Git pull/lock/commit/push |
| `slack-sync` | 증분 cursor, 스레드, 새 채널, 이름 변경, 수정·삭제, 봇 발언 제외 | Slack MCP 개인 인증과 Markdown 직접 편집 |
| `doc-archive` | 대화/문서 분리, file ID·원문 링크, 부분 변환, 공개 승인, 원본 누락 | kordoc 직접 호출 절차와 건별 Git commit UI |
| `archive-inbox` | 후보는 근거가 아님, 원문 대조, 반영/제외/보류, 승인 후 hash | 상단 요약 직접 편집과 local stamp |
| `hermes-install` | 단계별 preflight, 채널 참여, backfill, 완료 증적, 재개 지점 | GCP/Node 설치와 별도 Hermes 배포 |

각 fixture에는 입력, 기대 분류, 기대 출처, 기대 권한, 실패 코드가 있어야 한다. 운영 원문을
fixture로 복사하지 않는다.

### 3.3 GCP Hermes는 유지한다

이번 단계에서 실행 프로세스, 코드 저장소, `.env`, Slack 앱, 모델 키, 07:00/17:00 작업과
자료 Git commit/push 절차를 바꾸지 않는다. snapshot 뒤에도 GCP 자료가 늘어나므로 TYBot
전환 직전에 최종 delta snapshot을 한 번 더 제공할 수 있어야 한다.

## 4. 우리 팀이 해야 할 일

snapshot은 live archive에 직접 복사하지 않는다.

```text
/var/lib/tybot/imports/pf-hermes/<snapshot-id>/source
```

우리 팀은 다음 관문을 구현한다.

1. Git SHA, 파일 수와 hash 검증
2. 사람 원문·첨부 변환본·파생 출력·unknown 분류
3. 봇 발언, AI 요약, digest와 운영 상태 제외
4. PII 검사와 불명확 자료 격리
5. workspace/channel ID와 ACL 매핑
6. timestamp, file ID, permalink와 source commit 보존
7. 중복 제거와 재실행 가능한 dry-run importer
8. 사람 승인 후에만 TYBot v2 archive 반영

변환 후 정본은 기존 TYBot 형식이지만 importer만 쓴다.

```text
/var/lib/tybot/archive/workspaces/<workspace>/channels/<channel-id>__<name>/raw/YYYY-MM-DD.md
```

## 5. 통합 후 책임 경계

| 기능 | 소유자 |
|---|---|
| 사용자 의도·복합 요청 분해 | TYBot Master |
| 근거 검색·ACL·출처 | TYBot Archive Specialist |
| 실시간/소급 수집 | collection worker와 채널 관리 콘솔 |
| 첨부·Canvas 변환 | conversion worker와 재시도 queue |
| 요약 후보·검토 | summary review workflow |
| 작업 순서·잠금·checkpoint | job runner와 콘솔 |
| 원문 반영·ACL·PII 예외 승인 | 사람 |
| 답변/검토 Canvas 생성과 공유 | TYBot delivery |
| 설치·workspace onboarding | 배포 preflight와 채널 관리 콘솔 |

Master는 의도를 분류하고 allowlist된 job을 요청할 수 있지만 Git, 변환, 원문 편집,
commit/push를 직접 실행하지 않는다.

## 6. 현재 운영 결함의 목표 동작

- **원문 Canvas 수집/변환 실패**: conversion queue에서 재시도하고 콘솔 진단에 표시한다.
- **검토 Canvas 생성/권한 실패**: 같은 요약 후보를 DM으로 fallback하고 원인 코드를
  콘솔에 표시한다.
- **첨부 파일 DM**: 성공/실패 상세를 보내지 않는다. 성공 본문은 그날 요약의 근거로만
  쓰고 실패·지원 불가는 콘솔에서만 본다.
- **요약 검토 DM**: 계속 보낸다. 미발송은 `no-reviewer`, `no-new-source`,
  `no-accepted-candidate`, `already-sent`, `no-client`, `dm-failed`로 구분한다.

Canvas 실패 때문에 요약 검토 DM 전체가 사라져서는 안 된다.

## 7. 인계 완료 기준

- commit/hash가 확인된 read-only snapshot을 받았다.
- 원문/파생 자료 경계와 channel ID/visibility가 문서화됐다.
- 다섯 skill의 회귀 fixture와 delta 방법을 받았다.
- GCP Hermes는 중단·이전·재설정되지 않았다.
- TYBot live archive에는 아직 직접 섞이지 않았다.

제품 통합 완료는 별도다. importer와 TYBot pilot QA를 통과하고 사람 승인을 받은 뒤에만
프금팀에 TYBot을 적용한다.
