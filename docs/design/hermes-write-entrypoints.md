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
| | `decision_export.py` (2026-10-06) | **검토 결정 기록만** 쓴다 — 원문·아카이브는 안 건드린다 |
| `archive-run` | `board.py` | 진행판 기록 |

`decision_export.py` 는 원문을 쓰지 않는다. 그래도 이 표에 있는 이유는 이 목록이
「원문 쓰기」 목록이 아니라 **쓰기 성격의 진입점** 목록이기 때문이다 — 예외를 하나
두면 다음 사람이 그 파일에 원문 쓰기를 더해도 관문(`test_hermes_write_boundary.py`)이
안 걸린다.

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

## 6. 실행 역할과 검토 대조 (2026-10-06 구현)

### 6.1 빠지는 것은 「요약」 이 아니라 「직접 게시」 다

> **2026-10-06 정정.** 처음에 이 절을 「요약 발송은 TYBot 이 맡는다」 로 적었다.
> 틀렸다 — 그렇게 읽으면 **요약의 주인이 TYBot** 이 되고, 목표 구조와 반대로 굳는다.

[`archiving-bot-separation-2026-09-23.md`](archiving-bot-separation-2026-09-23.md)
가 정한 목표 소유자는 이렇다.

| 책임 | 목표 소유자 |
|---|---|
| 원문 수집·첨부 변환·provenance | **Archiving Bot** |
| 검토 후보·정정·승인/만료, 정기 요약, 검색·Q&A | **Hermes** |
| 신원·권한, 전문가 선택, 요청/응답 전달·감사 | **TYBot Master** |

그래서 연동에서 Hermes 가 내려놓는 것은 **자기 스케줄로 Slack 에 직접 올리는 경로**
하나다. 요약을 만드는 규칙은 그대로 Hermes 것이고, TYBot 은 **승인 인터페이스**다.

```text
근거(권한 검사)  TYBot  ─┐
요약 규칙·후보   Hermes ─┤─▶ 사람 확인(DM·Canvas)  TYBot  ─▶ 승인 반영
재요약           Hermes ─┘
```

구조는 `archive-run` 과 같다. 상황판은 **무엇이 남았나·순서·마감**만 알고 절차는
하위 스킬이 쥔다. 여기서는 TYBot 이 상황판, Hermes 가 절차다. 절차를 상황판에
복사하지 않는 이유도 같다 — **복사하면 원본이 바뀔 때 에러 없이 낡는다.**

### 6.1.1 역할표 — 현재와 목표

| 역할 | PF 직접 실행 | TYBot 연동 (현재) | 목표 |
|---|---|---|---|
| `ingest` 수집·소급·첨부 | Hermes | 차단 | **Archiving Bot** (역할 자체가 사라짐) |
| `digest-publish` **직접 게시** | Hermes | 차단 | 차단 유지 — 게시는 승인 뒤에만 |
| `health` 위생 점검 발송 | Hermes | 차단 | Archiving Bot 자료 기준으로 재정의 |
| 독립 Node `answer` 질문·DM 응답 | Hermes | **차단** | TYBot이 Hermes 계약을 권한 도구로 호출 |

`HERMES_MODE=tybot` 은 Node Hermes를 TYBot 옆에 하나 더 띄우는 모드가 아니다.
TYBot 연동의 Hermes는 `tybot-specialist.toml`의 `execution_mode="tools"` 계약이다.
따라서 독립 `npm start`·`npm run ask`·직접 `answerQuestion()`도 막힌다. `npm start`는
의존성 없는 `scripts/run-server.js`가 설정·토큰·아카이브보다 먼저 판정한다. 읽기 전용이라도
Hermes 로컬 아카이브를 열면 TYBot의 `RequestContext` 권한 판정을 우회하기 때문이다.

이름이 `digest` 가 아니라 `digest-publish` 인 이유가 이 표다. 「요약을 안 맡는다」
가 아니라 「직접 올리지 않는다」 다.

막는 자리는 셋이다 — **스케줄 등록** · **발송 함수**(`runDigest`·`runHealth` 앞머리)
· **CLI**. 스케줄만 막으면 손으로 돌리는 길이 남고, 그게 중복 게시의 실제 경로다.

### 6.2 요약 후보 계약 — 이어져 있고, 한 군데가 비어 있다

`summary_review.contract_prompt()` 가 `subbots/hermes/contract/summary-review.md` 를
**런타임에** 읽는다. 규칙을 TYBot 안으로 복사하지 않는다 — 그 점은 §6.1 의 원칙대로다.

| 단계 | 지금 | 목표 |
|---|---|---|
| 근거 수집 | TYBot `_source_rows` (권한 검사) | 그대로 |
| 후보 생성 | TYBot 이 Hermes **규칙**으로 LLM 호출 | 그대로(규칙 주인은 Hermes) |
| 후보 검증 | TYBot `parse_proposals` | 그대로 — 근거 대조는 근거 주인이 한다 |
| 사람 확인 | TYBot Canvas·DM | 그대로 |
| **재요약** | TYBot `projected_summary()` — **결정적 조합** | **Hermes** |

마지막 줄이 비어 있는 자리다. 지금은 승인 항목을 코드가 문자열 치환으로 조합한다
(`summary-review-canvas.md` §2.1 — 「LLM 을 다시 호출해 만들지 않는다」). 그건 모호한
교체를 막으려는 **의도된 결정**이었고, 목표 구조(재요약은 Hermes)와 충돌한다.

바꾸려면 CLAUDE.md 원칙 1 의 파생 요약 다섯 조건을 전부 지켜야 한다 — 사람 승인
전제 · 원문 좌표 상속 · 묶음 상한 · 세대 상한 2 · 연쇄 stale·단일 채널.
**그 전환은 이 변경에 포함하지 않았다.**

### 6.3 이미 끝낸 검토를 생략하는 조건

구현: [`src/tybot/summary_review_reconcile.py`](../../src/tybot/summary_review_reconcile.py)

**「로컬 스킬이 돌았다」 는 생략의 근거가 아니다.** 그것은 어떤 항목을 어떤 원문으로
끝냈는지 아무것도 말해 주지 않는다. 보는 것은 둘이다 — **같은 원문 좌표**인가,
**그 좌표의 해시가 그대로**인가. 그 위에 **확정된 결정**(승인·거절)만 쓴다.

아래 중 하나라도 걸리면 생략하지 않는다.

| 사유 코드 | 뜻 |
|---|---|
| `records_unreadable` | 기록을 못 읽었다 (없는 것과 다르다) |
| `no_match` | 그 좌표에 대한 결정이 없다 |
| `not_final` | 보류 — 아직 끝난 것이 아니다 |
| `source_mismatch` | 워크스페이스·채널이 다르다 |
| `evidence_changed` | 결정 이후 원문이 바뀌었다 |
| `no_coordinate` | 좌표·해시가 없어 대조할 수 없다 |

묻는 쪽으로 틀리면 사람이 한 번 더 볼 뿐이지만, **생략하는 쪽으로 틀리면 아무도
모른다.**

### 6.4 지금은 아무것도 생략되지 않는다 — 그리고 그게 맞다

Hermes 의 결정 기록(`.sync-state.json` 의 `applied`·`dismissed`)은 **항목 id 와 요약
섹션 해시**만 담는다. 원문 줄 좌표도, 그 줄의 해시도 없다. §6.3 의 조건을 채울 수
없으므로 대조는 늘 `no_match`·`source_mismatch` 로 끝난다.

생략하려면 Hermes 가 결정 시점에 아래를 함께 적어야 한다.

```json
{"decisions": [{
  "workspace": "tyit", "channel_id": "C0BQ…",
  "evidence_locator": "2026-10-02.md:42",
  "evidence_hash": "<그 줄의 content hash>",
  "state": "approved",
  "decided_at": "2026-10-02T09:00:00+09:00", "decided_by": "…"
}]}
```

### 6.5 좌표를 채웠다 — 그리고 사내·PF 사이는 여전히 안 맞는다 (2026-10-06, 2단계)

위 §6.4 의 「하지 않았다」 를 **PF 경로에 한해** 채웠다. `decide_work.py` 가 결정하는
순간 `workspace` · `channel_id` · `evidence_locator` · `evidence_hash` · `state` 를
`.sync-state.json` 에 함께 적고, `decision_export.py` 가 그것을
`summary-review-decisions/v1` 로 내보낸다. 자리와 형식은
[`summary-approval-ports.md`](summary-approval-ports.md) §3.A·§3.B.

| 무엇 | Hermes 쪽 값 |
|---|---|
| `workspace` | `config.json` 의 `workspace`. **비어 있으면 아무것도 내보내지 않는다** — 빈 값으로 적으면 받는 쪽에서 아무 결정에도 안 걸리고, 그 상태는 「상대편이 아직 안 했다」 와 화면에서 같다 |
| `channel_id` | `.sync-state.json` 의 `channels[<id>]` 를 뒤집어 얻는다. **한 파일에 ID 가 둘이면 안 쓴다**(개명 이력) |
| `evidence_locator` | `slack-export/channels/<파일>.md#L<첫줄>-L<끝줄>` — **메시지 블록** |
| `evidence_hash` | 그 블록의 sha256(앞 32자). 줄끝은 지문에 안 넣는다 |

**좌표는 결정하는 순간에 적는다.** 나중에 다시 재면 그 뒤 바뀐 원문의 지문이 나오고,
그러면 사람이 보고 정한 것이 아닌 것에 승인이 붙는다.

**좌표를 지어내지 않는다.** 아래 중 하나면 좌표를 비워 두고, 받는 쪽은
`no_coordinate` 로 **다시 묻는다.**

| 비우는 경우 | 왜 |
|---|---|
| 인용을 원문에서 못 찾았다 | 어느 메시지인지 모른다 |
| 근거가 **여러 메시지**에 걸쳐 있다 | 블록 하나를 적으면 사람이 본 근거의 일부만 가리킨다 |
| 원문에 없는 인용 조각이 섞여 있다 | 그 사실이 기록에서 사라진다 |
| 요약 계열이 아니다(새 채널·개명·note·파생값) | 근거 인용이 아예 없다 |
| 채널 ID 나 워크스페이스를 못 냈다 | 어느 자료인지 특정되지 않는다 |

탐색은 **한 자리에서만** 한다. `review_work.evidence_block()` 을 화면(`context_for`)과
좌표 도출이 함께 쓴다 — 따로 찾으면 사람이 본 원문과 기록의 좌표가 다른 메시지를
가리킬 수 있고, 그때 승인과 기록이 어긋난다. 관문:
`tests/test_archive_inbox_reconcile.py` ⑫.

**사내↔PF 사이는 여전히 `no_match` 다.** 좌표계가 둘이기 때문이다(§6.4 위의 표,
`summary-approval-ports.md` §3.2). 변환층은 **만들지 않았다** — 변환이 어긋난 날 다른
메시지를 같은 것으로 보게 되고, 그건 조용히 틀린다. 지금 닫힌 고리는 **PF 안**
(한 PC 에서 정한 것을 다른 PC·VM 이 읽는다)이고, 두 좌표계가 하나가 되는 것은
아카이브가 Archiver 아래로 모일 때다.
