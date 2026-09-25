# PF Hermes 개발자 작업지시서

> **2026-09-23 협의 이후:** 이 문서에서 조사한 source snapshot 폴더명은 내부 식별자일
> 뿐 PF 제품의 공식 버전명이 아니다. P0 snapshot 인계 계약은 개발용으로 유지하되,
> 수집/변환 분리와 TYBot 직접 호출 구현은
> [새 PF Hermes 수정 요청](pf-hermes-archiver-integration-request.md)을 따른다.

작성: 2026-09-23
대상: PF Hermes 개발자와 해당 저장소를 수정할 AI 에이전트
상위 계획: [PF Hermes와 TYBot 단계별 통합 계획](pf-tybot-phased-integration-2026-09-23.md)

## 먼저 읽을 결론

PF의 GCP Hermes와 Clio는 당분간 계속 운영한다. 이번 작업은 PF Hermes 전체를
TY DMZ로 옮기거나 PF Git을 즉시 중단하는 작업이 아니다. PF 개발자는 **기존
자료의 의미를 보존한 인계 계약과 병행 수집 비교 좌표**를 제공한다. TY 개발자는
이를 이용해 TYBot 수집 worker, 파일 worker와 답변 specialist를 단계별로 통합한다.

| 순서 | PF 개발자가 제출할 것 | 지금 운영 변경 |
|---|---|---|
| P0: 기준선 | 읽기 전용 snapshot manifest, 채널 지도, 미확정 목록 | 없음 |
| P1: 대화 비교 | 신규 수집분의 Slack 좌표 sidecar, 실패·누락 상태 | 승인 전 배포 없음 |
| P2: 첨부 비교 | 원본 file ID·채널·링크·변환 상태 대응표 | 승인 전 배포 없음 |
| P3: 답변 인수 | 현행 검색/답변의 평가 입력·출력·출처 계약 | G4 이후 |
| P4: PF 전환 | 수집/답변 분리, 중지·delta·복귀 절차 | G5 이후 |

**AI 에이전트의 첫 구현 범위는 P0의 offline exporter와 fixture다.** P1/P2의
운영 Hermes 수정은 코드를 별도 변경으로 준비하되, PF 오너 승인 없이 GCP에
배포하거나 live archive를 재작성하지 않는다. P3/P4를 P0 PR에 섞지 않는다.

## 두 시스템의 차이와 담당자

| 항목 | PF 현재 | TYBot 목표 | PF가 해야 할 일 |
|---|---|---|---|
| Slack 진입점 | Hermes가 직접 수집·답변 | Master가 수집·라우팅, worker가 저장 | 수집과 답변을 별도 제어할 지점 확인 |
| 자료 | 개인 Git의 채널별 MD, 문서별 MD | TY DMZ의 workspace/channel ID별 원문 | 자료 종류·출처·상태 manifest 제출 |
| 채널 권한 | 이름·`privateChannels`·개명 지도 | ID·현재 ACL, 기본 비공개 | 채널 ID/visibility/state 증거 제출 |
| 메시지 좌표 | 사람 MD는 표시 시각만 보존 | Slack `ts`로 중복 제거 | 신규 수집 때 원본 좌표 sidecar 제공 |
| 첨부 | `documents/projects/`와 승인/제외 | 원본 file ID·채널·변환/PII 상태 | provenance 누락 목록 제출 |
| 검토/요약 | Claude skill과 Git 점검 | 원문 밖 검토 DM·승인된 파생물 | 승인으로 오인될 메타 분리 |
| 답변 | PF Hermes가 자체 검색·전송 | TYBot Master가 Hermes specialist 선택 | G4 평가셋·검색 의미 제공 |
| Clio | GCP에서 별도 운영 | 이번 단계에서 유지 | 호출·정기 작업 영향만 보고 |

PF 코드를 TYBot Master에 통째로 이식하지 않는다. PF의 수집 불변조건은 TYBot
worker에, 검색·읽기·답변 기능은 Hermes specialist에 들어간다. Slack 권한 판정,
라우팅, 비용 제한과 최종 전송은 TYBot Master가 소유한다.

## 확인된 PF source snapshot 특성

실물과 근거는 `subbots/hermes_v1.3/docs/260922_TYBot-Hermes-통합_P2-자료출처조사_결과.md`
및 `hermes-main/src/ingest/archive-format.js`를 따른다.

- 채널 MD 43개와 변환 문서 MD 507개가 조사됐다. 채널 범위는 최소 54개이며
  ID 46개 확인, 8개 미확정이다. 숫자는 인계 시점에 다시 측정한다.
- 채널 MD 한 파일에는 사람 발언, 요약 머리말, 봇 자리표시가 섞인다.
  **파일 하나를 `slack_raw` 한 건으로 분류하면 안 된다.**
- 사람 메시지와 스레드 답글의 원본 Slack `ts`는 표시 시각으로 렌더링된다.
  기존 MD만으로 모든 메시지 좌표를 복원할 수 없다. 이름·시각·본문이 비슷하다고
  과거 TY 자료와 자동 일치시키지 않는다.
- `_승인자료`는 실제 채널이 아닌 가상 project다. 채널 ID 또는 공개 허용을
  폴더명에서 추정하지 않는다. 출처 채널과 공개 승인 증거가 없으면 제외한다.
- 기존 PF 자료에는 TYBot 검토 DM과 같은 `approved_summary` 절차가 없다.
  사람이 정한 채널 요약도 근거 `ts`와 승인 hash가 없다면 원문이나 승인 요약으로
  만들지 않는다.
- 스킬은 `archive-run`, `slack-sync`, `doc-archive`, `archive-inbox`,
  `hermes-install`, `issue-trim` 여섯 개다. 스킬 자체를 TYBot에 복사하지 않는다.

## P0. 읽기 전용 snapshot 인계

먼저 PF 코드 저장소에서 `src/config.js`, `src/ingest/archive-format.js`,
`slack-archive.js`, `summary.js`, `pending-work.js`,
`src/documents/{parse,access}.js`와 위 여섯 `SKILL.md`를 읽는다.
운영 설정을 module import만으로 읽거나 종료하는 파일은 exporter에서 직접
import하지 말고 인자가 명시된 pure classifier로 분리한다.

PF 저장소에 다음을 추가한다. 기존 `package.json` script는 유지한다.

```text
scripts/export-tybot-handoff.js
scripts/check-tybot-handoff.js
schemas/tybot-handoff-manifest.schema.json
docs/tybot-handoff.md
test-fixtures/tybot-handoff/
```

새 script: `handoff:inspect`, `check:handoff`. CLI는 명시적 절대경로
`--data-root`, `--channel-map`, `--out`, `--workspace-id`를 받는다.
기본은 읽기 전용이며 Slack·Anthropic·Git remote 호출, commit/push, 원본
자료 수정은 금지한다. 출력은 archive **밖의** 지정 경로에 원자적으로 쓴다.
실패한 부분 파일을 완성 manifest처럼 보이게 남기지 않는다.

### 채널 지도

`channel-map.json`의 각 행은 `workspace_id`, `channel_id`,
`current_name`, `source_names`, `visibility`
(`public/private/unknown`), `state` (`active/archived/deleted/unknown`),
확인 근거와 확인 시각을 갖는다. ID가 없거나 이름이 중복·변경되어 모호하면
`unknown`으로 남긴다. private/unknown은 Slack에서 현재 접근과 공개 승인을
확인하기 전까지 공개 자료로 분류하지 않는다.

### manifest 계약

`manifest.json`에는 schema 버전, PF 코드 full SHA, 자료 Git full SHA, 생성
시각, workspace ID, 상태별 건수와 entry 목록을 기록한다. entry는
`source_path`, source file SHA-256, **블록/행 범위**, `kind`,
`include_candidate`, 채널 ID·visibility·state, 확인 가능한 경우에만
`message_ts/thread_ts/file_id/source_permalink`, `reason_codes`를 갖는다.
채널 MD는 블록별로 분류하며 snapshot SHA로 행 좌표를 고정한다.

허용 `kind`: `slack_raw`, `document_converted`, `canvas_source`,
`approved_summary`, `ai_summary`, `bot_output`, `digest`, `index`,
`conversation_log`, `runtime_state`, `binary_or_temp`, `unknown`.
`approved_summary`는 schema의 예약 값일 뿐, 현재 PF 자료에 있다고 가정하지
않는다. `unknown` 또는 ID·권한·출처가 불명확한 항목은
`include_candidate=false`다. TYBot importer가 별도 PII·ACL·출처
검사를 하기 전까지 true도 운영 반영을 뜻하지 않는다.

최소 reason code: `missing-channel-id`, `ambiguous-channel`,
`unknown-visibility`, `archived-or-deleted-channel`, `missing-message-ts`,
`missing-file-id`, `missing-source-link`, `bot-authored`,
`derived-summary`, `partial-conversion`, `unclassified`.
오류·미확인·0건을 같은 값으로 합치지 않는다. manifest에는 원문 본문,
사람 이름, token을 넣지 않는다. 경로·채널명 자체도 업무 정보이므로 산출물은
제한된 전달 경로로만 공유한다.

## P1. 병행 대화 수집을 비교할 좌표

P0 manifest로 **과거 메시지 전수 대조가 가능하다고 주장하지 않는다.**
신규 병행 수집 구간에는 PF Hermes가 Slack에서 읽은 이벤트의 read-only
sidecar를 만든다. 형식은 NDJSON 등 결정적이고 검증 가능한 구조로 정한다.
각 record에는 `workspace_id`, `channel_id`, 원본 `message_ts`,
`thread_ts`, event/revision 종류, 사람/봇 구분, 첨부 file ID 목록,
읽기 상태, 처리 결과와 수집 회차 ID가 있어야 한다. 본문은 sidecar에 복제하지
않는다. 비교용 본문 hash가 필요하면 접근 제한된 TY/PF 비교 환경에서만 계산한다.

PF 개발자는 `slack-archive.js`의 수집 성공·실패·스레드 미열람·편집·삭제,
`pending-work.js`의 재시도, `archive-run`의 lock/부분 성공 상태를 mapping한다.
sidecar 쓰기 실패가 운영 PF 원문이나 답변을 손상시키지 않도록 설계하고,
누락을 성공으로 숨기지 않는다. 변경은 fixture와 offline 검사까지 준비한 뒤
PF 오너가 배포를 승인해야 한다. 같은 Slack bot token으로 두 Socket Mode
인스턴스를 동시에 띄우지 않는다.

## P2. 첨부·Canvas provenance

PF `doc-archive`가 만든 각 문서에 대해 Slack `file_id`, source channel ID,
원본 메시지 `ts`·링크, 변환 상태(완료/부분/실패/제외), 페이지·시트·표 상태,
권한/공개 승인 증거를 mapping한다. 없는 ID를 파일명으로 추측하지 않는다.
PF Git에는 원본 바이너리가 없을 수 있으므로 MD만 전달하고 다운로드까지
성공했다고 기록하지 않는다. `_승인자료`의 출처 채널은 별도로 확인한다.
Canvas를 PF가 수집하지 않았다면 `미지원`으로 보고하며 가짜 fixture를
운영 결과로 제출하지 않는다.

## P3/P4. 뒤 단계의 PF 책임

P3에서는 현재 검색·답변의 도구 결과, 프롬프트 입력 출처, 캐시 무효화,
스레드 맥락, 비용과 상태 코드를 20문항 평가셋으로 비교할 수 있게 제공한다.
G4 이전에는 GCP Hermes 답변을 끄지 않는다.

P4에서는 PF 답변 전송과 Git writer를 **각각** 중지할 수 있는 방법,
마지막 watermark, 진행 중 작업 drain, 늦은 쓰기 차단, 증분 재대사,
복귀 절차를 제출한다. TYBot 답변 pilot이 통과하기 전에는 PF writer를
멈추지 않는다. DMZ 원문을 개인 GCP로 역반출하는 호환 뷰는 기본안이 아니다.
Clio는 이 단계에서도 별도 서비스다.

## 검사와 제출

AI 에이전트는 별도 작업 branch에서 변경 전 `git status --short`를 확인하고
기존 변경을 보존한다. 토큰이 없는 synthetic fixture로 다음을 실행한다.

```bash
npm ci
npm run check:offline
npm run check:handoff
npm run handoff:inspect -- \
  --data-root "$PWD/test-fixtures/tybot-handoff" \
  --workspace-id TTEST0001 \
  --channel-map "$PWD/test-fixtures/tybot-handoff/channel-map.json" \
  --out "$PWD/.tmp/tybot-handoff"
git diff --check
```

fixture는 여섯 skill의 핵심 사례를 포함한다: 부분/0건/실패, 사람과 봇,
rename·edit·delete·미열람 스레드, 누락 file ID, private·`_승인자료`,
승인 근거 없는 요약, `issue-trim` 파생 머리말, 미지원 Canvas.
원본 fixture의 SHA와 mtime은 전후 같아야 한다. 같은 입력의 entry 순서와
hash는 같아야 하며 생성 시각만 달라질 수 있다. 네트워크·시크릿 없이
검사가 통과해야 한다.

P0 제출물: 변경 파일·full commit SHA, 검사 결과, manifest/schema,
channel map의 확인/미확정 수, 자료 Git snapshot full SHA와 시각,
알려진 미확정·부분 실패 목록. P1/P2는 별도 PR과 별도 검사 결과로 제출한다.
실제 archive 원문, `.env`, token, deploy key, 실사용자의 발언을 코드
저장소나 보고서에 넣지 않는다.

완료 기준은 **PF 운영 변화 0, 읽기 전용 P0 검증 통과, 미확정 자료 fail-closed,
혼합 MD 블록 분류, 이후 신규 수집분의 원본 Slack 좌표 확보 계획**이다.
PF 측이 자료와 계약을 제출해도 TYBot 운영 archive의 반입·권한 검증·PF
진입점 전환은 TY 팀과 오너의 별도 관문 결정이다.
