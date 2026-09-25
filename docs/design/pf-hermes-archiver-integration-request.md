# PF Hermes 개발자 수정 요청: Archiving 분리와 TYBot 직접 호출

작성: 2026-09-23 PF·IT 협의
독자: PF Hermes 개발자와 코드 수정 AI 에이전트
상위 결정: [Archiving Bot 분리와 Hermes 직접 호출](archiving-bot-separation-2026-09-23.md)

## 한눈에 보는 요청

한 달가량 PF의 Hermes·Clio 직접 호출과 TYBot 호출을 병행한다. **PF GCP
Hermes의 현재 동작을 중단하지 말고**, 같은 Hermes 원본 소스가 TY DMZ에서
전문 봇으로 실행되도록 모드를 분리해 달라. TYBot은 PF Hermes 기능을
부분 복사한 Python fork를 계속 수정하지 않는다.

| 작업 | PF Hermes에 남김 | Archiving Bot으로 넘김 |
|---|---|---|
| 검색·추가 읽기·근거 대조·Q&A | 예 | 아니오 |
| 일일/주간 요약, 후보 생성, Slack 검토·정정·승인 | 예 | 아니오 |
| Slack 원문/스레드/Canvas 수집, edit/delete, checkpoint | 아니오(전환 후) | 예 |
| 파일 다운로드·변환·PII·원본/변환본 보존 | 아니오(전환 후) | 예 |
| Git 자료 writer·백필·변환 재시도 | 아니오(전환 후) | 예 |
| Slack 질문 전송·사용자 권한·전문가 선택 | PF 현행 모드에서는 유지, TY 모드에서는 하지 않음 | 아니오 |

## 1. 구현 전 읽을 코드

- `src/index.js`: 직접 Slack 질문 진입점, `startScheduler()`
- `src/scheduler.js`: `ingest`, `ingestPre`, `health`, `healthPre`,
  `daily`, `weekly`. **health의 `sync:true`도 수집 경로**다.
- `src/ingest/**`: Slack 읽기, Git writer, 동기화 상태, derived index
- `src/slack/question.js`, `src/llm/{qa,tools,prompts}.js`,
  `src/{archive,documents,config}.js`: 답변/검색과 자료 읽기
- `.claude/skills/{archive-run,slack-sync,doc-archive,archive-inbox,issue-trim,hermes-install}/SKILL.md`

## 2. 모드 분리: 첫 PR

현재 GCP 동작을 기본값으로 유지한다. 명시적 `standalone` 모드는
기존 Slack 질문·수집·Git·요약 일정을 그대로 실행한다. `specialist`
모드는 **Slack Socket Mode를 시작하지 않고**, 수집·Git writer·파일 변환·
기존 cron을 기동하지 않는다. 대신 TYBot의 로컬 요청만 받아 검색·
답변한다. 리뷰 스케줄러는 Archiving 자료 계약이 준비된 뒤 별도
소유권 flag로 켜며, 중복 발송을 막는다.

모드 판정은 fail-closed여야 한다. 알 수 없는 값, 필요한 archive reader/
tool broker나 서명 자격증명 누락이면 기동 실패한다. 기존
`config.json`·개인 GCP 환경변수를 TYBot의 `/etc/tybot/tybot.env`에
합치지 않는다. TY DMZ Hermes는 독립된 env/credential, OS 사용자,
로그·상태 디렉터리를 쓴다. GCP 서비스와 Slack token은 유지한다.
동일 token으로 Socket Mode 두 개를 띄우지 않는다.

수집 코드를 당장 삭제하지 않는다. mode 시험 후 GCP PF도 새 Archiving
Bot에 완전히 의존하게 된 마지막 전환에서 제거한다. 모드만 끄고
`runHealth(sync:true)`나 CLI `ingest`가 남는지 자동 검사한다.

## 3. TYBot 호출 계약: 두 번째 PR

Node 22 자체는 TYBot 전문 봇 빌드 허용 목록에 있다
(`src/tybot/console/specialist_source.py`,
`deploy/tybot-specialist-build`). 그러나 현행 v2 요청은 **선택된 근거
텍스트만 전달하고 검색 도구가 없다.** 그 계약으로 PF Hermes의
검색 기능을 보존했다고 처리하면 안 된다. TY팀과 별도 버전의 호출·
도구 계약을 고정한 뒤 구현한다.

필수 입출력:

1. TYBot이 request ID, workspace, actor의 권한 판정 ID, 질문,
   후속 질문의 **원문 좌표**, 시간/비용 예산을 전달한다.
2. Hermes가 `search`, `read`, `recent`, `file` 도구로 추가
   근거를 요청한다. 도구는 TYBot/Archive 측이 현재 ACL을 확인한 뒤
   제한된 본문과 opaque evidence ID만 돌려준다. Hermes에게 archive
   전체 경로·Slack bot token·DB 자격을 주지 않는다.
3. Hermes는 답변, 사용한 evidence ID, 누락/불확실/실패 상태, 사용량을
   반환한다. 근거가 없으면 추측으로 보충하지 않는다.
4. TYBot이 ID와 권한을 재검증하고 한 번만 전송한다. 같은 요청을
   PF 직접 호출 bot이 중복 응답하지 않아야 한다.

내부 Unix socket 또는 동등한 로컬 IPC를 사용하며 인터넷 인바운드
포트를 추가하지 않는다. 요청·응답 schema, deadline, 서명/재생 방지,
도구 호출 상한, 응답 크기와 취소 전파를 fixture로 고정한다.
`nodejs22` 빌드 성공만으로 이 계약이 끝난 것은 아니다.
LLM 호출은 회사가 승인한 게이트웨이/egress 정책에 맞춘다.

## 4. 검토·요약: 세 번째 PR

PF의 `archive-inbox`는 skill을 쓰는 운영 절차이며 현재 archive에는
TYBot처럼 **Slack DM으로 승인된 요약 기록이 없다**. 기능을 Hermes
서비스 코드로 옮기되 Archiving Bot에는 사람 검토 기능을 넣지 않는다.

1. Archiving Bot이 확정한 채널별 **raw 사람 메시지와 첨부 출처**만
   입력으로 받아 후보 요약을 생성한다. 다른 채널·봇 답변·미검증
   파일 본문이 섞이면 실패시킨다.
2. 후보를 Canvas에 출처 링크·숫자·날짜·금액 단위와 함께 제시한다.
   지정된 검토자에게 DM으로 Canvas 링크와 `맞음` / `틀림` 버튼을 보낸다.
   파일 변환 목록 전체를 DM에 덤프하지 않는다.
3. `틀림`은 DM에서 자유 텍스트 정정사항을 받는다. Hermes가 원문
   좌표와 대조해 수정 후보를 만들고 **다시 확인**을 받는다.
4. 만료/무응답은 폐기하며 승인으로 간주하지 않는다. 승인본만 raw
   밖의 파생물로 저장하고 근거 원문 좌표·승인자·시각·버전·hash를
   남긴다. 승인 후에도 사용자 질의 때는 현재 ACL로 다시 검증한다.
5. 중복 이벤트/재시작/검토자 변경 시 DM 중복 발송이나 타인 승인
   처리가 없어야 한다. 수집과 검토의 타이밍이 어긋나면
   `pending`으로 남기고 없는 내용을 만들지 않는다.

`issue-trim`은 채널 머리말을 고치는 수집 동작이 아니라 **파생
요약 편집**으로 취급한다. `archive-run`·`slack-sync`·
`doc-archive`의 원문/파일 writer는 Archiving Bot으로 이전한다.
`hermes-install`의 GCP 운영 절차는 TY DMZ 런타임 지침과 분리한다.

## 5. 검증 및 인계

각 PR은 PF GCP 현행 회귀와 TY specialist 모드 offline 시험을 모두
통과해야 한다. 최소 fixture:

- 모드 누락/오류 시 기동 실패, standalone 현행 cron 유지,
  specialist에서 Slack 연결·ingest/Git/변환 실행 0회
- 질문당 검색→추가 읽기 2회 이상, private ACL 차단,
  후속 질문 원문 좌표 재검증, timeout/도구 실패/출처 누락
- Canvas 후보의 수치·출처 검토, 맞음/틀림/정정/재확인,
  무응답 폐기, 중복 DM 억제
- PF GCP와 TYBot 동시 운영 때 같은 질문의 사용자 답변 1건

제출물은 고정 commit/tag, 시험 명령·결과, standalone/specialist
기동법, env 변수 이름만 적은 예시, 요청/응답 fixture, 알려진 차이,
되돌리기 절차다. 실제 archive 원문, Slack token, API key, 개인
GCP 자격증명을 PR에 넣지 않는다. PF GCP 배포와 TY DMZ 배포는
각각 소유자 승인 후 별도로 한다.
