# PF archive 이전과 Hermes 통합 — 내부 실행 계획

> **상태 변경 (2026-09-23):** 아래 snapshot 반입 절차와 조사 결과는 유효하지만,
> Hermes 기능을 TYBot 코드 안에 흡수한다는 실행안은 폐기됐다. 현재 정본은
> [Archiving Bot 분리와 Hermes 직접 호출](archiving-bot-separation-2026-09-23.md)이며,
> PF 측 구현은 [PF Hermes 수정 요청](pf-hermes-archiver-integration-request.md)을 따른다.

작성: 2026-09-22
독자: 내부 오너와 작업을 수행하는 AI 에이전트
내일 목표: **PF archive 불변 snapshot을 TYBot 서버 격리 구역에 복제·검증**

프금팀 개발자에게는 이 문서 전체가 아니라
[`pf-hermes-developer-work-order.md`](pf-hermes-developer-work-order.md)를 우선 전달한다.
그 문서가 실제 구현 순서이고, 본 문서는 우리 팀의 운영·이전 계획이다.

## 1. 확정 결정과 금지선

1. 프금팀 Hermes 원본은 GCP에서 계속 운영한다.
2. 2026-09-23에는 archive만 이동한다.
3. 두 Hermes의 기능은 TYBot 코드 안에서 통합한다.
4. 통합본은 TYBot에서 먼저 검증한다.
5. 검증 후 프금팀에도 TYBot을 적용한다.

내일은 GCP Hermes를 중지하지 않고 Rocky에 Hermes runtime을 배포하지 않는다. 과거 계획의
`pf-hermes.service`, Node runtime, 별도 Slack/Anthropic env, 단일 인스턴스 전환도 하지
않는다. `deploy/setup-pf-hermes-host.sh`는 이번 작업에 실행하지 않는다.

## 2. 사람·에이전트·서비스 역할

| 주체 | 책임 | 금지 |
|---|---|---|
| 내부 오너 | 범위·비밀 전달·ACL·import 승인, go/no-go | 원문 직접 수정, 시크릿을 채팅에 입력 |
| 프금팀 개발자 | snapshot/schema/분류/delta/fixture 제공 | TYBot live archive에 직접 쓰기 |
| 개발 에이전트 | importer·필터·테스트·콘솔 상태 구현 | PII 예외 결정, 승인 없는 import |
| TYBot Master | 의도 분류, 대상 확인, job 요청, 결과 설명 | Git·원문·변환 파일 직접 변경 |
| worker/console | 수집·변환·재시도·잠금·checkpoint·감사 | 답변 생성, ACL 예외 결정 |
| 검토자 | 요약 정확성 확인과 정정 | 첨부 변환 로그 검토 |

## 3. PF skill 전체 통합 방침

### 3.1 `archive-run`: 상황판과 조율

가져올 것:

- 남은 작업, 수량 신뢰도, 마감, 담당 worker를 한 화면에 표시
- `partial`, `not-counted`, `error`를 0건과 구분
- 작업 간 동시 실행 방지와 단계별 상태

TYBot 구현 위치:

- Master: 자연어 요청을 작업 종류로 분류하고 대상/권한을 확인
- Console: 상황판, 실행 버튼, 진행률과 실패 코드
- Job runner: lock, timeout, retry, checkpoint와 감사 기록
- 사람: 원문 반영과 예외 승인

Master는 딱 세 단계까지만 맡는다: 의도 분류, 대상 확정, allowlist job 요청. Git pull,
변환, 파일 쓰기, commit/push는 맡기지 않는다.

### 3.2 `slack-sync`: 대화 수집

가져올 것:

- 마지막 cursor 이후 증분 수집
- thread reply, 새 채널, rename, edit/delete 처리
- 봇 발언 제외와 Slack permalink 보존
- backfill과 실시간 수집의 동일한 원문 계약

TYBot 구현 위치는 collection worker, `collect`, 과거 전체 수집 job과 archive writer다.
개인 Slack MCP 인증과 채널 Markdown 직접 편집은 가져오지 않는다.

### 3.3 `doc-archive`: 첨부 문서

가져올 것:

- 사람 대화와 문서 본문의 분리
- file ID, 채널, timestamp, 원문 링크와 변환 provenance
- 부분 변환/원본 없음/지원 불가의 명시적 상태
- 공개 승인과 문서 메타의 연결
- 바이너리와 임시 산출물이 Git/원문 archive에 섞이지 않는 검사

TYBot 구현 위치는 file staging, conversion queue, nightly retry와 archive diagnostics다.
Claude skill의 kordoc 호출 절차, 사업장 폴더 추측, 건별 Git commit UI는 가져오지 않는다.

### 3.4 `archive-inbox`: 요약 검토

가져올 것:

- 모델의 불일치 판단은 후보일 뿐 근거가 아님
- 원문 대조 뒤 승인/정정/보류/만료
- 검토한 버전의 hash와 이후 변경 감지
- 미승인 요약을 답변 근거로 사용하지 않음

TYBot 구현 위치는 summary review DB, 검토 Canvas, DM과 승인 요약 검색이다. 원문 블록이나
채널 문서 상단 요약을 직접 편집하지 않는다. 사용자가 정정 text를 보내면 다시 후보를
만들고 재검토하며, 확인하지 않은 후보는 만료·폐기한다.

### 3.5 `hermes-install`: 설치와 onboarding

가져올 것:

- 단계별 preflight와 "했다"가 아니라 "됐다"는 증적
- Slack app scope/event, bot channel membership, archive 초기화, backfill 확인
- 실패 단계부터 안전하게 재개
- fixture와 live-read smoke의 분리

TYBot 구현 위치는 deploy/install 검사, workspace registry, 채널 관리 콘솔과 진단 스크립트다.
GCP/Node 설치와 독립 Hermes 배포 마법사는 가져오지 않는다.

### 3.6 skill 대응표

| PF skill 작업 | TYBot 실행 주체 | Master가 하는 일 |
|---|---|---|
| 미변환 첨부 | conversion queue/nightly retry | 상태 설명, job 요청 |
| 새 채널 과거 대화 | channel backfill job | 채널·범위 확인 |
| 증분 대화·스레드 | collector | 요청하지 않음, 상태만 설명 |
| 수정·삭제 | audited collection job | 변경 종류 분류 |
| 공개 승인 반영 | review/ACL workflow | 승인 요청 안내 |
| 아침 보고·요약 | summary review | 요약 의도 전달 |
| 반영/제외/보류 | review state machine | 사용자의 text 정정을 연결 |
| 설치·채널 참여 | deploy/channel console | onboarding 요청 분류 |
| Git/lock/commit/push | importer/job runner + 사람 | 관여하지 않음 |

## 4. 오늘 준비

사람:

1. 프금팀에 [`pf-hermes-migration-handoff.md`](pf-hermes-migration-handoff.md)를 전달한다.
2. 자료 Git full SHA와 read-only 전달 방법을 확정한다.
3. workspace/team ID, channel ID, visibility 표를 요청한다.
4. 내일 대응할 프금팀 담당자와 내부 승인자를 정한다.
5. 최종 delta snapshot 일정을 별도로 잡는다.

에이전트:

1. archive inventory와 source/derived/unknown 분류 보고서를 만든다.
2. dry-run importer mapping schema와 중복 키를 설계한다.
3. 다섯 skill의 불변조건 fixture를 만든다.
4. 기본 비공개 ACL, channel ID 필수, 봇 발언 제외 검사를 만든다.
5. import 전후 file/message/attachment 수와 source commit 비교를 만든다.

## 5. 내일 08:00~12:00 실행 시간표

| 시각 | 작업 | 완료 조건 |
|---|---|---|
| 08:00~08:20 | go/no-go | SHA, read-only 전달, ID/권한 담당자, 기준 시각 확인 |
| 08:20~09:00 | 격리 경로 준비 | live archive 밖 새 snapshot 경로 |
| 09:00~10:00 | clone/반입 | detached commit이 기대 SHA와 일치 |
| 10:00~11:00 | hash·파일 수 검증 및 불변화 | clean tree와 manifest |
| 11:00~12:00 | inventory | 분류/누락/unknown 보고서, live archive 변경 0 |
| 13:00 이후 | importer 설계/fixture | 운영 import는 하지 않음 |

### 5.1 go/no-go

다음 중 하나라도 없으면 복제하지 않고 자료 목록만 확인한다.

- 자료 Git full SHA 또는 암호화 묶음 SHA-256
- read-only 접근 수단
- workspace/team ID와 channel ID mapping 담당자
- snapshot 시각과 GCP 마지막 수집 기준
- snapshot 보존/삭제 책임자

### 5.2 서버 격리 경로

```text
/var/lib/tybot/imports/pf-hermes/<snapshot-id>/source/
/var/lib/tybot/imports/pf-hermes/<snapshot-id>/manifest/
/var/lib/tybot/imports/pf-hermes/<snapshot-id>/reports/
```

`snapshot-id`는 `YYYYMMDD-HHMMSS-<short-sha>`다. live
`/var/lib/tybot/archive/workspaces`에는 아무것도 쓰지 않는다.

```bash
set -euo pipefail
SNAPSHOT_ID="20260923-080000-<short-sha>"
IMPORT_ROOT="/var/lib/tybot/imports/pf-hermes/${SNAPSHOT_ID}"

sudo install -d -o root -g tybot -m 0750 \
  "${IMPORT_ROOT}" \
  "${IMPORT_ROOT}/manifest" \
  "${IMPORT_ROOT}/reports"
sudo install -d -o tybot -g tybot -m 0750 "${IMPORT_ROOT}/source"
sudo test ! -e "/var/lib/tybot/archive/workspaces/pf-hermes"
sudo find "${IMPORT_ROOT}" -maxdepth 1 -type d -printf '%M %u:%g %p\n'
```

### 5.3 Git snapshot 반입

Git URL과 key는 서버 셸에서만 입력한다. 아래 placeholder를 그대로 실행하지 않는다.

```bash
set -euo pipefail
SNAPSHOT_ID="20260923-080000-<short-sha>"
IMPORT_ROOT="/var/lib/tybot/imports/pf-hermes/${SNAPSHOT_ID}"
SOURCE_DIR="${IMPORT_ROOT}/source"
EXPECTED_COMMIT="<full-40-character-sha>"

sudo -u tybot test -z "$(find "${SOURCE_DIR}" -mindepth 1 -print -quit)"
sudo -u tybot git clone --no-checkout --filter=blob:none \
  "<PF_ARCHIVE_READ_ONLY_GIT_URL>" "${SOURCE_DIR}"
sudo -u tybot git -C "${SOURCE_DIR}" checkout --detach "${EXPECTED_COMMIT}"
ACTUAL_COMMIT="$(sudo -u tybot git -C "${SOURCE_DIR}" rev-parse HEAD)"
test "${ACTUAL_COMMIT}" = "${EXPECTED_COMMIT}"
```

암호화 묶음이면 hash를 먼저 검사하고 archive entry에 절대경로와 `..`가 없는지 확인한
뒤 새 snapshot 경로에만 푼다.

### 5.4 검증과 불변화

```bash
set -euo pipefail
SNAPSHOT_ID="20260923-080000-<short-sha>"
IMPORT_ROOT="/var/lib/tybot/imports/pf-hermes/${SNAPSHOT_ID}"
SOURCE_DIR="${IMPORT_ROOT}/source"
MANIFEST_DIR="${IMPORT_ROOT}/manifest"

test -z "$(sudo -u tybot git -C "${SOURCE_DIR}" status --porcelain=v1)"
sudo -u tybot git -C "${SOURCE_DIR}" rev-parse HEAD \
  | sudo tee "${MANIFEST_DIR}/git-commit.txt" >/dev/null
sudo -u tybot git -C "${SOURCE_DIR}" ls-files -z \
  | (cd "${SOURCE_DIR}" && xargs -0 -r sha256sum --) \
  | sudo tee "${MANIFEST_DIR}/sha256.txt" >/dev/null
sudo find "${SOURCE_DIR}" -type f | wc -l \
  | sudo tee "${MANIFEST_DIR}/file-count.txt" >/dev/null
sudo chown -R root:tybot "${SOURCE_DIR}" "${MANIFEST_DIR}"
sudo chmod -R a-w "${SOURCE_DIR}" "${MANIFEST_DIR}"
sudo chmod -R go-w "${IMPORT_ROOT}/reports"
```

불일치하면 snapshot을 고치지 않고 새 ID로 다시 받는다.

### 5.5 inventory

보고서에는 본문을 복사하지 않고 다음 수량과 경로만 남긴다.

- `slack-export/channels`, `documents/projects` file 수
- config와 상태 JSON 존재 여부
- channel ID/visibility 없는 file 수
- bot/digest/summary/convo-log 후보 수
- 확정 원문, 변환 첨부, derived, unknown 수
- PII 검사 대기 수

12시 완료 조건은 snapshot, manifest, inventory가 있고 live archive 변경이 0인 것이다.

## 6. 이후 통합 순서

1. **Importer**: PF 형식을 TYBot v2 provenance로 변환하고 derived/unknown을 제외한다.
2. **Shadow QA**: 별도 test archive로 ACL, 출처, 중복, bot-output 제외와 다섯 skill fixture를
   검사한다.
3. **TYBot pilot**: 승인된 제한 workspace에만 반영해 정확도·권한·성능·비용을 본다.
4. **PF rollout**: pilot 승인 뒤 프금팀 workspace를 TYBot에 등록하고 최종 delta를 가져온다.
5. **GCP 결정**: PF 전환이 확인된 뒤에만 기존 Hermes 중지/보존 여부를 별도 승인한다.

## 7. 현재 결함 병행 처리

### 7.1 첨부 상세 DM 중단

요약 검토 DM은 유지하되 변환 성공/실패 상세 DM은 보내지 않는다.

```bash
sudo systemctl disable --now tybot-convert-alert.timer
sudo systemctl reset-failed tybot-convert-alert.service
systemctl is-enabled tybot-convert-alert.timer || true
systemctl is-active tybot-convert-alert.timer || true
```

코드의 `deploy/install.sh` timer 목록에서도 제거해야 다음 배포 때 다시 켜지지 않는다.
변환 실패는 conversion queue와 콘솔 archive diagnostics에만 남긴다.

### 7.2 Canvas 실패 분리

- **수집 Canvas 변환 실패**: 원문 Canvas/내부 첨부 문제. retry queue와 콘솔에
  `workspace/channel/file/stage/error_code`를 남긴다.
- **검토 Canvas 생성/권한 실패**: 표시 방식 문제. 같은 후보를 DM으로 fallback하고
  `canvas_fallback`과 오류 코드를 남긴다.

둘을 `canvas_failed` 하나로 합치지 않는다. Canvas 생성 실패 때문에 요약 검토 DM이
사라져서는 안 된다.

### 7.3 검토 DM 미발송 진단

```bash
sudo systemctl status tybot-review-dm.timer tybot-review-dm.service --no-pager
sudo journalctl -u tybot-review-dm.service --since today --no-pager
sudo -u tybot env TYBOT_ENV_FILE=/etc/tybot/tybot.env \
  /opt/tybot/.venv/bin/python /opt/tybot/scripts/diagnose_summary_review.py --days 30
```

콘솔 즉시 발송 결과에서 채널별 outcome을 확인한다.

| outcome | 확인할 것 |
|---|---|
| `no-reviewer` | 활성 검토자와 Slack user ID |
| `no-new-source` | watermark 이후 사람 원문 |
| `no-accepted-candidate` | 원문 대조/후보 filter |
| `already-sent` | 기존 delivery와 재발송 의도 |
| `no-client` | workspace token |
| `dm-failed` | Slack API error와 DM 허용 여부 |

`canvas_fallback>0`인데 `sent=0`이면 Canvas가 아니라 검토자/Slack DM 경로를 조사한다.

## 8. 승인 기준

- GCP Hermes는 계속 정상 운영한다.
- PF snapshot은 격리 경로에서 불변이고 commit/hash가 검증됐다.
- live TYBot archive는 승인 전 변경되지 않았다.
- importer는 source/derived/unknown과 ACL을 fail-closed로 구분한다.
- TYBot에서 기능·권한·성능 QA를 먼저 통과한다.
- 첨부 상세 DM은 없고 요약 검토 DM과 Canvas fallback은 동작한다.
- 프금팀 적용은 별도 go/no-go와 최종 delta 뒤 진행한다.
