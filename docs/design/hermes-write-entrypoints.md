# Hermes 원문 쓰기 진입점 — 목록과 경계

> 작성: 2026-10-02
> 대상: `subbots/hermes/` (우리 저장소에 들어온 Hermes 구현)
> 고정: [`tests/test_hermes_write_boundary.py`](../../tests/test_hermes_write_boundary.py)

## 1. 왜 목록이 필요한가

`82c120f` 로 Hermes 전체 소스(277 파일)가 `subbots/hermes/` 에 들어왔다. 그 안에는
**Slack 에서 원문을 받아 아카이브에 쓰는 경로가 여럿** 있다 — 스케줄러, CLI, 그리고
Claude Code 스킬까지.

지금 이 경로들은 **아무도 실행하지 않는다.** TYBot 은 Hermes 소스를 돌리지 않고
`contract/prompt.md` 만 읽는다(§3). 그런데 「지금 안 돈다」 와 「돌 수 없다」 는 다르다.
`npm run ingest` 한 줄이면 돌고, 스킬은 **에이전트가 집어서** 돌릴 수도 있다.

그래서 목록을 만들고 시험으로 고정한다. **새 쓰기 경로가 생기면 시험이 깨진다** —
조용히 늘어나는 것을 막는 것이 이 문서의 전부다.

## 2. 쓰기·수집 진입점 전수

### 2.1 스케줄러 — `src/scheduler.js`

| 작업 | 하는 일 | 쓰나 |
|---|---|---|
| `ingest` · `ingestPre` | 자동 반영 (`ingest/index.js` → `runIngest`) | **예** |
| `daily` · `weekly` | 요약 생성·Slack 전송 | **예**(요약) |
| `health` · `healthPre` | 위생 점검 | 아니오 |

`npm start` → `src/index.js` → 이 스케줄러. **기동만으로 쓰기가 예약된다.**

### 2.2 CLI — `package.json` scripts

| 스크립트 | 쓰나 | 비고 |
|---|---|---|
| `ingest` | **예** | 수집·반영. `--dry` 로 억제 가능 |
| `backfill` | **예** | 소급 수집 |
| `init-archive` | **예** | 저장소 씨앗 생성 |
| `digest:daily` · `digest:weekly` | **예** | 요약 + Slack 전송 |
| `start` | **예**(간접) | 스케줄러 기동 |
| `ask` · `health` · `doctor` · `deployed` | 아니오 | 읽기·진단 |
| `check` · `check:offline` · `check:archive` · `check:live` | 아니오 | 점검 |
| `log:render` · `log:measure` | 아니오 | 로그 가공 |

### 2.3 스킬 — `.claude/skills/*/scripts/`

**가장 위험한 축이다.** 사람이 명령을 치지 않아도 에이전트가 고를 수 있다.

| 스킬 | 스크립트 | 하는 일 |
|---|---|---|
| `doc-archive` | `fetch_slack_files.py` | Slack 파일 **다운로드** |
| | `insert_entry.py` | 아카이브 **삽입** |
| | `join_channels.py` | 채널 **참여** |
| | `apply_approvals.py` · `review_batch.py` | 승인 반영·검토 기록 |
| | `check_against_libreoffice.py` · `xlsx_to_blocks.py` | 외부 프로세스 실행 |
| `slack-sync` | `insert_messages.py` | 원문 메시지 **삽입** |
| | `apply_edits.py` · `verify_archive.py` · `verify_backfill.py` | 편집 반영·검증 |
| `archive-inbox` | `decide_work.py` · `review_work.py` | 작업 판정·기록 |
| `archive-run` | `board.py` | 진행판 기록 |

### 2.4 쓰기 원시 함수 — `src/`

| 모듈 | 호출 |
|---|---|
| `src/ingest/util.js` | `writeFile` · `mkdirSync` · `renameSync` · `rmSync` · `execFile` |
| `src/convo-log/store.js` | `appendFile` · `writeFile` · `mkdirSync` · `renameSync` |
| `src/slack-live.js` | `writeFile` · `mkdirSync` · `renameSync` |
| `src/ingest/git.js` · `verify.js` | `execFile` (git) |
| `src/ingest/slack-archive.js` · `pending-work.js` | `unlinkSync` |

## 3. TYBot 연동 모드 — 스위치 하나로 막는다 (2026-10-06 구현)

```bash
HERMES_MODE=tybot    # 연동. 원문 쓰기를 전부 막는다
                     # (미설정·그 외) PF 직접 실행 — 지금까지와 똑같다
```

판정은 **두 곳에서만** 읽는다 — 봇 쪽 [`src/mode.js`](../../subbots/hermes/src/mode.js),
스킬 쪽 [`.claude/skills/_shared/mode.py`](../../subbots/hermes/.claude/skills/_shared/mode.py).
여기저기서 읽으면 판정이 갈리고, 갈리면 오류가 아니라 **한쪽만 막힌 상태**로
나타난다 — 봇은 멈췄는데 스킬은 쓰고 있는 식이다.

### 막는 자리 — 네 축 전부

| 축 | 어떻게 | 어디 |
|---|---|---|
| 스케줄러 | 쓰는 작업을 **예약하지 않는다** | `src/scheduler.js` |
| CLI | 종료 코드 **2** 로 멈춘다(실패 1과 구별) | `run-ingest` · `run-backfill` · `init-archive` |
| 스킬 | `main()` **첫 줄**에서 종료 코드 2 | 쓰기 스킬 10개 |
| 직접 모듈 호출 | `ArchiveWriteBlocked` 를 던진다 | `runIngest` · `ingestConversations` · `writeMonth` · `commitAndPush` |

관문은 **맨 앞**에 둔다. `runIngest` 는 아래에서 git sync 를 도는데, 관문이 그 뒤면
트리를 건드린 뒤에 막는 꼴이라 「막혔는데 작업 트리는 더러워진」 상태가 남는다.
스킬도 같다 — 인자 해석이나 Slack 호출이 먼저 돌면 막기 전에 네트워크로 나가고,
그건 되돌릴 수 없다.

`commitAndPush` 는 자료 저장소에 **실제로 쓰는 유일한 자리**다. 위 관문을 전부
우회해도 여기서 막힌다.

### 기본값이 PF 인 이유

켜는 쪽을 기본값으로 두면 환경변수를 안 넘긴 PF 운영이 어느 날 조용히 멈춘다.
**막는 쪽이 기본값**(원칙 3)과 반대로 보이지만 대상이 다르다 — 원칙 3 은 「자료
열람」의 기본값이고, 여기는 「이미 돌고 있는 운영」의 기본값이다. 운영을 끄는 결정은
명시적이어야 한다. 모르는 값(오타)도 PF 로 본다.

### 회귀 시험

[`tests/test_hermes_tybot_mode.py`](../../tests/test_hermes_tybot_mode.py) 가 **우회
진입점별로** 본다. `node_modules` 가 있으면 실제로 실행해 막히는지 보고, 없으면
정적으로 관문의 존재와 위치를 본다 — 둘 다 없으면 「설치 안 돼서 통과」 가 되고
그게 가장 나쁜 초록불이다.

## 3.1 왜 이것만으로 충분한가 — 연동의 원래 구조

연동 선언(`subbots/hermes/tybot-specialist.toml`)이 이미 이렇게 정한다.

```toml
execution_mode = "tools"
prompt = "contract/prompt.md"
```

- `tools` = Hermes 는 **우리 아카이브 위의 도구**(`search` · `read_channel` ·
  `read_document` · `fetch_recent_slack`)만 부른다. 그 도구는 전부
  `src/tybot/specialist_tools.py` 안에 있고 **읽기 전용**이다.
- 권한은 `RequestContext` 하나로 판정된다(원칙 3). Hermes 는 자기 권한이 없다.
- 우리 코드가 Hermes 에서 읽는 것은 **`contract/` 아래 마크다운뿐**이다
  (`specialist_adapters.py`, `summary_review.py`). 소스를 import 하거나 실행하는
  경로가 없다.
- 배포도 같다 — `deploy/install.sh` 는 `subbots/*/contract/prompt.md` 만 설치한다.

> 그래서 **TYBot 이 Hermes 를 부르는 경로로는** 원문이 써질 수 없었다. 그런데
> 그것은 「아무도 안 부른다」 이지 「부를 수 없다」 가 아니다 — `npm run ingest`
> 한 줄이면 돌고, 스킬은 에이전트가 집어서 돌릴 수도 있다. §3 의 스위치가 그
> 차이를 메운다.

## 4. PF 직접 호출은 건드리지 않는다

PF 는 자기 배포에서 Hermes 를 직접 돌린다. 질문·DM·요약이 거기서 나온다.
**이 저장소의 사본은 PF 운영에 쓰이지 않는다**(`subbots/pf-hermes-archive/` ·
`subbots/pf-hermes-source/` 는 `.gitignore` 로 막혀 있다).

따라서 이 문서의 어떤 항목도 PF 기능을 끄지 않는다. **PF 의 원문 writer 를 끄는
전환은 하지 않는다** — 별도 게이트와 회귀 시험을 갖추기 전에는 실행 금지다(오너 지시,
2026-10-02). 그 전환을 하려면 최소한 이것들이 먼저 있어야 한다.

- [ ] 끄고 켜는 **단일 스위치**(환경변수 하나, 기본값 = 켜짐)
- [ ] 꺼진 상태에서 질문·DM·요약이 **그대로 동작**하는 회귀 시험
- [ ] 꺼진 상태에서 수집·소급·첨부 저장이 **전부 거부**되는 시험
- [ ] 되돌리는 절차와, 되돌렸을 때 유실이 없다는 근거

## 5. 시험이 고정하는 것

두 파일이 **서로 다른 질문**을 본다. 하나만 있으면 반쪽이다 — 목록이 맞아도 관문이
안 걸려 있으면 아무것도 막히지 않고, 관문이 있어도 새 진입점이 생기면 그리로 샌다.

| 파일 | 보는 것 |
|---|---|
| `test_hermes_write_boundary.py` | 진입점이 **늘지 않는가** |
| `test_hermes_tybot_mode.py` | 각 진입점이 **실제로 막히는가** |

### 5.1 진입점이 늘지 않는다

[`tests/test_hermes_write_boundary.py`](../../tests/test_hermes_write_boundary.py)

1. **진입점 인벤토리가 늘지 않는다** — npm 스크립트·스킬 쓰기 스크립트·`src` 쓰기
   모듈이 위 표와 정확히 같다. 새 경로가 생기면 **이 문서를 고쳐야** 통과한다.
2. **TYBot 은 Hermes 소스를 실행하지 않는다** — 우리 `src/` 어디에도 `subbots/hermes`
   의 `.js`·`.py` 를 부르는 경로가 없다. 읽는 것은 `contract/` 뿐이다.
3. **배포가 소스를 싣지 않는다** — `install.sh` 는 계약 파일만 설치한다.
4. **연동 선언이 `tools` 모드를 유지한다** — `execution_mode` 가 바뀌면 깨진다.

시험이 깨졌을 때 할 일은 「시험을 고치는 것」이 아니라 **왜 쓰기 경로가 늘었는지
확인하는 것**이다. 늘려야 할 이유가 있으면 이 문서에 적고 시험을 같이 고친다.

### 5.2 각 진입점이 실제로 막힌다

[`tests/test_hermes_tybot_mode.py`](../../tests/test_hermes_tybot_mode.py)

- 네 축(스케줄러·CLI·스킬·직접 모듈 호출)을 **진입점마다 하나씩** 본다
- 관문이 **앞머리에** 있는지도 본다 — 뒤에 있으면 막기 전에 부작용이 나간다
- `HERMES_MODE` 를 읽는 곳이 `src/mode.js` · `_shared/mode.py` **둘뿐**인지 본다
- **PF 모드에서는 막히지 않는 것**도 같이 본다. 차단을 넣으면서 PF 운영을 조용히
  멈추는 것이 가장 나쁜 실패다
