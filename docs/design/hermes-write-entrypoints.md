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

## 3. TYBot 연동 모드 — 쓰기는 **구조적으로** 닫혀 있다

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

> **그래서 「연동 모드에서 원문 쓰기를 막는다」 는 새로 만들 기능이 아니라
> 이미 참인 사실이다.** 할 일은 그것이 **계속 참이게** 못 박는 것이다 — §5.

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

[`tests/test_hermes_write_boundary.py`](../../tests/test_hermes_write_boundary.py)

1. **진입점 인벤토리가 늘지 않는다** — npm 스크립트·스킬 쓰기 스크립트·`src` 쓰기
   모듈이 위 표와 정확히 같다. 새 경로가 생기면 **이 문서를 고쳐야** 통과한다.
2. **TYBot 은 Hermes 소스를 실행하지 않는다** — 우리 `src/` 어디에도 `subbots/hermes`
   의 `.js`·`.py` 를 부르는 경로가 없다. 읽는 것은 `contract/` 뿐이다.
3. **배포가 소스를 싣지 않는다** — `install.sh` 는 계약 파일만 설치한다.
4. **연동 선언이 `tools` 모드를 유지한다** — `execution_mode` 가 바뀌면 깨진다.

시험이 깨졌을 때 할 일은 「시험을 고치는 것」이 아니라 **왜 쓰기 경로가 늘었는지
확인하는 것**이다. 늘려야 할 이유가 있으면 이 문서에 적고 시험을 같이 고친다.
