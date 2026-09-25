# Archiving Bot 분리와 Hermes 직접 호출 전환

결정일: 2026-09-23 PF·IT 협의
상태: 합의한 목표 구조. 구현·운영 전환은 아래 관문별로 진행한다.
PF 개발자 작업: [Hermes 수정 요청](pf-hermes-archiver-integration-request.md)
오너 확인: 각 워크스페이스에 Archiving Bot **별도 Slack 앱 설치 가능**.

현재 구현: `tybot.archiving_bot`과
`deploy/tybot-archiving-shadow@.service`는 **채널 메시지 그림자 수집 전용**
초기 실행본이다. edit/delete revision 보존과 첨부 독립 저장은 구현됐지만,
revision 최신 상태와 삭제를 검색에 반영하는 reader, Canvas, Hermes 검토/호출
전환은 아직 구현되지 않았다. 현재 수집·변환·검토 서비스를 끄거나 이 그림자
archive를 운영 검색에 연결하면 안 된다.

## 1. 이번 합의가 바꾼 것

이전의 PF/TY 단계별 통합 계획은
**수집 비교를 먼저 하고 답변 이관은 나중**이라는 가정이었다. 이 문서가 새
우선순위다. 약 한 달 동안 PF의 Hermes·Clio 직접 호출과 TYBot Master 호출을
병행 유지한다. 먼저 **TYBot → 수정된 PF 원본 Hermes** 호출을 안정화하고,
이후 PF에도 TYBot 진입점을 적용한다. PF archive의 DMZ 복제는 오늘 개발용
격리 snapshot이며, 운영 자료 공급원을 바꾸지 않는다.

| 책임 | 목표 소유자 | 그 외 서비스에서 제거할 것 |
|---|---|---|
| Slack 채널 원문 수집, 첨부 다운로드·변환, provenance, 원본 저장 | Archiving Bot | TYBot Master·PF Hermes의 writer |
| 원문 기반 검토 후보·정정·승인/만료, 정기 요약, 검색·읽기·Q&A | Hermes | Archiving Bot의 사람 확인 |
| 사용자 신원·권한 범위, 전문가 선택, 요청/응답 전달·감사 | TYBot Master | 원문/첨부 writer·변환·검토 내용 작성 |
| 보고서 작성 | Clio | 이번 변경 없음 |

TYBot Master가 **요청자 ACL과 전달 주체**를 계속 확인하는 것은 라우팅의
보안 경계다. 도메인 판단·검색·요약을 마스터가 대신하지 않는다.

## 2. 확인된 코드와 가장 큰 간극

- TYBot 실시간 수집은 `src/tybot/slack/pilot.py`의 `_ingest_live`,
  `_ingest_dm`, `_ingest_channel`에 있고, 백필은 `tybot.collect`,
  재변환은 별도 timer에 있다. 수집을 즉시 끄면 DM 업로드가 **이번 질문의 근거**
  로 들어가던 경로도 끊긴다.
- 현재 TYBot `archive/files.py`는 원본을 archive 밖 `objects/`에,
  추출본을 `staging/**/extracted.md`에 따로 저장하지만 **추출 본문을
  raw 채팅 MD에도 복제한다.** 별도 첨부 정본으로 바꾸려면 ArchiveStore,
  검색 색인, 출처 표시, 재처리와 요약 검토의 reader도 함께 바꿔야 한다.
- PF Hermes의 `src/ingest/**`와 `doc-archive` skill은 수집·Git 쓰기,
  변환과 요약 파생물을 다룬다. 삭제 전에 질의응답·정기 요약이 읽는 자료
  경로를 새 Archiving Bot의 읽기 계약으로 바꿔야 한다.
- TYBot 전문 봇 인프라는 `nodejs22` 빌드를 허용한다. 다만 현행
  `specialist_wire.py` v2는 사전에 고른 근거만 보내고, 15초/3,000자
  상한이며, 런타임에는 archive mount나 검색 도구가 없다. PF Hermes의
  다단계 검색·읽기를 **그대로** 수행하기 위한 호출 계약은 아직 없다.
- 별도 Slack 앱은 TYBot·Hermes 앱의 개인 DM을 읽을 수 없고, **일반 사용자
  간 DM은 수집 대상이 아니다.** 봇과 사용자의 대화는 답변 근거 raw에 섞지
  않고 별도 감사 기록으로 보존한다. PF GCP Hermes의 과거 대화 기록은 고정
  snapshot을 변환해 이관하며, 신규 Archiving 앱에 TYBot/Hermes 토큰을 주지
  않는다. 같은 bot token으로 Socket Mode를 두 인스턴스 띄우지 않는다.

## 3. 저장 구조 권고

`workspace/이름/채널이름/raw`은 이름 변경·동명 채널·다중 워크스페이스에서
신원을 잃으므로 사용하지 않는다. 이미 쓰는 v2 원문 경로의 **stable ID**를
유지한다.

```text
/var/lib/tybot/archive/workspaces/<workspace-key>/
  channels/<channel-id>__<display-slug>/
    raw/YYYY-MM-DD.md             # 사람 Slack 채팅 원문만, 첨부는 ID·링크 참조
    attachments/<file-id>/<revision>.md  # 변환된 첨부 본문과 출처 메타
    canvases/<canvas-id>/<revision>.md   # 사람이 만든 Canvas 원문
  bot-conversations/...            # 봇 대화 감사 기록; 검색 근거 raw와 분리
/var/lib/tybot/objects/workspaces/<workspace-key>/channels/<channel-id>/attachments/<file-id>/
  <original-sha256>               # 원본 바이너리; 검색 트리 바깥
/var/lib/tybot/imports/pf-hermes/<snapshot-sha>/source/
  ...                             # PF 개발용 복제; 운영 검색에서 제외
```

디렉터리 slug는 표시용이며 identity는 workspace ID + Slack channel/file ID다.
개명해도 경로를 새 정본으로 만들지 않는다. 첨부 본문은 **raw에 중복 저장하지
않고** 독립 문서로 인덱싱한다. raw의 `file_id`·원본 메시지 `ts` 참조와
첨부의 source metadata가 같은 좌표를 가리켜야 한다. 정보가 없으면 임의
채널에 붙이지 않는다. PII 판정·private ACL·부분 변환 상태를 첨부별로
보존한다. 기존 raw에 박힌 첨부 추출 줄은 소급 삭제하지 않고 legacy
reader로 유지하며, 신규 reader에서 같은 file ID를 두 번 근거로 세지 않는다.

## 4. 서비스 경계와 전환 순서

### A. 오늘: 개발용 기준선

1. PF Git full SHA를 고정하고 암호화된 승인 경로로 DMZ의
   `/var/lib/tybot/imports/pf-hermes/<snapshot-sha>/source`에 복제한다.
2. 파일별 hash, 자료 종류, 채널 ID·visibility·미확정, 파일 ID 누락을
   보고한다. snapshot은 운영 ArchiveStore 경로에 두지 않는다.
3. Archiving Bot의 독립 프로세스와 별도 자격증명·workspace 설정,
   idempotent writer, 첨부 정본 구조의 테스트를 먼저 만든다.
4. PF GCP Hermes와 TYBot의 현행 수집·답변·검토 timer는 **건드리지 않는다**.

### B. 먼저: TYBot → PF 원본 Hermes 호출

PF 개발자는 수집을 끌 수 있는 **명시적 모드**를 만들되 GCP 운영의 기본값은
현행으로 둔다. Node Hermes의 검색·답변 엔진은 TY DMZ에서 별도 프로세스로
기동한다. TYBot은 요청자·허용 범위·질문을 보내고, Hermes는 권한이 제한된
archive read tool로 직접 검색·추가 읽기·요약·답변을 수행한다. Master는
출처 ID와 권한을 재검증하고 Slack에 한 번만 전송한다.

현행 v2 evidence-only endpoint에 PF 엔진을 억지로 맞추면 자체 검색이
사라진다. **버전이 분리된 v3 호출·도구 계약**을 정의한다. 최소 계약은
request ID, actor/workspace, 권한 스냅샷 ID, deadline/예산, follow-up 원문
좌표, 도구 호출 횟수 상한, `search/read/recent/file` 결과의 opaque ID,
답변·출처·상태·비용이다. Hermes가 파일 경로나 ACL을 추측해 직접 열지
않으며, tool broker는 매번 현재 권한을 확인한다. 별도 인터넷 인바운드는
열지 않고 같은 서버의 Unix socket 또는 동등한 로컬 IPC를 쓴다.

Node 지원은 **기반 코드가 있다는 의미**이지 통합 완료가 아니다.
`nodejs22` 빌드·서비스 health·HMAC·방화벽/LLM egress·읽기 도구·긴
답변 timeout·콘솔 release/rollback을 합성 fixture와 실측으로 통과해야 한다.
PF source를 TYBot 내부 Python Hermes로 부분 복사하거나 그 fork를 계속
수정하지 않는다.

### C. 이후: Archiving Bot을 채널별로 인수

새 앱의 채널 접근·속도 제한·PII·첨부 품질·스레드·edit/delete·Canvas를
그림자 모드로 비교한다. 첨부별 **수집 완료 ack**를 시험한다.
ack 전에는 방금 올린 파일을 이미 검색 가능하다고 말하지 않는다.
채널별 누락 0·권한 유출 0·첨부 숫자 손실 0을 확인한 뒤 writer 소유권을
인수한다. PF 쪽 direct-call 서비스가 남아 있는 동안 그 자료 공급도
확인하지 않고 PF writer를 먼저 끄지 않는다.

### D. 마지막: 검토·PF 진입점 전환

Hermes가 새 원문에서 검토 후보를 만들고 Slack DM/Canvas로 출처·숫자
확인 → 정정 텍스트 → 수정 후보 재확인 → 승인 또는 만료 폐기를 담당한다.
Archiving Bot은 검토 DM을 보내지 않는다. 승인 요약은 raw가 아닌 별도
파생물이며 답변 근거로 사용할 때 원문 좌표와 현재 ACL을 다시 확인한다.
TY workspace pilot이 통과한 뒤 PF 채널별 답변 전달자를 TYBot으로 바꾸고
PF GCP writer/답변을 각각 drain한다. Clio는 별도 검증 전까지 그대로 둔다.

## 5. 웹 콘솔에서 확인할 것

기존 전문 봇 화면의 release, image digest, health, 호출 성공률, 지연,
도구 실패, 근거 수, 비용, rollback을 Hermes **실행본**에 연결한다.
현재 화면에 등록 가능하다는 것과 실제 Node Hermes를 호출한다는 것은
다르다. 직접 호출 시험은 synthetic → TY 제한 채널 → PF pilot 순으로
열고, 화면에 실행 SHA·계약 버전·자료 세대·마지막 성공 시각을 함께 보인다.
Archiving Bot은 별도의 수집/변환/누락 지표를 갖고, Hermes의 review DM
상태와 섞지 않는다. 외부에서 접근 가능한 새 포트는 열지 않는다.

## 6. 중지 조건

- Archiving Bot용 별도 Slack 앱·자격증명이 없다.
- 새 첨부 reader가 없어 별도 본문이 답변에서 사라진다.
- PF Hermes 수집과 답변을 독립적으로 제어할 수 없다.
- v3 tool broker가 ACL·출처·후속 질문·timeout을 보장하지 않는다.
- 같은 요청에 PF bot과 TYBot이 둘 다 답하거나 검토 DM을 중복 발송한다.

위 조건이 남아 있으면 **현재 수집 경로를 끄지 않는다.** 단순한
`REALTIME_INGEST=0`만으로 이관 완료라고 판단하지 않는다.
