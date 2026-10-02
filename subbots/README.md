# TYBot 전문 봇 연동 경계

이 디렉터리는 마스터 TYBot이 전문 봇을 호출하기 전에 검증할 **계약과 연동
메타데이터**를 추적한다. 기본값은 그대로다 — 전문 봇 소스는 여기 두지 않는다.

**예외는 `hermes/` 하나다**(2026-10-02). 우리가 소유하고 이 저장소에서 직접 고치는
구현이라 소스가 같이 들어와 있다. 예외를 둔 자리에는 **경계를 따로 세운다** — §구현을
두는 경우.

## 구조

```text
subbots/
  README.md
  hermes/                       # 예외: 우리가 소유한 구현 (소스 포함)
    tybot-specialist.toml       # 연동 메타데이터 — 실행 코드가 아니라 선언
    contract/
      prompt.md                 # 답변 규칙. 이 파일이 곧 계약 본문이다
      summary-review.md         # 요약 검토 규칙 (summary_review.py 가 읽는다)
    src/ · scripts/ · .claude/skills/   # 구현. **마스터는 실행하지 않는다**
  <다른 전문 봇>/               # 기본값: 계약과 연동 메타데이터만
    tybot-specialist.toml
    contract/
      prompt.md
```

`prompt.md` 가 있어야 **배포된 것**이다(`specialist_adapters.available_keys`).
디렉터리만 만들고 계약을 안 넣으면 콘솔에는 「미배포」 로 보인다 — 「등록했는데
답을 못 한다」 보다 낫다.

`src/tybot/specialist_prompts/` 도 아직 읽는다. 그 경로는 패키지 안이라 어떤
설치 형태에서도 따라오므로 마지막 보루로 남긴다. **같은 key 를 두 곳에 두지
않는다** — 어느 쪽이 도는지 아무도 모르게 되고, 고친 쪽이 안 도는 상태가 조용히
생긴다(`tests/test_specialist_adapters.py` 가 막는다).

## 저장하는 것

- 버전이 명시된 입출력 계약
- 계약 검사에 쓰는 비민감 테스트 자료
- 코드에 등록된 어댑터를 가리키는 연동 메타데이터
- 전문 봇이 반환할 수 있는 오류 코드와 폴백 조건
- **우리가 소유한 전문 봇의 구현**(§구현을 두는 경우의 조건을 지킬 때)

## 저장하지 않는 것

- 다른 팀이 소유한 전문 봇의 소스, 또는 중첩 `.git` 디렉터리
- 마스터가 실행할 외부 실행 파일 경로와 임의 외부 URL
- 토큰, API 키, **운영 원문**, 질문·답변 본문
- `ref/` 아래의 검토용 프로젝트 사본

운영 원문이 들어오는 경로는 `.gitignore` 로 막는다 — `subbots/pf-hermes-archive/`,
`subbots/pf-hermes-source/`, `subbots/hermes_v*/`. 이 목록은 **사람 기억이 아니라
파일로** 유지한다.

## 구현을 두는 경우 — 지켜야 하는 넷

소스가 들어오면 「마스터가 안 부른다」 는 사실만으로는 부족하다. **부를 수 있는
상태**가 곧 위험이다. `npm run ingest` 한 줄이면 원문이 써지고, `.claude/skills/`
아래 스크립트는 사람이 명령을 치지 않아도 **에이전트가 집어** 실행할 수 있다.

1. **마스터는 그 소스를 실행하지 않는다.** 우리 코드가 읽는 것은 `contract/` 아래
   마크다운뿐이다(`specialist_adapters.py`, `summary_review.py`). import 도, 하위
   프로세스 실행도 없다.
2. **연동은 읽기 전용 도구 모드다.** `execution_mode = "tools"` — 전문 봇은 우리
   아카이브 위 도구(`search`·`read_channel`·`read_document`·`fetch_recent_slack`)만
   부르고, 권한은 `RequestContext` 한 곳에서 판정된다(원칙 3). 전문 봇에게 자기
   권한은 없다.
3. **배포가 소스를 싣지 않는다.** `deploy/install.sh` 는 `subbots/*/contract/prompt.md`
   만 설치한다. 서버에 쓰기 경로가 생기지 않는다.
4. **쓰기 진입점은 목록으로 고정한다.** 스케줄러·CLI·스킬의 수집·소급·첨부 저장
   경로를 전수 적고 시험이 그 목록을 잠근다. 새 경로가 생기면 시험이 깨진다 —
   조용히 늘어나는 것을 막는 것이 핵심이다.

목록과 근거: [`docs/design/hermes-write-entrypoints.md`](../docs/design/hermes-write-entrypoints.md)
고정: [`tests/test_hermes_write_boundary.py`](../tests/test_hermes_write_boundary.py)

> 넷 중 하나라도 못 지키면 소스를 여기 두지 않는다. 지킬 수 없는 구현은 별도
> 저장소로 간다.

## 다른 전문 봇

각 전문 봇은 개발팀이 소유한 별도 Git 저장소에서 개발하고 독립적으로 배포한다. 실행형
전문 봇은 마스터의 `/opt/tybot` 밖에 설치하며, 마스터 배포 스크립트가 해당 디렉터리를
복사하거나 재시작하지 않게 한다. 콘솔은 전문 봇의 등록·승인·활성화·중지와 보고된 버전,
계약 검사, 헬스만 관리한다.

PF 가 운영하는 Hermes 는 **PF 의 배포가 돌린다.** 이 저장소의 `hermes/` 사본은 PF
운영에 쓰이지 않으며, PF 의 질문·DM·요약 기능에 영향을 주지 않는다.

새 저장소는 [`templates/specialist-bot/`](../templates/specialist-bot/)을 복사해 시작한다.
템플릿 디렉터리 자체에 `.git`을 만들지 말고, 복사한 별도 프로젝트에서 Git 저장소를
초기화한다.

구체적인 배포 방식과 보안 경계는
[`docs/design/specialist-deployment.md`](../docs/design/specialist-deployment.md)를 따른다.
